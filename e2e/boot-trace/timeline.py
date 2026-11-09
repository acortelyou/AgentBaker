"""Shared evidence model. Scheduler accounting is per thread, never per service."""

import collections
import datetime
import json
import math
import re

from events import ftrace_events, lttng_events


def utc_seconds(value):
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps must include an explicit timezone")
    return parsed.timestamp()


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite timestamp or duration")
    return result


def analyze(bundle, ctf_offset=None):
    metadata = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("schema") != 1:
        raise ValueError("Unsupported capture schema")
    diagnostics = list(metadata.get("warnings", []))
    if metadata.get("status") != "stopped" or "finalized_at" not in metadata:
        diagnostics.append("Guest capture was not successfully finalized; results are partial.")
    clock = metadata["clock_start"]
    realtime_origin = number(clock["realtime"]) - number(clock["monotonic"])
    uncertainty = number(clock["sample_span_seconds"])
    end_clock = metadata.get("clock_end")
    if end_clock:
        end_origin = number(end_clock["realtime"]) - number(end_clock["monotonic"])
        if abs(end_origin - realtime_origin) > max(0.05, uncertainty):
            diagnostics.append("Realtime/monotonic mapping changed during capture.")
    readiness_path = bundle / "readiness.json"
    readiness = json.loads(readiness_path.read_text()) if readiness_path.exists() else None
    ready = None
    if readiness:
        if readiness["boot_id"] != metadata["boot_id"]:
            raise ValueError("Kubernetes BootID does not match the captured boot")
        ready = utc_seconds(readiness["transition_time"]) - realtime_origin
        if ready < 0:
            raise ValueError("NodeReady precedes the mapped boot origin")
        diagnostics.append(
            "Ready uses the kubelet-reported LastTransitionTime, not an exact "
            "apiserver acceptance timestamp. Cross-host clock error is not measured."
        )
    else:
        diagnostics.append("No Kubernetes readiness evidence; boot-to-Ready is unknown.")
    backend = metadata["backend"]
    if backend == "ftrace":
        stream = ftrace_events(bundle / "ftrace.txt", diagnostics)
        stats_path = bundle / "ftrace-stats.json"
        if stats_path.exists():
            stats = json.loads(stats_path.read_text())
            for name, text in stats.items():
                if re.search(r"(?:overrun|dropped events|commit overrun):\s*[1-9]", text):
                    diagnostics.append(f"Ring buffer loss reported in {name}.")
    elif backend == "lttng":
        stream = lttng_events(bundle / "ctf", diagnostics, ctf_offset, realtime_origin)
    else:
        raise ValueError(f"Unknown backend: {backend}")

    threads = {}
    generations = collections.defaultdict(int)
    running = {}
    wakeups = {}
    lanes = collections.defaultdict(lambda: collections.defaultdict(float))
    counts = collections.Counter()
    first = last = None
    stream_last = None
    max_order_error = 0.0
    io_events = 0
    io_bins = collections.defaultdict(collections.Counter)
    discontinuities = 0

    def thread(tid, comm):
        key = (tid, generations[tid])
        if key not in threads:
            threads[key] = {
                "tid": tid, "generation": key[1], "comm": comm,
                "cpu_seconds": 0.0, "runnable_seconds": 0.0,
                "switches": 0, "first": last, "last": last,
            }
        row = threads[key]
        if comm:
            row["comm"] = comm
        row["last"] = last
        return key, row

    def span(key, start, end):
        if end <= start or key[0] == 0:
            return
        threads[key]["cpu_seconds"] += end - start
        # Fixed 100 ms bins bound HTML size without claiming event-level rendering.
        for bucket in range(int(start * 10), math.ceil(end * 10)):
            occupied = min(end, (bucket + 1) / 10) - max(start, bucket / 10)
            if occupied > 0:
                lanes[key][bucket] += occupied

    for event in stream:
        t = number(event["t"])
        if t < 0:
            raise ValueError("Negative boot timestamp; check the trace clock mapping")
        if stream_last is not None and t < stream_last:
            max_order_error = max(max_order_error, stream_last - t)
            continue
        stream_last = t
        if event.get("gap"):
            running.clear()
            wakeups.clear()
            discontinuities += 1
        if ready is not None and t > ready:
            continue
        counts[event["name"]] += 1
        first = t if first is None else first
        last = t
        name, data, cpu = event["name"], event["data"], event["cpu"]
        if name == "sched_switch":
            match = re.search(
                r"prev_comm=(.*?) prev_pid=(\d+).*?prev_state=(\S+).*?"
                r"next_comm=(.*?) next_pid=(\d+)", data
            )
            if not match:
                raise ValueError(f"Unrecognized sched_switch fields: {data}")
            prev_comm, prev, state, next_comm, nxt = match.groups()
            prev, nxt = int(prev), int(nxt)
            prev_key, prev_row = thread(prev, prev_comm)
            next_key, next_row = thread(nxt, next_comm)
            if cpu in running:
                key, started = running[cpu]
                if key != prev_key:
                    discontinuities += 1
                else:
                    span(key, started, t)
            # A switch-out proves this task was running, even if its switch-in
            # was outside coverage. Do not retain an older pending wakeup.
            wakeups.pop(prev_key, None)
            # Linux TASK_RUNNING is 0; textual ftrace uses R or R+.
            if state in ("0", "R", "R+"):
                wakeups.setdefault(prev_key, t)
            if next_key in wakeups:
                next_row["runnable_seconds"] += t - wakeups.pop(next_key)
            running[cpu] = (next_key, t)
            prev_row["switches"] += 1
        elif name in ("sched_wakeup", "sched_wakeup_new"):
            match = re.search(r"comm=(.*?) pid=(\d+)", data)
            if match:
                key, _ = thread(int(match[2]), match[1])
                if key not in (value[0] for value in running.values()):
                    wakeups.setdefault(key, t)
        elif name == "sched_process_exit":
            match = re.search(r"(?:^| )pid=(\d+)", data)
            if match:
                tid = int(match[1])
                key = (tid, generations[tid])
                # The final switch-out can follow sched_process_exit.
                wakeups.pop(key, None)
        elif name == "sched_process_fork":
            match = re.search(r"child_comm=(.*?) child_pid=(\d+)", data)
            if match:
                tid = int(match[2])
                if (tid, generations[tid]) in threads:
                    old_key = (tid, generations[tid])
                    wakeups.pop(old_key, None)
                    for active_cpu, (key, _started) in list(running.items()):
                        if key == old_key:
                            del running[active_cpu]
                            discontinuities += 1
                    generations[tid] += 1
                thread(tid, match[1])
        elif name.startswith("block_"):
            io_events += 1
            io_bins[int(t * 10)][name] += 1
    if max_order_error:
        raise ValueError(
            f"Trace is not chronologically merged (up to {max_order_error:.6f}s). "
            "Merge per-CPU streams before import."
        )
    if discontinuities:
        diagnostics.append(
            f"{discontinuities} scheduler discontinuities; affected intervals were not charged."
        )
    if first is None:
        raise ValueError("No decoded events in the boot-to-Ready interval")
    stop = min(ready, stream_last) if ready is not None else last
    for key, started in running.values():
        span(key, started, stop)
    for key, started in wakeups.items():
        if key[0] and key in threads:
            threads[key]["runnable_seconds"] += max(0.0, stop - started)
    if not counts["sched_switch"]:
        diagnostics.append("No scheduler switches; CPU and runnable time are unavailable.")
    if metadata.get("backend") == "lttng" and (
        first - metadata.get("recorder_active_monotonic", first) > 0.1
    ):
        diagnostics.append("LTTng trace prefix may be missing; compare stream bounds and recorder start.")
    if io_events:
        diagnostics.append(
            "Block events are counted, not attributed to a thread or converted to "
            "I/O wait. Request merging and asynchronous I/O prevent naive attribution."
        )
    else:
        diagnostics.append("No block I/O events decoded; I/O activity is unknown.")
    if ready is not None and stream_last < ready:
        diagnostics.append(
            "Last trace event precedes Ready; capture end metadata must also be "
            "checked to distinguish inactivity from missing coverage."
        )
    diagnostics.append(
        "CPU/runnable values cover observed scheduler intervals only. Sleeping "
        "time is not classified as disk or network wait. Per-thread sums can "
        "overlap across CPUs; they are not additive boot phases."
    )
    milestones, services = semantic_evidence(bundle, realtime_origin, ready)
    milestones.insert(0, {"name": "Linux boot (clock origin)", "t": 0.0, "source": "clock mapping"})
    if ready is not None:
        milestones.append({
            "name": "First observed Ready=True transition", "t": ready,
            "source": "Kubernetes NodeCondition.LastTransitionTime",
        })
    rows = sorted(
        (row for row in threads.values() if row["tid"]),
        key=lambda row: row["cpu_seconds"], reverse=True,
    )
    for row in rows:
        key = (row["tid"], row["generation"])
        row["cpu_bins"] = [[bucket / 10, value] for bucket, value in sorted(lanes[key].items())]
        row["observed_span_seconds"] = row["last"] - row["first"]
        if not counts["sched_switch"]:
            row["cpu_seconds"] = row["runnable_seconds"] = None
    return {
        "schema": 1, "metadata": metadata, "readiness": readiness,
        "ready_seconds": ready, "trace_first": first, "trace_last": stream_last,
        "clock_sample_uncertainty_seconds": uncertainty,
        "event_counts": dict(counts), "block_event_count": io_events,
        "io_bins": [[bucket / 10, dict(value)] for bucket, value in sorted(io_bins.items())],
        "threads": rows, "services": services, "milestones": milestones,
        "warnings": list(dict.fromkeys(diagnostics)),
    }


def semantic_evidence(bundle, origin, ready):
    milestones, services = [], []
    path = bundle / "services.json"
    if path.exists():
        for unit in json.loads(path.read_text()):
            start = number(unit["start_monotonic_us"]) / 1e6
            active = number(unit["active_monotonic_us"]) / 1e6
            if start > 0 and active >= start and (ready is None or start <= ready):
                services.append({
                    "name": unit["name"], "start": start, "end": active,
                    "source": "systemctl show (most recent activation, not full initialization)",
                })
    path = bundle / "milestones.json"
    if path.exists():
        for item in json.loads(path.read_text()):
            if not item.get("source"):
                raise ValueError("An imported milestone needs its source/provenance")
            t = number(item["boot_seconds"]) if "boot_seconds" in item else (
                utc_seconds(item["timestamp"]) - origin
            )
            if t < 0:
                raise ValueError("Milestone precedes this boot")
            milestones.append({"name": item["name"], "t": t, "source": item["source"]})
    return milestones, services
