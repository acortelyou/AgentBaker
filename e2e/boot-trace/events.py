"""Adapters for textual ftrace and LTTng CTF. All event times are boot seconds."""

import re


LINE = re.compile(
    r"^\s*(?P<comm>.+)-(?P<pid>\d+)\s+(?:\(\s*[^)]*\)\s+)?"
    r"\[(?P<cpu>\d+)\]\s+(?:\S+\s+)?"
    r"(?P<time>\d+\.\d+):\s+(?P<name>[\w:]+):\s*(?P<data>.*)$"
)


def ftrace_events(path, diagnostics):
    """Read tracefs text, not the trace-cmd binary container."""
    unmatched = 0
    gap = False
    with path.open(encoding="utf-8", errors="strict") as source:
        for line in source:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "LOST" in line or "CPU:" in line and "EVENTS" in line:
                diagnostics.append("ftrace reports lost events: " + line.strip())
                gap = True
                continue
            match = LINE.match(line)
            if not match:
                unmatched += 1
                gap = True
                continue
            value = match.groupdict()
            yield {
                "t": float(value["time"]),
                "cpu": int(value["cpu"]),
                "name": value["name"].rsplit(":", 1)[-1],
                "comm": value["comm"].strip(),
                "pid": int(value["pid"]),
                "data": value["data"],
                "gap": gap,
            }
            gap = False
    if unmatched:
        diagnostics.append(f"{unmatched} non-header ftrace lines could not be decoded.")


def lttng_events(path, diagnostics, offset=None, realtime_origin=0.0):
    """Use distro-provided Babeltrace 2 bindings; importing reports needs no bt2."""
    try:
        import bt2
    except ImportError as error:
        raise RuntimeError(
            "LTTng import needs Babeltrace 2 Python bindings (python3-bt2)."
        ) from error

    def fields(field):
        if field is None:
            return {}
        numeric = {"pid", "tid", "vtid", "cpu_id", "prev_tid", "next_tid",
                   "parent_tid", "child_tid", "prev_state"}
        return {
            str(key): str(int(field[key])) if key in numeric else str(field[key])
            for key in field
        }

    first = True
    gap = False
    for message in bt2.TraceCollectionMessageIterator(str(path)):
        if isinstance(message, (bt2._DiscardedEventsMessageConst, bt2._DiscardedPacketsMessageConst)):
            diagnostics.append("LTTng reports discarded events or packets.")
            gap = True
        if not isinstance(message, bt2._EventMessageConst):
            continue
        if first:
            clock = message.default_clock_snapshot.clock_class
            if not clock.origin_is_unix_epoch and offset is None:
                raise ValueError(
                    "CTF clock has no Unix origin; supply --ctf-offset-seconds "
                    "(boot seconds minus CTF seconds)."
                )
            diagnostics.append(
                "CTF time converted to boot time using the supplied clock mapping; "
                "wall-clock changes during capture require additional review."
            )
            first = False
        mapping = offset if offset is not None else -realtime_origin
        event = message.event
        data = fields(event.payload_field)
        context = fields(event.common_context_field)
        packet = fields(event.packet.context_field) if event.packet else {}
        name = event.name.rsplit(":", 1)[-1]
        if name == "sched_switch":
            payload = (
                f"prev_comm={data['prev_comm']} prev_pid={data['prev_tid']} "
                f"prev_state={data['prev_state']} ==> "
                f"next_comm={data['next_comm']} next_pid={data['next_tid']}"
            )
        elif name in ("sched_waking", "sched_wakeup", "sched_wakeup_new"):
            payload = f"comm={data['comm']} pid={data['tid']}"
        elif name == "sched_process_exit":
            payload = f"comm={data['comm']} pid={data['tid']}"
        elif name == "sched_process_fork":
            payload = (
                f"comm={data['parent_comm']} pid={data['parent_tid']} "
                f"child_comm={data['child_comm']} child_pid={data['child_tid']}"
            )
        else:
            payload = " ".join(f"{key}={value}" for key, value in data.items())
        if name.startswith("sched_") and "cpu_id" not in packet:
            raise ValueError("CTF scheduler event has no packet cpu_id; cannot account CPU time")
        yield {
            "t": message.default_clock_snapshot.ns_from_origin / 1e9 + mapping,
            "cpu": int(packet.get("cpu_id", -1)),
            "name": name,
            "comm": context.get("procname", ""),
            "pid": int(context.get("vtid", context.get("tid", 0))),
            "data": payload,
            "gap": gap,
        }
        gap = False
