#!/usr/bin/env python3
"""Root-only guest recorder for a dedicated, pre-instrumented test image."""

import argparse
import datetime
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys
import time


ROOT = Path("/var/log/aks-node-boot-trace")
TRACEFS = Path("/sys/kernel/tracing")
UNITS = (
    "cloud-init-local.service", "cloud-init.service", "cloud-config.service",
    "cloud-final.service", "walinuxagent.service", "waagent.service",
    "containerd.service", "kubelet.service",
)


def run(*args, timeout=30):
    try:
        return subprocess.run(
            args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        ).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"{args[0]} failed: {error.stderr.strip()}") from error


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def clock_sample():
    before = time.monotonic()
    realtime = time.time()
    after = time.monotonic()
    return {
        "monotonic": (before + after) / 2, "realtime": realtime,
        "sample_span_seconds": after - before,
    }


def disk_size(path):
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def systemd_services(diagnostics):
    rows = []
    for unit in UNITS:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=Id,LoadState,"
             "ExecMainStartTimestampMonotonic,ActiveEnterTimestampMonotonic"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30,
        )
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        if values.get("LoadState") == "not-found":
            diagnostics.append(f"No {unit} unit; its activation evidence is unavailable.")
            continue
        result.check_returncode()
        rows.append({
            "name": values["Id"],
            "start_monotonic_us": values.get("ExecMainStartTimestampMonotonic", "0"),
            "active_monotonic_us": values.get("ActiveEnterTimestampMonotonic", "0"),
        })
    return rows


def trim_partial_line(output):
    """Keep only complete trace records after a byte-limited write."""
    end = output.tell()
    cursor = end
    while cursor:
        start = max(0, cursor - 65536)
        output.seek(start)
        newline = output.read(cursor - start).rfind(b"\n")
        if newline >= 0:
            retained = start + newline + 1
            output.truncate(retained)
            output.seek(0, os.SEEK_END)
            return retained < end
        cursor = start
    output.truncate(0)
    return end > 0


