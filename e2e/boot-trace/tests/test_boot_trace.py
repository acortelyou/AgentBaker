"""Synthetic, offline tests. No Azure, SSH, tracing, installs or image builds."""

import argparse
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from events import ftrace_events, lttng_events
from guest.capture import trim_partial_line
from milestones import import_cse
from orchestrate import provider, scale_and_collect, unpack
from report import write_html
from timeline import analyze
from trace_boot import main


BOOT = "11111111-1111-1111-1111-111111111111"
SUB = "22222222-2222-2222-2222-222222222222"
PROVIDER_ID = (
    f"azure:///subscriptions/{SUB}/resourceGroups/nodes/providers/"
    "Microsoft.Compute/virtualMachineScaleSets/aks-pool-vmss/virtualMachines/0"
)


def event(t, prev, nxt, state="S", cpu=0):
    return (
        f"    worker-{prev} [{cpu:03d}] d..2 {t:.6f}: sched_switch: "
        f"prev_comm=worker prev_pid={prev} prev_prio=120 prev_state={state} ==> "
        f"next_comm=worker next_pid={nxt} next_prio=120\n"
    )


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "capture"
        self.bundle.mkdir()
        self.metadata = {
            "schema": 1, "backend": "ftrace", "boot_id": BOOT, "kernel": "test",
            "architecture": "x86_64", "status": "stopped", "finalized_at": "test",
            "clock_start": {"realtime": 1010, "monotonic": 10, "sample_span_seconds": 0.001},
            "clock_end": {"realtime": 1012, "monotonic": 12, "sample_span_seconds": 0.001},
        }
        self.write("metadata.json", self.metadata)
        self.write("readiness.json", {
            "boot_id": BOOT, "transition_time": "1970-01-01T00:16:42Z",
        })
        (self.bundle / "ftrace.txt").write_text(
            event(1, 0, 42) + event(1.2, 42, 0, "R")
            + event(1.3, 0, 42) + event(1.5, 42, 0) + event(2.2, 0, 0)
        )

    def write(self, name, value):
        (self.bundle / name).write_text(json.dumps(value))

    def test_cpu_and_runnable_accounting(self):
        result = analyze(self.bundle)
        thread = next(row for row in result["threads"] if row["tid"] == 42)
        self.assertAlmostEqual(thread["cpu_seconds"], 0.4)
        self.assertAlmostEqual(thread["runnable_seconds"], 0.1)
        self.assertEqual(result["ready_seconds"], 2)

    def test_running_interval_is_clipped_to_ready_not_last_pre_ready_event(self):
        (self.bundle / "ftrace.txt").write_text(event(1, 0, 42) + event(2.2, 42, 0))
        thread = next(row for row in analyze(self.bundle)["threads"] if row["tid"] == 42)
        self.assertAlmostEqual(thread["cpu_seconds"], 1)

    def test_wrong_boot_rejected(self):
        self.write("readiness.json", {"boot_id": "other", "transition_time": "1970-01-01T00:16:42Z"})
        with self.assertRaisesRegex(ValueError, "BootID"):
            analyze(self.bundle)

    def test_missing_ready_is_unknown_not_zero(self):
        (self.bundle / "readiness.json").unlink()
        result = analyze(self.bundle)
        self.assertIsNone(result["ready_seconds"])
        self.assertTrue(any("readiness evidence" in item for item in result["warnings"]))

    def test_clock_jump_and_ring_loss_are_visible(self):
        self.metadata["clock_end"]["realtime"] += 5
        self.write("metadata.json", self.metadata)
        self.write("ftrace-stats.json", {"per_cpu/cpu0/stats": "overrun: 17\n"})
        warnings = "\n".join(analyze(self.bundle)["warnings"])
        self.assertIn("mapping changed", warnings)
        self.assertIn("Ring buffer loss", warnings)

    def test_out_of_order_stream_is_not_silently_accounted(self):
        (self.bundle / "ftrace.txt").write_text(event(1.2, 0, 42) + event(1.1, 42, 0))
        with self.assertRaisesRegex(ValueError, "chronologically merged"):
            analyze(self.bundle)

    def test_unknown_text_is_reported(self):
        with (self.bundle / "ftrace.txt").open("a") as source:
            source.write("unexpected record\n")
        self.assertTrue(any("could not be decoded" in item for item in analyze(self.bundle)["warnings"]))

    def test_semantic_services_do_not_become_cpu_time(self):
        self.write("services.json", [
            {"name": "containerd.service", "start_monotonic_us": 100000, "active_monotonic_us": 900000},
            {"name": "kubelet.service", "start_monotonic_us": 200000, "active_monotonic_us": 800000},
        ])
        result = analyze(self.bundle)
        self.assertEqual(len(result["services"]), 2)
        self.assertAlmostEqual(sum(row["cpu_seconds"] for row in result["threads"]), 0.4)

    def test_parallel_cpu_work_is_not_serial_wall_time(self):
        (self.bundle / "ftrace.txt").write_text(
            event(1, 0, 42, cpu=0) + event(1.1, 0, 43, cpu=1)
            + event(2.2, 42, 0, cpu=0) + event(2.3, 43, 0, cpu=1)
        )
        result = analyze(self.bundle)
        self.assertAlmostEqual(sum(row["cpu_seconds"] for row in result["threads"]), 1.9)
        self.assertEqual(result["ready_seconds"], 2)

    def test_reused_tid_gets_separate_generations(self):
        (self.bundle / "ftrace.txt").write_text(
            event(1, 0, 42) + event(1.2, 42, 0)
            + " worker-1 [000] d..2 1.400000: sched_process_fork: "
              "comm=parent pid=1 child_comm=child child_pid=42\n"
            + event(1.5, 0, 42) + event(1.8, 42, 0) + event(2.2, 0, 0)
        )
        threads = sorted(
            (row for row in analyze(self.bundle)["threads"] if row["tid"] == 42),
            key=lambda row: row["generation"],
        )
        self.assertEqual([row["generation"] for row in threads], [0, 1])
        self.assertAlmostEqual(threads[0]["cpu_seconds"], 0.2)
        self.assertAlmostEqual(threads[1]["cpu_seconds"], 0.3)

    def test_scheduler_gap_is_not_charged_as_cpu(self):
        (self.bundle / "ftrace.txt").write_text(
            event(1, 0, 42) + event(1.2, 99, 0) + event(2.2, 0, 0)
        )
        result = analyze(self.bundle)
        self.assertEqual(sum(row["cpu_seconds"] for row in result["threads"]), 0)
        self.assertTrue(any("discontinuities" in warning for warning in result["warnings"]))

    def test_missing_scheduler_is_unknown_not_zero(self):
        (self.bundle / "ftrace.txt").write_text(
            " worker-1 [000] d..2 1.400000: sched_process_fork: "
            "comm=parent pid=1 child_comm=child child_pid=42\n"
        )
        self.assertIsNone(analyze(self.bundle)["threads"][0]["cpu_seconds"])

    def test_loss_marker_invalidates_open_scheduler_intervals(self):
        (self.bundle / "ftrace.txt").write_text(
            event(1, 0, 42) + "CPU:0 [LOST 3 EVENTS]\n"
            + event(1.8, 42, 0) + event(2.2, 0, 0)
        )
        result = analyze(self.bundle)
        self.assertEqual(sum(row["cpu_seconds"] for row in result["threads"]), 0)
        self.assertTrue(any("lost events" in warning for warning in result["warnings"]))

    def test_io_activity_has_time_bins_not_thread_attribution(self):
        with (self.bundle / "ftrace.txt").open("a") as source:
            source.write(" worker-1 [000] d..2 2.400000: block_rq_issue: 8,0 WS 0 () 1 + 8 [worker]\n")
        # Events after Ready are excluded from the activity view.
        self.assertEqual(analyze(self.bundle)["block_event_count"], 0)
        (self.bundle / "readiness.json").unlink()
        result = analyze(self.bundle)
        self.assertEqual(result["block_event_count"], 1)
        self.assertEqual(result["io_bins"][0][1], {"block_rq_issue": 1})

    def test_no_script_injection_in_html_payload(self):
        result = analyze(self.bundle)
        result["warnings"].append('</script><img src=x onerror="alert(1)">')
        output = self.root / "report.html"
        write_html(output, [result], ["Capture"])
        text = output.read_text()
        self.assertNotIn("</script><img", text)
        self.assertIn("\\u003c/script\\u003e", text)
        self.assertIn("connect-src 'none'", text)

    def test_cli_writes_html_and_json(self):
        output = self.root / "report"
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["report", str(self.bundle), "--output", str(output)]), 0)
        self.assertTrue((output / "report.html").is_file())
        self.assertEqual(json.loads((output / "report.json").read_text())["ready_seconds"], 2)


