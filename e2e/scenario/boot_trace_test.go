package scenario

import (
	"archive/tar"
	"bytes"
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Azure/agentbaker/e2e/config"
	"github.com/Azure/azure-sdk-for-go/sdk/azcore/to"
	"github.com/Azure/azure-sdk-for-go/sdk/resourcemanager/containerservice/armcontainerservice/v8"
	"github.com/stretchr/testify/require"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/fake"
	ktesting "k8s.io/client-go/testing"
)

const traceTestBootID = "11111111-1111-1111-1111-111111111111"
const traceTestPrefix = "azure:///subscriptions/test/resourceGroups/nodes/providers/Microsoft.Compute/virtualMachineScaleSets/trace/virtualMachines/"

func traceTestNode(uid string, ready bool, transition time.Time) *corev1.Node {
	status := corev1.ConditionFalse
	if ready {
		status = corev1.ConditionTrue
	}
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: "trace-node", UID: types.UID(uid)},
		Spec:       corev1.NodeSpec{ProviderID: traceTestPrefix + "0"},
		Status: corev1.NodeStatus{
			NodeInfo: corev1.NodeSystemInfo{BootID: traceTestBootID},
			Conditions: []corev1.NodeCondition{{
				Type: corev1.NodeReady, Status: status, LastTransitionTime: metav1.NewTime(transition),
			}},
		},
	}
}

func TestBootTraceObservesFirstReadyAndIgnoresUnrelatedNodes(t *testing.T) {
	stream := watch.NewRaceFreeFake()
	defer stream.Stop()
	first := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	other := traceTestNode("other", true, first.Add(-time.Minute))
	other.Spec.ProviderID = strings.ReplaceAll(traceTestPrefix, "/trace/", "/trace-other/") + "0"
	stream.Add(other)
	stream.Add(traceTestNode("first", false, first))
	stream.Modify(traceTestNode("first", true, first))
	stream.Modify(traceTestNode("first", true, first.Add(time.Minute)))
	result := observeBootTraceReady(context.Background(), stream, strings.ToUpper(traceTestPrefix), nil)
	require.NoError(t, result.err)
	require.Equal(t, types.UID("first"), result.node.UID)
	require.Equal(t, first, result.transition)
	require.False(t, result.observed.IsZero())
}

func TestBootTraceRejectsWatchGapsAndIdentityChanges(t *testing.T) {
	for _, name := range []string{"closed", "expired", "replaced", "deleted", "rebooted", "missing-boot-id", "missing-transition"} {
		t.Run(name, func(t *testing.T) {
			stream := watch.NewRaceFreeFake()
			defer stream.Stop()
			first := time.Now().UTC()
			switch name {
			case "closed":
				stream.Stop()
			case "expired":
				stream.Error(&metav1.Status{Code: 410, Reason: metav1.StatusReasonExpired})
			case "replaced":
				stream.Add(traceTestNode("first", false, first))
				stream.Modify(traceTestNode("replacement", true, first))
			case "deleted":
				stream.Delete(traceTestNode("first", false, first))
			case "rebooted":
				stream.Add(traceTestNode("first", false, first))
				node := traceTestNode("first", true, first)
				node.Status.NodeInfo.BootID = "22222222-2222-2222-2222-222222222222"
				stream.Modify(node)
			case "missing-boot-id":
				node := traceTestNode("first", true, first)
				node.Status.NodeInfo.BootID = ""
				stream.Add(node)
			case "missing-transition":
				stream.Add(traceTestNode("first", true, time.Time{}))
			}
			require.Error(t, observeBootTraceReady(context.Background(), stream, traceTestPrefix, nil).err)
		})
	}
}

func TestBootTraceKeepsFirstReadyBeforeProviderIDIsPopulated(t *testing.T) {
	stream := watch.NewRaceFreeFake()
	defer stream.Stop()
	first := time.Now().UTC()
	early := traceTestNode("first", true, first)
	early.Spec.ProviderID = ""
	stream.Add(early)
	stream.Modify(traceTestNode("first", false, first.Add(time.Second)))
	result := observeBootTraceReady(context.Background(), stream, traceTestPrefix, nil)
	require.NoError(t, result.err)
	require.Equal(t, first, result.transition)
	require.Equal(t, traceTestPrefix+"0", result.node.Spec.ProviderID)
}

