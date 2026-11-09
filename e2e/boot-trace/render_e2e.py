"""Render collected AgentBaker bundles, without contacting Azure or Kubernetes."""

import argparse
import json
from pathlib import Path
import sys

from milestones import import_cse
from report import write_html
from timeline import analyze, number, utc_seconds


def select_trials(root):
    trials = {}
    identities = set()
    configurations = []
    for marker in sorted(root.glob("**/boot-trace/result.json")):
        if json.loads(marker.read_text()).get("status") != "collected":
            raise ValueError(f"Incomplete trial: {marker.parent}")
        bundle = marker.parent
        metadata = json.loads((bundle / "metadata.json").read_text())
        readiness = json.loads((bundle / "readiness.json").read_text())
        backend = metadata["backend"]
        if backend not in ("ftrace", "lttng") or backend in trials:
            raise ValueError("Expected exactly one successful trial per backend; retries must stay disabled")
        if metadata["boot_id"] != readiness["boot_id"] or metadata["status"] != "stopped" or not metadata.get("finalized_at"):
            raise ValueError("Capture identity or finalization does not match readiness")
        for field in ("boot_id", "node_uid", "provider_id"):
            identity = (field, readiness[field].lower())
            if identity in identities:
                raise ValueError(f"The two trials must use distinct new nodes: duplicate {field}")
            identities.add(identity)
        provenance = metadata["image_provenance"]
        if provenance["backend"] != backend or provenance["kernel"] != metadata["kernel"]:
            raise ValueError("Baked provenance does not match the captured kernel/backend")
        configuration = readiness["configuration"]
        required = ("vm_size", "os_sku", "kubernetes_version", "network_plugin",
                    "canonical_base_version", "cluster_resource_id", "provisioning")
        if any(not configuration.get(key) for key in required):
            raise ValueError("Missing required comparison configuration")
        if provenance["base_version"] != configuration["canonical_base_version"]:
            raise ValueError("Canonical base provenance does not match readiness configuration")
        configurations.append((
            metadata["kernel"], metadata["architecture"],
            *(configuration[key] for key in required),
            configuration.get("network_policy"), configuration.get("network_dataplane"),
        ))
        trials[backend] = bundle
    if set(trials) != {"ftrace", "lttng"}:
        raise ValueError("Both ftrace and LTTng bundles are required")
    if configurations[0] != configurations[1]:
        raise ValueError("Trials differ in base image, kernel, architecture, VM, cluster or provisioning configuration")
    return trials


def render(root, output):
    trials = select_trials(root)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    results = []
    for backend in ("ftrace", "lttng"):
        bundle = trials[backend]
        result = analyze(bundle)
        datapoints = bundle / "cse-boot-datapoints.json"
        if datapoints.is_file():
            points = json.loads(datapoints.read_text())
            if points.get("BootDatapoints"):
                # Reuse the same importer as the standalone CLI, without saving CSE internals.
                milestones = import_cse(points)
                clock = result["metadata"]["clock_start"]
                origin = number(clock["realtime"]) - number(clock["monotonic"])
                for point in milestones:
                    t = utc_seconds(point["timestamp"]) - origin
                    if t < 0:
                        result["warnings"].append(
                            f'{point["name"]} maps {t:.6f}s before boot and was excluded, '
                            "not clamped. Check CSE timestamp resolution and clock mapping."
                        )
                        continue
                    result["milestones"].append({
                        "name": point["name"], "t": t, "source": point["source"],
                    })
            else:
                result["warnings"].append("CSE boot datapoints were unavailable after provisioning.")
        else:
            result["warnings"].append("No sanitized CSE boot datapoints were collected.")
        capture_output = output / backend
        capture_output.mkdir(mode=0o700)
        (capture_output / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        write_html(capture_output / "report.html", [result], [backend])
        results.append(result)
    write_html(output / "comparison.html", results, ["ftrace", "LTTng"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        render(args.artifacts, args.output)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