class ArchiveTests(unittest.TestCase):
    def test_rejects_traversal_and_links(self):
        for name, kind in [(BOOT + "/../escape", tarfile.REGTYPE), (BOOT + "/link", tarfile.SYMTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "capture.tar"
                with tarfile.open(archive, "w") as output:
                    info = tarfile.TarInfo(name)
                    info.type = kind
                    info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
                    output.addfile(info)
                with self.assertRaises(ValueError):
                    unpack(archive, root / "output", BOOT, 1024)

    def test_rejects_expansion_over_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "capture.tar"
            with tarfile.open(archive, "w") as output:
                info = tarfile.TarInfo(BOOT + "/large")
                info.size = 10
                output.addfile(info, io.BytesIO(b"0123456789"))
            with self.assertRaisesRegex(ValueError, "byte limit"):
                unpack(archive, root / "output", BOOT, 5)

    def test_partial_trace_tail_is_removed_without_losing_complete_records(self):
        source = io.BytesIO(b"complete\nanother complete\npartial")
        source.seek(0, io.SEEK_END)
        self.assertTrue(trim_partial_line(source))
        self.assertEqual(source.getvalue(), b"complete\nanother complete\n")


class OrchestrationTests(unittest.TestCase):
    def args(self, output):
        return argparse.Namespace(
            subscription=SUB, resource_group="rg", cluster="test", pool="pool",
            kubeconfig=Path("/not-read-by-mock"), context="explicit", ssh_user="azureuser",
            ssh_config=None, backend="ftrace", output=output, timeout=600, max_mib=256,
            execute=False, instrumentation_confirmed=False,
        )

    def responses(self, autoscale=False):
        return [
            json.dumps({"id": "test-cluster", "provisioningState": "Succeeded",
                        "nodeResourceGroup": "nodes", "fqdn": "test.example"}),
            json.dumps({"type": "VirtualMachineScaleSets", "osType": "Linux",
                        "provisioningState": "Succeeded", "count": 1,
                        "enableAutoScaling": autoscale}),
            json.dumps({"clusters": [{"cluster": {"server": "https://test.example:443"}}]}),
            json.dumps({"metadata": {"resourceVersion": "10"}, "items": [
                {"metadata": {"name": "old", "uid": "old"}, "spec": {"providerID": PROVIDER_ID}},
            ]}),
            '["0"]',
        ]

    def test_dry_run_never_scales_watches_or_creates_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "absent"
            with patch("orchestrate.command", side_effect=self.responses()) as call, \
                    patch("orchestrate.NodeWatch") as watch, patch("sys.stdout", new_callable=io.StringIO):
                scale_and_collect(self.args(output))
            self.assertFalse(output.exists())
            watch.assert_not_called()
            self.assertTrue(all("scale" not in invocation.args[0] for invocation in call.call_args_list))

    def test_autoscaler_rejected_before_mutation(self):
        with patch("orchestrate.command", side_effect=self.responses(True)):
            with self.assertRaisesRegex(ValueError, "without autoscaler"):
                scale_and_collect(self.args(Path("/unused")))

    def test_provider_identity_is_not_inferred_from_node_name(self):
        with self.assertRaisesRegex(ValueError, "providerID"):
            provider({"metadata": {"name": "aks-pool-vmss000000"}, "spec": {}})

    def test_watch_is_started_before_scale_and_survives_no_ready_as_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(Path(directory) / "capture")
            args.execute = args.instrumentation_confirmed = True
            sequence = []
            responses = iter(self.responses())

            def invoke(command, timeout=90):
                if "scale" in command:
                    sequence.append("scale")
                    return ""
                return next(responses)

            watch = unittest.mock.Mock()
            watch.next.side_effect = RuntimeError("watch disconnected")

            def start_watch(*_args):
                sequence.append("watch")
                return watch

            with patch("orchestrate.command", side_effect=invoke), \
                    patch("orchestrate.NodeWatch", side_effect=start_watch):
                with self.assertRaisesRegex(RuntimeError, "disconnected"):
                    scale_and_collect(args)
            self.assertEqual(sequence, ["watch", "scale"])
            watch.close.assert_called_once()
            self.assertEqual(json.loads((args.output / "result.json").read_text())["status"], "incomplete")


class LttngTests(unittest.TestCase):
    def test_non_epoch_ctf_requires_explicit_clock_mapping(self):
        class EventMessage:
            default_clock_snapshot = types.SimpleNamespace(
                clock_class=types.SimpleNamespace(origin_is_unix_epoch=False),
            )

        fake = types.SimpleNamespace(
            _EventMessageConst=EventMessage,
            _DiscardedEventsMessageConst=type("DiscardedEvents", (), {}),
            _DiscardedPacketsMessageConst=type("DiscardedPackets", (), {}),
            TraceCollectionMessageIterator=lambda _path: [EventMessage()],
        )
        with patch.dict(sys.modules, {"bt2": fake}):
            with self.assertRaisesRegex(ValueError, "no Unix origin"):
                list(lttng_events(Path("/unused"), []))

    def test_ftrace_headers_are_not_unknown_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            path.write_text("# tracer: nop\n" + event(1, 0, 42))
            warnings = []
            self.assertEqual(len(list(ftrace_events(path, warnings))), 1)
            self.assertEqual(warnings, [])


class CseTests(unittest.TestCase):
    def test_existing_systemd_and_unixdate_formats(self):
        result = import_cse({"BootDatapoints": {
            "KernelStartTime": "Fri 2026-11-06 17:00:00 UTC",
            "CSEStartTime": "Fri Nov  6 17:00:12 UTC 2026",
        }})
        self.assertEqual(result[0]["timestamp"], "2026-11-06T17:00:00+00:00")
        self.assertEqual(result[1]["timestamp"], "2026-11-06T17:00:12+00:00")

    def test_ambiguous_timezone_needs_explicit_offset(self):
        data = {"KubeletStartTime": "Fri 2026-11-06 09:00:00 PST"}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            import_cse(data)
        self.assertEqual(
            import_cse(data, "-08:00")[0]["timestamp"], "2026-11-06T09:00:00-08:00",
        )

    def test_only_boot_fields_are_exported(self):
        result = import_cse({
            "BootDatapoints": {"KernelStartTime": "2026-11-06T17:00:00Z"},
            "Output": "sensitive output must not enter the report",
        })
        self.assertNotIn("sensitive", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