func TestBootTraceWatchUsesListResourceVersionBeforeVMCreation(t *testing.T) {
	client := fake.NewSimpleClientset()
	client.PrependReactor("list", "nodes", func(ktesting.Action) (bool, runtime.Object, error) {
		return true, &corev1.NodeList{ListMeta: metav1.ListMeta{ResourceVersion: "42"}}, nil
	})
	stream := watch.NewRaceFreeFake()
	client.PrependWatchReactor("nodes", func(action ktesting.Action) (bool, watch.Interface, error) {
		require.Equal(t, "42", action.(ktesting.WatchAction).GetWatchRestrictions().ResourceVersion)
		return true, stream, nil
	})
	s := &Scenario{
		Config: Config{BootTraceBackend: "ftrace"},
		Runtime: &ScenarioRuntime{
			Kube: &Kubeclient{Typed: client}, VMSSName: "trace",
			Cluster: &Cluster{Model: &armcontainerservice.ManagedCluster{
				Properties: &armcontainerservice.ManagedClusterProperties{NodeResourceGroup: to.Ptr("nodes")},
			}},
		},
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	observer, err := startBootTraceWatch(ctx, s)
	require.NoError(t, err)
	defer observer.stop()
	stream.Error(&metav1.Status{Code: 410})
	select {
	case result := <-observer.result:
		require.ErrorContains(t, result.err, "watch failed")
	case <-ctx.Done():
		t.Fatal("readiness observer did not report the watch failure")
	}
}

func TestBootTraceRefusesAnAlreadyRegisteredVMSS(t *testing.T) {
	oldSubscription := config.Config.SubscriptionID
	config.Config.SubscriptionID = "test"
	defer func() { config.Config.SubscriptionID = oldSubscription }()
	s := &Scenario{
		Config: Config{BootTraceBackend: "lttng"},
		Runtime: &ScenarioRuntime{
			Kube:     &Kubeclient{Typed: fake.NewSimpleClientset(traceTestNode("existing", true, time.Now()))},
			VMSSName: "trace",
			Cluster: &Cluster{Model: &armcontainerservice.ManagedCluster{
				Properties: &armcontainerservice.ManagedClusterProperties{NodeResourceGroup: to.Ptr("nodes")},
			}},
		},
	}
	_, err := startBootTraceWatch(context.Background(), s)
	require.ErrorContains(t, err, "already has a registered node")
}

func TestBootTraceArchiveExtraction(t *testing.T) {
	for _, name := range []string{"valid", "traversal", "symlink", "duplicate", "foreign-boot", "reserved-file", "oversize", "truncated"} {
		t.Run(name, func(t *testing.T) {
			directory := t.TempDir()
			archivePath := filepath.Join(directory, "input.tar")
			var data bytes.Buffer
			writer := tar.NewWriter(&data)
			add := func(path string, kind byte, body string) {
				header := &tar.Header{Name: path, Typeflag: kind, Mode: 0o600, Size: int64(len(body))}
				if kind == tar.TypeSymlink {
					header.Linkname = "/etc/passwd"
				}
				require.NoError(t, writer.WriteHeader(header))
				_, err := io.WriteString(writer, body)
				require.NoError(t, err)
			}
			prefix := traceTestBootID + "/"
			switch name {
			case "traversal":
				add(prefix+"../escape", tar.TypeReg, "")
			case "symlink":
				add(prefix+"metadata.json", tar.TypeSymlink, "")
			case "duplicate":
				add(prefix+"metadata.json", tar.TypeReg, "{}")
				add(prefix+"metadata.json", tar.TypeReg, "{}")
			case "foreign-boot":
				add("another-boot/metadata.json", tar.TypeReg, "{}")
			case "reserved-file":
				add(prefix+"readiness.json", tar.TypeReg, "{}")
			default:
				add(prefix+"metadata.json", tar.TypeReg, "{}")
				add(prefix+"ctf/trace/channel_0", tar.TypeReg, "events")
			}
			require.NoError(t, writer.Close())
			content := data.Bytes()
			if name == "truncated" {
				content = content[:513]
			}
			require.NoError(t, os.WriteFile(archivePath, content, 0o600))
			destination := filepath.Join(directory, "bundle")
			require.NoError(t, os.Mkdir(destination, 0o700))
			maximum := bootTraceMaxBytes
			if name == "oversize" {
				maximum = 1
			}
			err := extractBootTraceArchive(archivePath, destination, traceTestBootID, maximum)
			if name != "valid" {
				require.Error(t, err)
				return
			}
			require.NoError(t, err)
			actual, err := os.ReadFile(filepath.Join(destination, "ctf/trace/channel_0"))
			require.NoError(t, err)
			require.Equal(t, "events", string(actual))
		})
	}
}

func TestBootTraceStreamCannotExceedLimit(t *testing.T) {
	var output bytes.Buffer
	writer := &bootTraceLimitedWriter{destination: &output, remaining: 3}
	n, err := writer.Write([]byte("abc"))
	require.NoError(t, err)
	require.Equal(t, 3, n)
	_, err = writer.Write([]byte("d"))
	require.ErrorContains(t, err, "byte limit")
	require.Equal(t, "abc", output.String())
}