def record(args):
    if os.geteuid() != 0:
        raise PermissionError("Capture requires root on a dedicated test node")
    os.umask(0o077)
    if not 0 < args.max_seconds <= 600 or args.max_mib < 16:
        raise ValueError("Duration must be 1..600 seconds, with at least 16 MiB")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    destination = ROOT / boot_id
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    metadata = {
        "schema": 1, "backend": args.backend, "boot_id": boot_id,
        "kernel": os.uname().release, "architecture": os.uname().machine,
        "clock_start": clock_sample(), "status": "recording",
        "image_label": args.image_label,
        "warnings": [
            "Capture starts before selected userspace services, not at kernel time zero.",
            "Service activation timestamps are not full application initialization.",
        ],
    }
    image_manifest = Path("/opt/aks-node-boot-trace/image.txt")
    if image_manifest.is_file():
        metadata["image_provenance"] = dict(
            line.split("=", 1) for line in image_manifest.read_text().splitlines() if "=" in line
        )
    else:
        metadata["warnings"].append("No baked image provenance manifest is available.")
    metadata["warnings"].append(
        "Type=notify service ordering delays selected boot services until the recorder is ready. "
        "AgentBaker E2E also opens a Bastion/SSH connection during boot. "
        "No uninstrumented control is measured by this trial."
    )
    save(destination / "metadata.json", metadata)
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    selector = selectors.DefaultSelector()
    trace_fd = None
    output = None
    session_created = False
    byte_limit_reached = False
    failed = False
    try:
        if args.backend == "ftrace":
            if not (TRACEFS / "tracing_on").exists():
                raise RuntimeError("tracefs is not mounted at /sys/kernel/tracing")
            if (TRACEFS / "tracing_on").read_text().strip() != "1":
                raise RuntimeError("ftrace must already be enabled by boot configuration")
            trace_clock = (TRACEFS / "trace_clock").read_text()
            if "[mono]" not in trace_clock:
                raise RuntimeError("ftrace must use trace_clock=mono")
            metadata["ftrace_clock"] = trace_clock.strip()
            metadata["enabled_events"] = (TRACEFS / "set_event").read_text().splitlines()
            if not any("sched_switch" in event for event in metadata["enabled_events"]):
                raise RuntimeError("sched_switch was not enabled before this boot")
            if (TRACEFS / "current_tracer").read_text().strip() != "nop":
                raise RuntimeError("This experiment expects event tracing with current_tracer=nop")
            trace_fd = os.open(TRACEFS / "trace_pipe", os.O_RDONLY | os.O_NONBLOCK)
            selector.register(trace_fd, selectors.EVENT_READ)
            output = (destination / "ftrace.txt").open("w+b")
        else:
            if (
                (TRACEFS / "tracing_on").exists()
                and (TRACEFS / "tracing_on").read_text().strip() == "1"
                and (
                    (TRACEFS / "set_event").read_text().strip()
                    or (TRACEFS / "current_tracer").read_text().strip() != "nop"
                )
            ):
                raise RuntimeError("Disable boot ftrace before running the LTTng-only trial")
            # Only one private session; never stop or destroy unrelated sessions.
            session = "aks-boot-" + boot_id
            metadata["session_name"] = session
            save(destination / "metadata.json", metadata)
            run("lttng", "create", session, "--output=" + str(destination / "ctf"))
            session_created = True
            run(
                "lttng", "enable-channel", "--kernel", "--session", session,
                "--subbuf-size=256K", "--num-subbuf=4", "--switch-timer=100000",
                "--tracefile-size=16M", "boot",
            )
            # Detect rollover separately; never treat a rotated archive as complete.
            run(
                "lttng", "enable-event", "--kernel", "--session", session,
                "--channel=boot", "sched_switch,sched_wakeup,sched_wakeup_new,"
                "sched_process_fork,sched_process_exit,sched_process_exec,"
                "block_rq_issue,block_rq_complete",
            )
            run(
                "lttng", "add-context", "--kernel", "--session", session,
                "--channel=boot", "--type=pid", "--type=tid", "--type=procname",
            )
            run("lttng", "start", session)
            metadata["warnings"].append(
                "LTTng starts after session setup; early kernel events are not covered. "
                "The size watchdog is sampled and may overshoot while buffers flush. "
                "Use a bounded dedicated filesystem for a hard disk-space cap."
            )
        metadata["recorder_active_monotonic"] = time.monotonic()
        save(destination / "metadata.json", metadata)
        notify = os.environ.get("NOTIFY_SOCKET")
        if notify:
            address = "\0" + notify[1:] if notify.startswith("@") else notify
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.connect(address)
                channel.sendall(b"READY=1")
        deadline = time.monotonic() + args.max_seconds
        limit = args.max_mib * 1024 * 1024
        while not stopping:
            if time.monotonic() >= deadline:
                metadata["warnings"].append("Capture stopped by duration watchdog.")
                break
            if disk_size(destination) >= limit:
                metadata["warnings"].append("Capture stopped by size watchdog.")
                break
            if trace_fd is None:
                time.sleep(0.2)
            else:
                for key, _ in selector.select(timeout=0.2):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    remaining = limit - output.tell()
                    if len(chunk) > remaining:
                        chunk = chunk[:max(0, remaining)]
                        metadata["warnings"].append("ftrace byte limit reached.")
                        byte_limit_reached = True
                        stopping = True
                    output.write(chunk)
        metadata["status"] = "stopped"
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        failed = True
        metadata["status"] = "failed"
        metadata["error"] = str(error)
        raise
    finally:
        metadata["clock_end"] = clock_sample()
        save(destination / "metadata.json", metadata)
        try:
            if args.backend == "ftrace" and trace_fd is not None:
                (TRACEFS / "tracing_on").write_text("0")
                # Drain events queued before tracing was disabled, subject to the limit.
                while not byte_limit_reached and output.tell() < args.max_mib * 1024 * 1024:
                    try:
                        chunk = os.read(trace_fd, min(
                            65536, args.max_mib * 1024 * 1024 - output.tell(),
                        ))
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    output.write(chunk)
                if output.tell() >= args.max_mib * 1024 * 1024:
                    metadata["warnings"].append("Final ftrace drain reached the byte limit; tail may be truncated.")
                if trim_partial_line(output):
                    metadata["warnings"].append("Incomplete final ftrace record was removed.")
                stats = {
                    str(path.relative_to(TRACEFS)): path.read_text()
                    for path in TRACEFS.glob("per_cpu/cpu*/stats")
                }
                save(destination / "ftrace-stats.json", stats)
            elif session_created:
                run("lttng", "stop", session)
                metadata["lttng_status"] = run("lttng", "list", session)
                run("lttng", "destroy", session)
            if not failed:
                save(destination / "services.json", systemd_services(metadata["warnings"]))
                if args.include_journal:
                    journal = run(
                        "journalctl", "--boot=0", "--no-pager", "--output=json",
                        "--lines=10000", *("--unit=" + unit for unit in UNITS),
                    )
                    (destination / "journal.jsonl").write_text(journal)
            metadata["finalized_at"] = datetime.datetime.now(
                datetime.timezone.utc,
            ).isoformat()
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
            metadata["status"] = "failed"
            metadata["error"] = "Finalization failed: " + str(error)
            raise
        finally:
            selector.close()
            if trace_fd is not None:
                os.close(trace_fd)
            if output is not None:
                output.close()
            save(destination / "metadata.json", metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("ftrace", "lttng"), required=True)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--max-mib", type=int, default=256)
    parser.add_argument("--image-label", required=True)
    parser.add_argument("--include-journal", action="store_true")
    args = parser.parse_args()
    try:
        record(args)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Capture failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
