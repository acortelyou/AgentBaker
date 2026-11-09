#!/bin/bash
set -euo pipefail

check_trace_target() {
    local backend="$1" base_version="$2" image_label="$3"
    local os_id os_version
    os_id=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
    os_version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"')
    if [[ "$os_id" != ubuntu || "$os_version" != 26.04 || "$(uname -m)" != x86_64 ]]; then
        echo "Boot tracing only supports Ubuntu 26.04 x86_64" >&2
        return 1
    fi
    if [[ "$backend" != ftrace && "$backend" != lttng ]]; then
        echo "Expected ftrace or lttng" >&2
        return 1
    fi
    if [[ ! "$base_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || ! "$image_label" =~ ^[a-zA-Z0-9._-]+$ ]]; then
        echo "An exact base image version and a safe image label are required" >&2
        return 1
    fi
    if [[ "$(uname -r)" != 7.0.0-1020-azure ]]; then
        echo "Recheck boot-trace compatibility before changing the pinned kernel" >&2
        return 1
    fi
}

prepare_trace_image() {
    local backend="$1" base_version="$2" image_label="$3" source_dir="$4"
    check_trace_target "$backend" "$base_version" "$image_label"
    install -d -m 0755 /opt/aks-node-boot-trace
    install -m 0644 "$source_dir/capture.py" "$source_dir/stop_capture.py" \
        "$source_dir/ftrace-grub.cfg" /opt/aks-node-boot-trace/
    install -m 0755 "$source_dir/install.sh" /opt/aks-node-boot-trace/install.sh
    install -m 0644 "$source_dir/aks-node-boot-trace.service" /etc/systemd/system/
    printf 'BACKEND=%s\nMAX_SECONDS=600\nMAX_MIB=256\nIMAGE_LABEL=%s\n' \
        "$backend" "$image_label" > /etc/aks-node-boot-trace.env
    chmod 0600 /etc/aks-node-boot-trace.env
    printf 'backend=%s\nbase_version=%s\nkernel=%s\n' \
        "$backend" "$base_version" "$(uname -r)" > /opt/aks-node-boot-trace/image.txt

    if [[ "$backend" == lttng ]]; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            lttng-tools=2.14.1-1build1 lttng-modules-dkms=2.15.0-1ubuntu1.1
        apt-mark manual lttng-tools lttng-modules-dkms "linux-headers-$(uname -r)"
        dkms autoinstall -k "$(uname -r)"
        depmod -a "$(uname -r)"
        modinfo -k "$(uname -r)" lttng-tracer
        systemctl disable --now lttng-sessiond.service
        install -d -m 0755 /etc/systemd/system/lttng-sessiond.service.d \
            /etc/systemd/system/aks-node-boot-trace.service.d
        cat > /etc/systemd/system/lttng-sessiond.service.d/boot-trace.conf <<'EOF'
[Unit]
DefaultDependencies=no
After=local-fs.target
Before=aks-node-boot-trace.service sysinit.target shutdown.target
Conflicts=shutdown.target

[Service]
EnvironmentFile=
Environment=DAEMON_ARGS=
EOF
        cat > /etc/systemd/system/aks-node-boot-trace.service.d/lttng.conf <<'EOF'
[Unit]
Requires=lttng-sessiond.service
After=lttng-sessiond.service
EOF
    fi
    dpkg-query -W -f='${binary:Package}=${Version}\n' python3 "linux-image-$(uname -r)" \
        >> /opt/aks-node-boot-trace/image.txt
    if [[ "$backend" == lttng ]]; then
        dpkg-query -W -f='${binary:Package}=${Version}\n' lttng-tools lttng-modules-dkms \
            >> /opt/aks-node-boot-trace/image.txt
    fi
    systemctl daemon-reload
    systemctl disable aks-node-boot-trace.service
}

arm_trace_image() {
    local backend="$1" base_version="$2" image_label="$3"
    local kernel config flag event
    check_trace_target "$backend" "$base_version" "$image_label"
    kernel=$(uname -r)
    config="/boot/config-$kernel"
    for flag in MODULES KALLSYMS HIGH_RES_TIMERS TRACEPOINTS KPROBES EVENT_TRACING; do
        grep -qx "CONFIG_${flag}=y" "$config"
    done
    if [[ -d /var/log/aks-node-boot-trace ]]; then
        echo "A capture already exists on the builder; refusing to seal this image" >&2
        return 1
    fi
    if [[ "$backend" == ftrace ]]; then
        if [[ ! -e /sys/kernel/tracing/available_events ]]; then
            mount -t tracefs tracefs /sys/kernel/tracing
        fi
        for event in sched:sched_switch sched:sched_wakeup sched:sched_wakeup_new \
            sched:sched_process_fork sched:sched_process_exec sched:sched_process_exit \
            block:block_rq_issue block:block_rq_complete; do
            grep -qx "$event" /sys/kernel/tracing/available_events
        done
        install -m 0644 /opt/aks-node-boot-trace/ftrace-grub.cfg \
            /etc/default/grub.d/99-aks-boot-trace.cfg
        update-grub
    else
        modinfo -k "$kernel" lttng-tracer
        if [[ -e /etc/default/grub.d/99-aks-boot-trace.cfg ]]; then
            echo "LTTng image must not contain ftrace boot arming" >&2
            return 1
        fi
    fi
    systemctl enable aks-node-boot-trace.service
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    if [[ "$#" != 4 ]]; then
        echo "Usage: install.sh prepare|arm ftrace|lttng BASE_VERSION IMAGE_LABEL" >&2
        exit 1
    fi
    case "$1" in
        prepare) prepare_trace_image "$2" "$3" "$4" "$(dirname "$0")" ;;
        arm) arm_trace_image "$2" "$3" "$4" ;;
        *) echo "Expected prepare or arm" >&2; exit 1 ;;
    esac
fi
