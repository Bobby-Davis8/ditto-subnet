"""End-to-end: the Go probe runner's records pass the offline PR1 verifier.

The runner binary assembles records from collected observations; this test feeds
its output back through the exact offline verifier and asserts it is accepted,
and that a weakened observation is refused. It reuses the PR1 evidence tests'
synthetic World (reviewed checkout, store and profiles) so no host, daemon,
custody path or credential is touched. A Docker-gated case proves the runner
refuses a missing approved image instead of pulling it; it skips cleanly when
Docker is unavailable.
"""

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
API = ROOT / "services/dittobench-api"
CMD = "./cmd/dittobench-coding-enforcement-probe"

PR1_TESTS = Path(__file__).parent / "test_coding_native_enforcement_evidence.py"
_pr1_spec = importlib.util.spec_from_file_location("pr1_evidence_tests", PR1_TESTS)
assert _pr1_spec is not None and _pr1_spec.loader is not None
PR1 = importlib.util.module_from_spec(_pr1_spec)
_pr1_spec.loader.exec_module(PR1)


def _go() -> str | None:
    return shutil.which("go")


@pytest.fixture(scope="module")
def runner_binary(tmp_path_factory) -> Path:
    go = _go()
    if go is None:
        pytest.skip("go toolchain is unavailable")
    out = tmp_path_factory.mktemp("probe-runner") / "enforcement-probe"
    build = subprocess.run(
        [go, "build", "-o", str(out), CMD],
        cwd=API,
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f"probe runner did not build: {build.stderr}")
    return out


def _env_file(record: dict) -> dict:
    return {
        "host": record["host"],
        "release": record["release"],
        "tools": record["tools"],
        "inputs": record["inputs"],
        "pre_collection_preflight_sha256": record["pre_collection_preflight_sha256"],
        "started_at_unix": record["started_at_unix"],
        "completed_at_unix": record["completed_at_unix"],
    }


def _observations_file(record: dict) -> dict:
    phases = []
    for phase in record["phases"]:
        observations = [
            {
                "id": probe["id"],
                "language": probe["language"],
                "endpoint_sha256": probe["endpoint_sha256"],
                "observed": probe["observed"],
            }
            for probe in phase["probes"]
        ]
        phases.append(
            {
                "name": phase["name"],
                "started_at_unix": phase["started_at_unix"],
                "completed_at_unix": phase["completed_at_unix"],
                "observations": observations,
            }
        )
    return {"phases": phases}


