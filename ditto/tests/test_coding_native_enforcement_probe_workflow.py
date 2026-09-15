"""Static guards for the rootless native enforcement probe-runner CI job."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/coding-native-enforcement-probe.yml"
PROBE = ROOT / "services/dittobench-api/internal/codingenforcement/probe"


def load() -> tuple[str, dict]:
    text = WORKFLOW.read_text()
    return text, yaml.load(text, Loader=yaml.BaseLoader)


def test_probe_job_is_path_filtered_bounded_and_credential_free():
    text, workflow = load()
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    paths = workflow["on"]["pull_request"]["paths"]
    assert "services/dittobench-api/**" not in paths
    assert ".github/workflows/coding-native-enforcement-probe.yml" in paths
    # Every monorepo package the runner and its integration test import triggers it.
    sources = [
        *PROBE.glob("*.go"),
        *(
            ROOT / "services/dittobench-api/cmd/dittobench-coding-enforcement-probe"
        ).glob("*.go"),
    ]
    for source in sources:
        for imported in re.findall(
            r'"github\.com/ditto-assistant/dittobench-api/(internal/[a-z0-9_/]+)"',
            source.read_text(),
        ):
            package = f"services/dittobench-api/{imported}/"
            assert any(
                path.endswith("/**") and package.startswith(path[:-2]) for path in paths
            ), imported
    assert workflow["permissions"] == {"contents": "read"}
    for forbidden in ("secrets.", "id-token", "environment:", "self-hosted", "gcloud"):
        assert forbidden not in text
    (job,) = workflow["jobs"].values()
    assert job["runs-on"] == "ubuntu-24.04"
    assert 0 < int(job["timeout-minutes"]) <= 20
    assert job["steps"][0]["with"]["persist-credentials"] == "false"


def test_probe_job_pins_docker_delegates_cgroups_and_runs_the_exact_tests():
    text, workflow = load()
    env = workflow["env"]
    assert env["DOCKER_VERSION"] == "29.1.3"
    for key in ("DOCKER_SHA256", "DOCKER_ROOTLESS_EXTRAS_SHA256"):
        assert re.fullmatch(r"[0-9a-f]{64}", env[key])
    assert "sha256sum --check --strict" in text
    # Resource limits are only enforced with a delegated systemd cgroup driver.
    assert "Delegate=cpu cpuset io memory pids" in text
    assert "native.cgroupdriver=systemd" in text
    assert "io.heyditto.dittobench.isolated=true" in text
    assert "docker pull" not in text and "registry:" not in text
    assert "-tags native_probe_integration" in text
    test_name = "TestExecutorResourceEnforcementIsMeasuredThroughTheProductionLaunch"
    assert f"-test.run '^{test_name}$'" in text
    assert "DITTOBENCH_NATIVE_PROBE_OUTPUT" in text
    assert "ditto/tests/test_coding_native_probe_runner.py" in text
    integration = (PROBE / "resource_integration_linux_test.go").read_text()
    assert integration.startswith("//go:build native_probe_integration\n")
    assert "t.Skip" not in integration


def test_probe_runner_is_never_wired_to_a_host_workflow():
    workflows = ROOT / ".github/workflows"
    for path in workflows.glob("*.y*ml"):
        if path.name == WORKFLOW.name:
            continue
        text = path.read_text()
        assert "dittobench-coding-enforcement-probe" not in text or (
            path.name == "coding-native-release.yml"
        ), path.name
    operate = workflows / "coding-hosted-operate.yml"
    if operate.exists():
        assert "enforcement-probe" not in operate.read_text()
