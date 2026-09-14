"""Static checks that the scorer image carries exactly the certification pack.

The default-off certification canary loads a repo-shaped root from the sandbox
scorer image. These checks bind the Dockerfile's copy, digest pins, and runtime
path to the committed files without building an image.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
DOCKERFILE = ROOT / "services/dittobench-api/Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"

PACK_DIR = "research/dittobench-coding-datagen/certification/v1"
POLICY = (
    "packages/dittobench-coding-contract/testdata/"
    "coding_inference_policy_locked_v1.json"
)
STAGE_ROOT = "/pack/certification-root"
IMAGE_ROOT = "/opt/ditto/coding/certification-root"
# The identity Platform binds into every certification lease and the Go loader
# test pins; see services/dittobench-api/internal/codingcanary/pack_test.go.
MANIFEST_SHA256 = "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903"
POLICY_FILE_SHA256 = "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"


def _instructions() -> list[str]:
    logical: list[str] = []
    current = ""
    for line in DOCKERFILE.read_text().splitlines():
        if not current and line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        if current.strip():
            logical.append(" ".join(current.split()))
        current = ""
    assert not current
    return logical


def _stages() -> dict[str, list[str]]:
    stages: dict[str, list[str]] = {}
    name = ""
    for instruction in _instructions():
        if instruction.startswith("FROM "):
            name = instruction.rsplit(" AS ", 1)[1]
            assert name not in stages
            stages[name] = []
        if name:
            stages[name].append(instruction)
    return stages


def _sha256(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def _pins() -> dict[str, str]:
    # Read the raw stage text: sha256sum -c and `cut -c67-` both depend on the
    # exact two-space separator that instruction normalization would collapse.
    text = DOCKERFILE.read_text()
    stage = text.split(" AS coding-certification-pack\n", 1)[1].split("\nFROM ", 1)[0]
    pins: dict[str, str] = {}
    for digest, path in re.findall(r'"([0-9a-f]{64})  ([^"\s]+)"', stage):
        assert path not in pins
        pins[path] = digest
    assert len(pins) == stage.count("  research/") + stage.count("  packages/")
    return pins


def test_pack_stage_copies_only_the_certification_capsule_and_locked_policy() -> None:
    stage = _stages()["coding-certification-pack"]
    assert stage[0] == "FROM alpine:3.22 AS coding-certification-pack"
    copies = [item for item in stage if item.startswith(("COPY ", "ADD "))]
    assert copies == [
        f"COPY {PACK_DIR}/ {STAGE_ROOT}/{PACK_DIR}/",
        f"COPY {POLICY} {STAGE_ROOT}/{POLICY}",
    ]


def test_pack_stage_pins_every_committed_file_to_its_digest() -> None:
    pins = _pins()
    committed = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / PACK_DIR).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert set(pins) == committed | {POLICY}
    for path, digest in pins.items():
        assert _sha256(path) == digest, path

    manifest = json.loads((ROOT / PACK_DIR / "manifest.json").read_bytes())
    assert pins[f"{PACK_DIR}/manifest.json"] == MANIFEST_SHA256
    assert manifest["inference_policy"] == {
        "path": POLICY,
        "sha256": POLICY_FILE_SHA256,
    }
    assert pins[POLICY] == POLICY_FILE_SHA256
    task = manifest["grader_plan"]["task_id"]
    for grader_file in manifest["grader_plan"]["grader_files"]:
        path = f"{PACK_DIR}/capsules/{task}/grader/{grader_file['path']}"
        assert pins[path] == grader_file["sha256"]


def test_pack_stage_fails_the_build_on_drift_extra_files_or_links() -> None:
    stage = _stages()["coding-certification-pack"]
    (verify,) = [item for item in stage if item.startswith("RUN ")]
    for required in (
        "> /tmp/certification-pack.sha256",
        "sha256sum -c /tmp/certification-pack.sha256",
        "cut -c67- /tmp/certification-pack.sha256 | sort",
        "find . -type f | sed 's|^\\./||' | sort",
        "diff -u /tmp/certification-pack.expected /tmp/certification-pack.actual",
        'test -z "$(find . ! -type f ! -type d)"',
        "find . -type d -exec chmod 0555 {} +",
        "find . -type f -exec chmod 0444 {} +",
    ):
        assert required in verify, required
    assert " || " not in verify
    assert stage.index(verify) == len(stage) - 1


def test_only_the_sandbox_scorer_carries_the_pack_at_a_traversable_fixed_root() -> None:
    stages = _stages()
    carriers = {
        name
        for name, stage in stages.items()
        if any("--from=coding-certification-pack" in item for item in stage)
    }
    assert carriers == {"sandbox"}
    sandbox = stages["sandbox"]
    copy = "COPY --from=coding-certification-pack /pack/ /opt/ditto/coding/"
    assert sandbox.count(copy) == 1
    # BuildKit gives the implicitly created /opt/ditto/coding the policy COPY's
    # --chmod=0444, which the non-root scorer cannot traverse.
    policy_copy = (
        f"COPY --chown=65532:65532 --chmod=0444 {POLICY} "
        "/opt/ditto/coding/coding_inference_policy_locked_v1.json"
    )
    chmod = "RUN chmod 0555 /opt/ditto/coding"
    user = "USER 65532:65532"
    assert sandbox.index(policy_copy) < sandbox.index(copy) < sandbox.index(chmod)
    assert sandbox.index(chmod) < sandbox.index(user)
    # /pack/certification-root lands at the fixed runtime root.
    assert STAGE_ROOT.removeprefix("/pack/") == IMAGE_ROOT.removeprefix(
        "/opt/ditto/coding/"
    )


def test_compose_points_the_gated_canary_at_the_baked_root_only() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    scorer = compose["services"]["dittobench-api"]
    assert scorer["build"]["target"] == "sandbox"
    environment = scorer["environment"]
    assert environment["DITTOBENCH_CODING_CANARY_ENABLED"] == (
        "${DITTOBENCH_CODING_CANARY_ENABLED:-false}"
    )
    assert environment["DITTOBENCH_CODING_CERTIFICATION_ROOT"] == IMAGE_ROOT
    assert scorer["read_only"] is True
    for volume in scorer.get("volumes", []):
        target = volume.split(":")[1] if isinstance(volume, str) else volume["target"]
        assert not target.startswith("/opt/ditto")