def _assemble(
    runner_binary: Path,
    tmp: Path,
    kind: str,
    record: dict,
    extra: list[str] | None = None,
) -> bytes:
    env_path = tmp / "env.json"
    obs_path = tmp / "observations.json"
    env_path.write_text(json.dumps(_env_file(record)))
    obs_path.write_text(json.dumps(_observations_file(record)))
    result = subprocess.run(
        [
            str(runner_binary),
            "assemble",
            "--kind",
            kind,
            "--env",
            str(env_path),
            "--observations",
            str(obs_path),
            *(extra or []),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


# The runner collects the non-network kinds; network is deferred to a later PR.
NON_NETWORK_KINDS = ("resource_enforcement", "preexec_confinement", "cleanup_recovery")


@pytest.mark.parametrize("kind", NON_NETWORK_KINDS)
def test_runner_record_equals_the_verified_record(runner_binary, tmp_path, kind):
    world = PR1.World(tmp_path)
    record = world.records[kind]
    produced = _assemble(runner_binary, tmp_path, kind, record)
    assert produced == PR1.canonical(record), "runner diverged from the canonical form"


@pytest.mark.parametrize("kind", NON_NETWORK_KINDS)
def test_verifier_accepts_the_runner_records(runner_binary, tmp_path, kind):
    world = PR1.World(tmp_path)
    record = world.records[kind]
    produced = _assemble(runner_binary, tmp_path, kind, record)
    sha = world.put(produced)
    result, ok = world.verify([sha])
    assert ok and result["records"][0]["failure"] is None, result


def test_verifier_refuses_a_weakened_runner_record(runner_binary, tmp_path):
    world = PR1.World(tmp_path)
    record = PR1.copy.deepcopy(world.records["resource_enforcement"])
    # A container whose measured OOM peak sits far below the floor is not
    # enforcement. The runner recomputes matched=false; the verifier refuses it.
    probe = PR1.find_probe(record, "harness.memory_oom", language="go")
    probe["observed"]["measured"] = 1
    produced = _assemble(runner_binary, tmp_path, "resource_enforcement", record)
    sha = world.put(produced)
    failure = world.verify([sha])[0]["records"][0]["failure"]
    assert failure is not None and "harness.memory_oom" in failure, failure


def test_test_mode_exposes_only_passed_and_total(runner_binary, tmp_path):
    world = PR1.World(tmp_path)
    record = world.records["cleanup_recovery"]
    output = _assemble(
        runner_binary, tmp_path, "cleanup_recovery", record, extra=["--test"]
    )
    summary = json.loads(output)
    assert set(summary) == {"passed", "total"}
    assert summary["passed"] == summary["total"] > 0


def test_runner_refuses_a_missing_approved_image_instead_of_pulling(runner_binary):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    info = subprocess.run([docker, "info"], capture_output=True, check=False)
    if info.returncode != 0:
        pytest.skip("docker daemon is unavailable")
    absent = "registry.invalid/enforcement-probe-e2e@sha256:" + "0" * 64
    result = subprocess.run(
        [str(runner_binary), "resolve-images", absent],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "refusing to pull" in result.stderr, result.stderr


LIVE_OUTPUT = "DITTOBENCH_NATIVE_PROBE_OUTPUT"


def _live_observations() -> dict:
    path = os.environ.get(LIVE_OUTPUT)
    if not path or not Path(path).is_file():
        pytest.skip(f"{LIVE_OUTPUT} is unset; the rootless CI job provides it")
    return json.loads(Path(path).read_text())


def _live_world(tmp_path: Path, live: dict, monkeypatch) -> "PR1.World":
    """A World whose approved grading profile is the one the live run enforced."""

    for name, value in live["grading_resource_policy"].items():
        monkeypatch.setitem(PR1.GRADING_POLICY, name, value)
    grading_sha = PR1.hashlib.sha256(PR1.go_canonical(PR1.GRADING_PROFILE)).hexdigest()
    monkeypatch.setitem(PR1.INPUTS, "grading_profile_sha256", grading_sha)
    return PR1.World(tmp_path)


def _splice_live(record: dict, live: dict, mutate=None) -> int:
    spliced = 0
    for phase in live["phases"]:
        for observation in phase["Observations"]:
            probe = PR1.find_probe(record, observation["ID"], observation["Language"])
            observed = dict(observation["Observed"])
            if mutate is not None:
                observed = mutate(observation["ID"], observed)
            probe["observed"] = observed
            spliced += 1
    return spliced


def test_verifier_accepts_live_rootless_executor_observations(
    runner_binary, tmp_path, monkeypatch
):
    live = _live_observations()
    world = _live_world(tmp_path, live, monkeypatch)
    record = PR1.copy.deepcopy(world.records["resource_enforcement"])
    assert _splice_live(record, live) >= 5
    produced = _assemble(runner_binary, tmp_path, "resource_enforcement", record)
    result, ok = world.verify([world.put(produced)])
    assert ok and result["records"][0]["failure"] is None, result


def test_verifier_refuses_live_observations_under_a_weakened_profile(
    runner_binary, tmp_path, monkeypatch
):
    live = _live_observations()
    world = _live_world(tmp_path, live, monkeypatch)
    record = PR1.copy.deepcopy(world.records["resource_enforcement"])

    def loosen(probe_id, observed):
        # A launch that doubled the pids cap no longer equals the approved profile.
        if probe_id == "executor_grading.pids_max":
            observed["cgroup"] = observed["cgroup"] * 2
        return observed

    _splice_live(record, live, loosen)
    produced = _assemble(runner_binary, tmp_path, "resource_enforcement", record)
    failure = world.verify([world.put(produced)])[0]["records"][0]["failure"]
    assert failure is not None and "executor_grading.pids_max" in failure, failure
