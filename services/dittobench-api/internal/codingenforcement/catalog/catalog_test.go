package catalog

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
)

func TestEmbeddedCatalogIsTheCheckedInFile(t *testing.T) {
	raw, err := os.ReadFile("catalog-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(raw, Bytes()) || SHA256() != digestHex(raw) {
		t.Fatal("embedded catalog differs from catalog-v1.json")
	}
}

func TestCatalogDefinesEveryRequiredProbeSet(t *testing.T) {
	loaded, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	network := loaded.Kinds["network_enforcement"]
	first := network.Probes[0]
	if first.ID != "candidate.router.source" || first.Phase != "active" || first.Scope != ScopeHost ||
		first.Expect.Type != ExpectOutcomeIn || strings.Join(first.Expect.Accept, ",") != "container_address" {
		t.Fatalf("first network probe = %+v", first)
	}
	if bounds := network.EndpointRoles["refusing_proxy"]; bounds != (Bounds{Min: 1, Max: 1}) {
		t.Fatalf("refusing proxy bounds = %+v", bounds)
	}
	trusted := []string{strings.Repeat("a", 64), strings.Repeat("b", 64)}
	counts := map[string]int{}
	for _, kind := range Kinds {
		endpoints := []string(nil)
		if kind == "network_enforcement" {
			endpoints = trusted
		}
		instances, err := loaded.RequiredInstances(kind, endpoints)
		if err != nil {
			t.Fatal(err)
		}
		counts[kind] = len(instances)
	}
	// 20 host probes plus 7 trusted-endpoint probes for two endpoints; 34 and 22
	// probes for each of the four language images; 10 cleanup probes.
	want := map[string]int{
		"network_enforcement":  20 + 7*2,
		"resource_enforcement": 34 * 4,
		"preexec_confinement":  22 * 4,
		"cleanup_recovery":     10,
	}
	for kind, count := range want {
		if counts[kind] != count {
			t.Errorf("%s instances = %d, want %d", kind, counts[kind], count)
		}
	}
	for name, endpoints := range map[string][]string{
		"raw endpoint": {"10.20.0.7:5432"},
		"none":         nil,
		"empty":        {},
		"duplicate":    {trusted[0], trusted[0]},
		"too many":     manyHashes(33),
	} {
		if _, err := loaded.RequiredInstances("network_enforcement", endpoints); err == nil {
			t.Errorf("network %s trusted endpoints accepted", name)
		}
	}
	if _, err := loaded.RequiredInstances("cleanup_recovery", trusted); err == nil {
		t.Error("cleanup accepted trusted endpoints")
	}
	for _, probe := range loaded.Kinds["resource_enforcement"].Probes {
		field, bindable := bindFields[probe.Expect.Type]
		if bindable && probe.Bind[field] == "" {
			t.Errorf("%s reports an unbound %s", probe.ID, field)
		}
	}
	for _, kind := range loaded.Kinds {
		for _, probe := range kind.Probes {
			if strings.Contains(probe.ID, "reboot") || strings.Contains(probe.ID, "restart") {
				t.Errorf("%s claims coverage beyond the same boot", probe.ID)
			}
		}
	}
}

func TestCatalogRefusals(t *testing.T) {
	mutate := func(change func(map[string]any)) []byte {
		decoded, err := Decode(Bytes())
		if err != nil {
			t.Fatal(err)
		}
		value := decoded.(map[string]any)
		change(value)
		raw, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	kind := func(value map[string]any, name string) map[string]any {
		return value["kinds"].(map[string]any)[name].(map[string]any)
	}
	for name, change := range map[string]func(map[string]any){
		"cpu tolerance": func(v map[string]any) {
			v["tolerances"].(map[string]any)["cpu_usage_max_permille_of_quota"] = 1200
		},
		"not covered": func(v map[string]any) { v["not_covered"] = []any{"daemon_restart_recovery"} },
		"coverage":    func(v map[string]any) { v["coverage"] = "cross_boot" },
		"unknown key": func(v map[string]any) { v["approved"] = true },
		"repeated probe": func(v map[string]any) {
			cleanup := kind(v, "cleanup_recovery")
			probes := cleanup["probes"].([]any)
			cleanup["probes"] = append(probes, probes[0])
		},
		"unknown expect": func(v map[string]any) {
			probe := kind(v, "cleanup_recovery")["probes"].([]any)[0].(map[string]any)
			probe["expect"] = map[string]any{"type": "anything"}
		},
		"unknown outcome": func(v map[string]any) {
			probe := kind(v, "network_enforcement")["probes"].([]any)[0].(map[string]any)
			probe["expect"] = map[string]any{"type": "outcome_in", "accept": []any{"teleported"}}
		},
		"extra expect key": func(v map[string]any) {
			probe := kind(v, "network_enforcement")["probes"].([]any)[0].(map[string]any)
			probe["expect"].(map[string]any)["tolerance"] = "cpu_usage_max_permille_of_quota"
		},
		"unused phase": func(v map[string]any) {
			cleanup := kind(v, "cleanup_recovery")
			cleanup["phases"] = append(cleanup["phases"].([]any), "reboot")
		},
		"case variant key": func(v map[string]any) { v["COVERAGE"] = "same_boot" },
		"case variant tolerance": func(v map[string]any) {
			v["tolerances"].(map[string]any)["CPU_usage_max_permille_of_quota"] = 1150
		},
		"case variant probe key": func(v map[string]any) {
			probe := kind(v, "cleanup_recovery")["probes"].([]any)[0].(map[string]any)
			probe["Phase"] = probe["phase"]
			delete(probe, "phase")
		},
		"case variant kind key": func(v map[string]any) {
			cleanup := kind(v, "cleanup_recovery")
			cleanup["Inputs"] = cleanup["inputs"]
			delete(cleanup, "inputs")
		},
		"resource limit unbound": func(v map[string]any) {
			probe := kind(v, "resource_enforcement")["probes"].([]any)[0].(map[string]any)
			probe["bind"] = map[string]any{}
		},
		"network probe binds a limit": func(v map[string]any) {
			probe := kind(v, "network_enforcement")["probes"].([]any)[0].(map[string]any)
			probe["bind"] = map[string]any{"limit": "pids_limit"}
		},
		"authoring timeout bound": func(v map[string]any) {
			resource := kind(v, "resource_enforcement")
			for _, item := range resource["probes"].([]any) {
				probe := item.(map[string]any)
				if probe["id"] == "executor_grading.supervisor_timeout" {
					probe["id"] = "executor_authoring.supervisor_timeout"
				}
			}
		},
		"container limit drift": func(v map[string]any) {
			v["resource_containers"].(map[string]any)["harness"].(map[string]any)["nofile_limit"] = 4096
		},
		"repeated phase": func(v map[string]any) {
			cleanup := kind(v, "cleanup_recovery")
			phases := cleanup["phases"].([]any)
			cleanup["phases"] = append(phases, phases[0])
		},
	} {
		if _, err := Parse(mutate(change)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func manyHashes(count int) []string {
	result := make([]string, count)
	for index := range result {
		result[index] = fmt.Sprintf("%064x", index+1)
	}
	return result
}
