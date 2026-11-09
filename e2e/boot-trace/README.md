# AKS node boot observatory

Experimental first-boot tracing for a **dedicated AKS Linux VMSS test pool**:
find work worth optimizing between Linux boot and NodeReady, including work
running in parallel. Capture with **ftrace or LTTng on separate new nodes**, then
produce the same standalone, interactive HTML report.

**Status: local checks passed; VHD builds and guest runtime remain unvalidated.**
The AgentBaker VHD/E2E integration has passed the offline Python tests, targeted
Go tests, ShellSpec checks and repository shell lint. The systemd unit was
checked statically and the pipeline YAML parsed locally, not expanded by Azure
DevOps. No VHD build has been launched. Public Ubuntu
package metadata and package contents were inspected without guest installation.
Kernel compatibility at runtime, DKMS loading, Babeltrace decoding, boot ordering,
browser rendering and end-to-end collection remain unvalidated. There are no
measured results yet.

## AgentBaker integration workspace

The integration target is **Ubuntu 26.04 Minimal Gen2 x64**. A copy of this
experiment lives in `e2e/boot-trace/` on the AgentBaker experimental branch
[`thicohen/trace-node-boot-time`](https://github.com/Azure/AgentBaker/tree/thicohen/trace-node-boot-time).

Local Orca worktree:
`/home/thicohen/orca/workspaces/AgentBaker/trace-node-boot-time`.

The original `node-sig` code is intentionally retained until the AgentBaker PR
is opened. The copies are not automatically synchronized. No PR is open;
publishing this experimental branch does not trigger the manual VHD pipeline.

### Prepared VHD and E2E path

The existing manual VHD pipeline `.pipelines/.vsts-vhd-builder-release.yaml`
provides two instrumented Ubuntu 26.04 Minimal Gen2 x64 builds:
`2604minimalgen2containerd-boot-trace-ftrace` and
`2604minimalgen2containerd-boot-trace-lttng`. They never replace the normal
`2604minimalgen2containerd` image. Publishing-info generation, production
publication and prefetch optimization are disabled for this experimental path;
the pipeline still creates billable build VMs and separate versions in its
normal staging gallery when explicitly run. Standard VHD build/content checks
are retained. The tracing and standard-reference jobs inherit the pipeline's
normal cleanup settings. When enabled, the existing cleanup also purges eligible
image versions older than seven days across the shared staging gallery; it is
not limited to this experiment. The standard build helper may
recreate a failed staging gallery; the content test also deletes its temporary
resource group unless debugging is enabled.

On this experimental branch, only `build2604minimalgen2containerdBootTraceFtrace`,
`build2604minimalgen2containerdBootTraceLttng`, and the uninstrumented
`build2604minimalgen2containerd` reference are enabled by default. All other
image builds default to disabled. Select the AgentBaker branch in the existing
pipeline run form to load these defaults. `useOverrides=false` does not
require an aks-rp branch. For tracing E2E and the comparison report, enable
both builds and `runBootTraceE2E`, and ensure `SKIP_E2E_TESTS` is not `true`.
This selects the tracing E2E stage instead of the regular Linux E2E stage.
Without that option, the regular E2E stage still follows its normal setting;
set `SKIP_E2E_TESTS=true` if only building images.

The shared `bootTraceCanonicalBaseVersion` defaults to `26.04.202610280`, with URN
`Canonical:ubuntu-26_04-lts:minimal:26.04.202610280`. Azure Marketplace metadata
was checked on 2026-11-06: the image is active, x64 and Hyper-V V2 in `eastus`
and `eastus2`. Recheck availability in the actual `PACKER_BUILD_LOCATION`
before launching; its value comes from the pipeline environment. This metadata
check does not verify the guest kernel or tracing runtime.
Any override must also be an exact version, not `latest`.
All three jobs use it. The standard reference retains the normal Packer template,
with publishing-info generation and prefetch optimization disabled like the
tracing images. It is built for a later uninstrumented timing comparison;
the tracing E2E and automatic report still cover only ftrace and LTTng.
Template generation for the tracing images fails if Packer settings silently
override it, or if production naming/publishing or another OS/architecture is
selected. Normal builds retain their original Packer template.
`prepare_vhd.py` derives the instrumented template instead of duplicating it.

The image helper deliberately supports only Ubuntu 26.04 x86_64, not other
AgentBaker operating systems. It prepares tools after the last builder reboot,
before the package inventory. It only **enables**, never starts, the recorder
after final cleanup. ftrace kernel parameters are added through a GRUB drop-in
then, preserving Ubuntu's initrdless boot path. The main initramfs is not modified:
Ubuntu can skip it and load only the early microcode archive.
The helper refuses a changed kernel or an existing builder capture.
No bootconfig utility or kernel source download is required.

| Component | Prepared version/source |
| --- | --- |
| Azure kernel | `7.0.0-1020-azure`, package `7.0.0-1020.21` |
| LTTng tools | Ubuntu `2.14.1-1build1` |
| LTTng DKMS | Ubuntu `2.15.0-1ubuntu1.1`, compiled during the VHD build for the running pinned kernel |
| Report-job Babeltrace bindings | Ubuntu 26.04 `python3-bt2=2.1.2-1build2` |

These are intentional experiment pins, not proof of runtime compatibility.
Recheck them together before changing the kernel. Missing pinned packages fail
the build rather than falling back to another version. The LTTng package's
session daemon is disabled on the builder, then required by the early recorder
with default boot dependencies removed. Its distribution environment override
is cleared so it cannot silently disable kernel tracing.

`runBootTraceE2E` defaults to **false**. If explicitly enabled in a future pipeline run,
the E2E stage selects `Ubuntu2604Minimal_BootTrace_ftrace` and
`Ubuntu2604Minimal_BootTrace_lttng`, with concurrency one and scenario retries
disabled. Separate scenarios are necessary because each requires its own
instrumented image and fresh VM. They are skipped in ordinary suites unless
`E2E_BOOT_TRACE=true` and exact VHD metadata are provided; they are excluded
from automatic NBC sampling and do not support PIS/VHD caching.

The framework opens its Kubernetes list/watch **before** creating the VMSS.
It retains the first Ready event even if ProviderID arrives later, rejects watch
gaps/expired versions, old node UIDs, observed reboots and identity mismatches,
and disables both VMSS recreation retry loops for these trials.
It opens the existing Bastion/SSH transport as soon as the new VM/IP is
available, without waiting for ARM/CSE completion. Once Ready and SSH are both
available, it stops the owned recorder and streams a bounded tar archive
**before workload validation**. Archive extraction rejects traversal, links,
duplicates, unexpected files and excessive sizes/counts. Captures that ended
before Ready fail; incomplete archives are retained as diagnostics, not accepted
measurements. Never-Ready failures retain normal E2E diagnostics, not a validated
boot-to-Ready bundle; the guest watchdog remains the safety stop.

Successful bundles are under each scenario's `boot-trace/` artifact directory.
They include exact image provenance, VM/cluster/network settings, Ready evidence
and only the four recognized CSE boot datapoints, never full CSE output.
The report stage uses an Ubuntu 26.04 container, selects exactly one successful
bundle per backend, rejects mismatched base/kernel/VM/cluster settings and reused
node identities, and writes the autonomous `boot-trace-report/comparison.html`
plus each backend's HTML/JSON. It is not a statistical performance verdict.
Second-resolution CSE timestamps can map slightly before the precise boot
origin: such milestones remain in the raw sanitized export but are excluded
from the rendered timeline with a warning, never silently clamped to zero.
The container job and its dependency installation are recipes only, not run here.

For downloaded E2E artifacts, the same offline report recipe is:

```bash
python3 -B render_e2e.py /private/path/to/downloaded-artifacts --output reports/e2e-comparison
```

The instructions and commands in this document are **not execution history**.
VHD builds and Azure E2E execution still require separate authorization and an
exact Canonical base image version.

## What is measured

| Evidence | Output | Important limit |
| --- | --- | --- |
| Node Ready condition + guest clock samples | Linux boot to first observed Ready=True transition | Kubelet's `lastTransitionTime`, not exact API server acceptance |
| `sched_switch` | Observed CPU seconds per thread | Missing prefixes, lost events and discontinuities limit accounting |
| Wakeups and preemptions | Observed runnable wait per thread | Not all off-CPU time, disk wait or network wait |
| Forks and scheduler identities | TID generations, last observed thread name, observed span | Not guaranteed lifetime, process ancestry, or service membership |
| Block issue/complete events | Counts and a time-binned activity lane | **No request latency or per-thread I/O attribution yet** |
| `systemctl show` | Selected service activation intervals | Most recent activation; not full initialization or the Ready critical path |
| Exported CSE boot datapoints | Kernel, Guest Agent, CSE and kubelet milestones | Optional local import, not automatic extraction of CSE internals |

Elapsed time, CPU time, runnable wait and service activation are different
measurements. Never sum parallel work into a boot duration. An expensive thread
is an optimization candidate, not proof it delays Ready. Unclassified off-CPU
time is not relabeled as I/O.

Existing RP code already reports broad CSE boot datapoints and readiness
latency. This experiment adds scheduler-level evidence, coverage information,
an inspectable timeline and before/after comparison. It does not modify aks-rp.
The existing boot-data parser unit test is not a first-boot performance benchmark.

## Files and prerequisites

- `trace_boot.py`: local CLI; `orchestrate.py`: explicit scale-out/watch/SSH.
- `guest/`: image-side recorder, systemd unit, safety stop and ftrace GRUB drop-in.
- `events.py`: tracefs-text and CTF adapters; `timeline.py`: shared accounting.
- `milestones.py`: CSE datapoint import; `report.py`: embedded HTML/CSS/JavaScript.
- `prepare_vhd.py`, `guest/install.sh`: opt-in Packer image integration.
- `render_e2e.py`: offline E2E artifact selection and comparison.
- `tests/`: synthetic offline `unittest` cases; local checks passed.
- `../scenario/boot_trace*.go`: E2E watch, Bastion collection and focused Go tests.
- `../../spec/e2e/boot_trace_install_spec.sh`: image-guard ShellSpec tests.

Local requirements: Python **3.10+**; for collection, existing authenticated
`az`, `kubectl` and OpenSSH clients, an explicitly selected kubeconfig/context,
Azure pool-scale and VMSS-read permissions, and Kubernetes list/watch nodes.
No packages are installed by the tool. Rendering ftrace requires only the
Python standard library. Rendering LTTng additionally requires the distro's
Babeltrace 2 Python bindings (`python3-bt2`) in the selected interpreter.

The guest needs Python 3.10+, systemd, writable local storage, root permissions
and either tracefs with ftrace boot parameters or compatible LTTng kernel modules and
tools. LTTng modules must match the kernel and any Secure Boot signing policy.
All configured events must exist on that image. No full-syscall or function-graph
capture is enabled.

The **standalone pool collector** uses SSH to the new node's InternalIP;
AgentBaker E2E uses the framework's existing Bastion transport instead. Establish an
authorized route, trusted host keys or host certificates, authentication and
noninteractive sudo for the service stop and capture tar commands beforehand.
Strict host-key verification is required; the tool never auto-trusts a new key.
Use `--ssh-config` for existing bastion/proxy settings. No SSH installation,
credential distribution or alternate AKS transport is implemented.

## Preparing first-boot capture

The following is the standalone collector's image-integration contract.
The AgentBaker-specific build recipe is described above.
First establish a supported, authorized test-image deployment path. An ordinary
AKS pool is not assumed to accept arbitrary custom images. Do not prepare a
production pool, modify its VMSS directly, or replace first boot with a reboot.

In that image-build process, arrange the following before the measured VM exists:

| Source | Destination in the test image |
| --- | --- |
| `guest/capture.py` and `guest/stop_capture.py` | `/opt/aks-node-boot-trace/` (root-owned) |
| `guest/aks-node-boot-trace.service` | `/etc/systemd/system/aks-node-boot-trace.service`, enabled for `sysinit.target` |
| Adapted `guest/config.env.example` | `/etc/aks-node-boot-trace.env`, root-owned mode 0600 |

Set `BACKEND`, the actual `IMAGE_LABEL`, `MAX_SECONDS` (1..600) and `MAX_MIB`
(at least 16). Do not bake credentials or earlier build/boot captures into the
image. Do not start the recorder on the image builder. Remove only experiment
artifacts from the image before sealing; retain required OS state.

For **ftrace**, install `guest/ftrace-grub.cfg` as
`/etc/default/grub.d/99-aks-boot-trace.cfg` and run `update-grub` before sealing
the image. It sets `trace_clock=mono`, `trace_buf_size=4M` and the selected
`trace_event` list directly on the kernel command line, without requiring the
main initramfs. Ensure tracefs is mounted. Simply copying the file through
cloud-init does not enable early-boot tracing. The recorder requires an already
active `mono` event trace with `current_tracer=nop`; it consumes `trace_pipe`
without resetting the boot buffer. Reserve 4 MiB per CPU for that buffer and
verify that no other consumer owns the root trace instance.

For **LTTng**, do not apply the ftrace boot parameters. Prepare kernel modules,
sessiond and consumer support without an existing measurement session. The
recorder creates one private `aks-boot-<boot-id>` session and a targeted kernel
channel. Its first event necessarily follows session setup: it cannot observe
the early kernel prefix. Raw CTF remains available for Trace Compass.

The unit orders recorder activation before cloud-init, the Azure agent,
containerd and kubelet. `Type=notify` waits for recorder setup, so this
**perturbs boot ordering and adds setup overhead**. It is not a production
configuration or an overhead-free measurement. Check the target image for
systemd ordering cycles and compare with an uninstrumented baseline.

The local watchdog samples duration and disk usage. systemd adds a 12-minute
runtime limit and a safety stop for the owned trace. Size limits are **not a hard
filesystem quota**: LTTng buffers, metadata, journal output and final flushing
can overshoot. Use a dedicated bounded filesystem when a hard cap is required.
Early ftrace buffers also consume RAM before the recorder starts. Validate the
failure/stop paths on the chosen image before any measurement campaign.

## Standalone pool collection CLI (not the AgentBaker E2E path)

Examples below are documentation only; they were **not run**.

Run from this directory. A read-only preflight queries Azure and Kubernetes,
prints the proposed +1 operation, and does not create an output directory:

```bash
python3 -B trace_boot.py collect \
  --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
  --cluster "$CLUSTER" --pool "$DEDICATED_TEST_POOL" \
  --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
  --ssh-user azureuser --backend ftrace --output captures/ftrace-01
```

Only after image/transport feasibility has been verified, the **same command
with `--execute --instrumentation-confirmed` creates one billable node**.
That confirmation is the operator's assertion, not remote proof of preparation.
The pool must already have a registered node, use Linux VMSS, have no autoscaler
and not use deallocated-instance reuse. Do not run concurrent pool operations.
The tool checks recorded counts and provisioning state but does not acquire
a cluster-wide lock.

The collector starts a node watch from the baseline resource version before
requesting the AKS scale-out. It distinguishes new Node UIDs and VMSS instance
IDs, records the first observed Ready condition and local observation time,
then stops and fetches that boot's capture. Fetching after Ready is expected:
analysis clips accounting to the mapped transition. The service may already
have stopped itself through its watchdog.

Watch disconnects/expiration, multiple new nodes, deletion, timeouts, missing
host trust and incomplete guest finalization fail explicitly. **Automatic
watch reconnection and collection from never-Ready nodes are not implemented.**
When capture fails after scale-out, inspect `result.json`, `watch.stderr` and
`ssh.stderr`; the node may still exist and the guest watchdog remains responsible
for stopping recording. An incomplete run is not an end-to-end success.

The 900-second default collector timeout can exceed the guest's 600-second
capture window. Choose image-side limits deliberately; longer collector waits
cannot recover events after the recorder stops. `--max-mib` on the collector
bounds archive transfer and extraction independently (default 512 MiB).

For LTTng, prepare the LTTng-only image/pool, use `--backend lttng`, and create
another fresh node with equivalent configuration. Do not run both backends
together or silently reboot the ftrace node.

## Offline reports and comparison

```bash
python3 -B trace_boot.py report captures/ftrace-01 --output reports/ftrace-01
python3 -B trace_boot.py report captures/lttng-01 --output reports/lttng-01
python3 -B trace_boot.py compare \
  reports/ftrace-01/report.json reports/ftrace-02/report.json \
  --output reports/before-after
```

Open `report.html` or `comparison.html` locally. There are no external scripts,
CDNs, telemetry uploads or browser network calls. Reports contain filters,
time-window zoom, click details, sortable thread accounting, semantic evidence,
warnings, and configuration-aware duration/CPU/runnable deltas. HTML CPU and
block activity lanes use **100 ms bins**, not full event resolution. Accounting
uses event timestamps; the raw traces retain deeper evidence. The view limits
displayed threads, not the normalized JSON accounting.

CTF with a Unix-origin clock is mapped through the paired guest clock sample.
Other CTF clocks require an explicitly established
`--ctf-offset-seconds` (boot seconds minus CTF seconds). Never guess an offset.
Text ftrace must be chronologically merged across CPUs; `trace.dat` is not
accepted. Use the original capture rather than assuming a text conversion has
the correct clock/fields.

Compare repeated trials with the same image, kernel, VM SKU, network, workload
and instrumentation. The HTML highlights recorded configuration differences,
but cannot prove comparability or statistical significance. Thread-name
comparison groups the last observed `comm` values, which can be truncated and
shared across processes; it is not stable process or service identity.

### Optional existing CSE milestones

Export just the existing `BootDatapoints` object locally, without full CSE output
or credentials. Upper/lower initial JSON field names are accepted:

```json
{
  "BootDatapoints": {
    "KernelStartTime": "2026-11-06T17:00:00Z",
    "GuestAgentStartTime": "2026-11-06T17:00:03Z",
    "CSEStartTime": "2026-11-06T17:00:08Z",
    "KubeletStartTime": "2026-11-06T17:00:23Z"
  }
}
```

```bash
python3 -B trace_boot.py import-cse /private/path/boot-datapoints.json \
  --output captures/ftrace-01/milestones.json
```

ISO-8601 with an explicit offset and existing UTC/GMT systemd/UnixDate layouts
are accepted. Ambiguous zone abbreviations fail unless the source's actual
offset is explicitly supplied, for example `--timezone-offset=-08:00`.
No missing datapoint is inferred. `ExecDuration` and `SystemdSummary` are not
expanded into invented internal CSE phases.

Alternatively add `milestones.json`, a list of objects with `name`, `source`
and either `boot_seconds` or a timezone-qualified `timestamp`. Such milestones
must come from this same boot; provenance is mandatory, but external milestone
boot identity is the operator's responsibility.

## Bundle and interpretation limits

```text
capture/
  metadata.json        # boot/kernel/backend, paired clocks, status, warnings
  readiness.json       # Node UID/providerID/bootID, transition and observation
  services.json        # selected systemd activation properties
  ftrace.txt           # ftrace backend only
  ftrace-stats.json    # ftrace per-CPU buffer diagnostics
  ctf/                # LTTng backend only
  milestones.json      # optional imported semantic evidence
  operation.json       # standalone pool collector's requested operation only
  cse-boot-datapoints.json # sanitized CSE timestamps from E2E, when available
  result.json          # collected or explicitly incomplete
```

Missing Ready evidence stays unknown. Boot-ID mismatches and invalid clock
origins are rejected. Clock drift, trace loss, scheduler discontinuities and
missing coverage are surfaced. Clock samples bound only local sampling skew;
they do not quantify cross-host clock error or a change that occurred before
sampling. No CPU work before the first known switch is invented, and accounting
is not extrapolated beyond the last event to fill a missing tail.

Systemd data can reflect a restart rather than the first activation. Journal
export is opt-in through the guest recorder's `--include-journal` option and is
not enabled by the provided unit; journal lines are not automatically parsed
into semantic phases. Raw process names, paths, optional journals and reports
may contain sensitive operational information.

## Safety, cost and cleanup

The standalone pool collector does not install dependencies, build images, change pool image
settings, scale down, delete resources or roll back a requested scale-out.
Capture files and local outputs use restrictive permissions. Tar extraction
rejects traversal, links and special files, and enforces a byte limit.
Keep captures, HTML, JSON, CTF, SSH settings, kubeconfigs and credentials private;
generated `captures/` and `reports/` directories are ignored.

**Every executed standalone pool collection leaves a billable node allocated.** After reviewing
the run and checking workloads/concurrent operations, explicitly retire the
dedicated test pool/node using your authorized AKS procedure. Scaling a shared
pool down does not guarantee deletion of the measured node; this tool does not
choose that action for you. Stop an owned recorder before removing its files.
Remove only identified experiment outputs and discard the instrumented image
when finished; do not recursively clean shared directories or tracing sessions.
AgentBaker E2E instead retains the framework's existing cleanup of its own VMSS
and tunnel (subject to the framework's keep-resources setting). It does not
delete the shared AKS cluster, Bastion, unrelated nodes or system pools.

## Local validation and pending runtime checks

Synthetic tests cover scheduler accounting, parallel CPUs, PID reuse, coverage
gaps, clocks/boot IDs, CSE parsing, HTML escaping, unsafe archives and dry-run/
watch-before-scale behavior. From the repository root, the local checks are:

```bash
(cd e2e/boot-trace && python3 -B -m unittest discover -s tests)
(cd e2e && go test ./scenario -run '^TestBootTrace' -count=1)
shellspec --shell bash --format d \
  spec/e2e/boot_trace_install_spec.sh spec/e2e/e2e_runner_scripts_spec.sh
make validate-shell
systemd-analyze verify e2e/boot-trace/guest/aks-node-boot-trace.service
```

ShellSpec 0.28.1 is the version used by the repository's test container.
The checks above passed locally, along with existing VMSS provisioning and SSH
retry regression tests. This is not a VHD build or a validation against Azure.
Live validation is pending separately for ftrace and LTTng, including kernel
options, image integration, stop/watchdog behavior, Babeltrace field
compatibility, HTML interactions and instrumentation overhead against an
uninstrumented control.
The integration adds Python cases in `tests/test_integration.py`, Go cases named
`TestBootTrace*` in `e2e/scenario/boot_trace_test.go`, and ShellSpec cases in
`spec/e2e/boot_trace_install_spec.sh`.

References: [kernel boot tracing](https://docs.kernel.org/trace/boottime-trace.html),
[LTTng](https://lttng.org/docs/v2.14/),
[systemd-analyze](https://www.freedesktop.org/software/systemd/man/latest/systemd-analyze.html).
