package scenario

import (
	"context"
	"os"

	"github.com/Azure/agentbaker/e2e/config"
)

// Each backend needs its own fresh node and instrumented image, not the baseline scenario's node.
func init() {
	for _, backend := range []string{"ftrace", "lttng"} {
		image := *config.VHDUbuntu2604MinimalGen2Containerd
		image.Name += "-boot-trace-" + backend
		Register(&Scenario{
			Name:        "Ubuntu2604Minimal_BootTrace_" + backend,
			Description: "Captures first-boot scheduler and block activity before workload validation",
			SkipIf: func(context.Context) string {
				if os.Getenv("E2E_BOOT_TRACE") != "true" {
					return "experimental boot tracing requires E2E_BOOT_TRACE=true"
				}
				if config.Config.TestPreProvision || config.Config.VHDMetadataFile == "" {
					return "boot tracing requires direct provisioning and exact VHD metadata"
				}
				return ""
			},
			Config: Config{
				Cluster:                ClusterLatestKubernetesVersionKubenet,
				VHD:                    &image,
				BootTraceBackend:       backend,
				ExcludeFromNBCSampling: true,
			},
		})
	}
}
