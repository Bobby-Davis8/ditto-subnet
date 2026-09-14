"""Hosted-v2 profile approval builder and curator verifier tests.

Synthetic inputs only; the curator key is a throwaway Ed25519 key generated in
memory per test. No private key file, provider, database or live artifact.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_hippius_publication import (
    load_curator_signing_public_key,
)
from ditto.api_server.coding_private_catalog_v2_compile import (
    compile_private_catalog_v2,
)
from ditto.api_server.coding_private_v2_payload import build_private_v2_payload
from ditto.api_server.coding_private_v2_publication import (
    private_v2_publication_signing_message,
)
from ditto.coding_hosted_profile_approval import (
    DOCUMENT_FIELDS,
    DOCUMENT_SCHEMA,
    LANGUAGE_PROFILES,
    REQUEST_SCHEMA,
    ApprovalInputs,
    ProfileApprovalError,
    build_profile_approval,
    main,
    parse_profile_approval_document,
    verify_profile_approval,
)
from ditto.tests.api_server import test_coding_hosted_profiles as profiles_helper
from ditto.tests.api_server.test_coding_private_v2_payload import _bound_fixture

REPO = Path(__file__).resolve().parents[4]
REVISION = "c0ffee" + "1" * 34
IMAGE_DIGEST = "sha256:" + "9" * 64
LANGUAGE = "python"
INDEX = 3
NATIVE = "approved_native_oci_manifest"


def _h(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


CONTRACT = _h("hosted-grader-contract")


def _canonical(value: dict[str, Any]) -> bytes:
    return coding_canonical_json_bytes(value, maximum_bytes=8 << 20, label="test")


def _compact(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


ENCODERS: dict[str, Callable[[dict[str, Any]], bytes]] = {
    "execution_profile": _canonical,
    "grading_profile": _canonical,
    "profile_receipt": _canonical,
    "payload_authority": _canonical,
    "registration": _canonical,
    "release_index": _compact,
    "native_approval": lambda value: _compact(value) + b"\n",
    "native_provenance": lambda value: _compact(value) + b"\n",
    "native_summary": lambda value: _compact(value) + b"\n",
    "request": lambda value: json.dumps(value, indent=2).encode(),
}


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _image_ref(language: str) -> str:
    digest = IMAGE_DIGEST if language == LANGUAGE else "sha256:" + _h(language)
    return f"coding-runtime.invalid/{language}/runtime@{digest}"


def _objects(curator_sha256: str) -> dict[str, dict[str, Any]]:
    limits = {"MaxPatchBytes": 1 << 20, "MaxToolCalls": 128}
    evidence = {
        name: _h(name)
        for name in (
            "host_preflight",
            "network_enforcement",
            "resource_enforcement",
            "preexec_confinement",
            "cleanup_recovery",
            "private_input_custody",
        )
    }
    return {
        "execution_profile": {
            "schema": "dittobench-coding-hosted-authoring-profile-v2",
            "image_digest": IMAGE_DIGEST,
            "resource_policy": {"CandidateLimits": limits},
            "budgets": {"wall_time_seconds": 600, "workspace_tool_calls": 128},
        },
        "grading_profile": {
            "schema": "dittobench-coding-hosted-grading-profile-v2",
            "image_digest": IMAGE_DIGEST,
            "grader_contract_sha256": CONTRACT,
            "grader_bundle_sha256": _h("grader"),
            "resource_policy": {"CandidateLimits": limits},
            "build": {"Required": False},
            "test_groups": [{"Group": "hidden"}, {"Group": "visible"}],
            "execution_timeout": 600_000_000_000,
        },
        "payload_authority": {
            "schema": "dittobench-coding-private-payload-v2",
            "coding_contract_version": 2,
            "weight_eligible": False,
            "catalog_sha256": _h("catalog"),
            "catalog_merkle_root": _h("merkle"),
            "task_version_count": 5,
            "objects": [],
            "task_assets": [
                {
                    "catalog_index": index,
                    "task_version_id": f"task-version-{index}",
                    "task_commitment_sha256": _h(f"commitment-{index}"),
                    "artifacts": {
                        "grader_bundle": _h("grader" if index == INDEX else str(index))
                    },
                }
                for index in range(5)
            ],
        },
        "registration": {
            "schema": "dittobench-coding-private-v2-registration-v1",
            "coding_contract_version": 2,
            "weight_eligible": False,
            "shadow_only": True,
            "corpus_release_id": "coding-private-v2-test",
            "private_release_sha256": _h("private-release"),
            "transport_sha256": _h("transport"),
            "wrapping_key_sha256": _h("wrapping-key"),
            "publication_receipt_sha256": _h("publication"),
            "previous_registration_sha256": None,
        },
        "profile_receipt": {
            "schema": "dittobench-coding-hosted-profile-receipt-v1",
            "catalog_index": INDEX,
            "task_version_id": f"task-version-{INDEX}",
            "task_commitment_sha256": _h(f"commitment-{INDEX}"),
            "corpus_release_id": "coding-private-v2-test",
            "private_release_sha256": _h("private-release"),
            "request_sha256": _h("profile-request"),
            "image_digest": IMAGE_DIGEST,
            "max_patch_bytes": 1 << 20,
            "grader_bundle_sha256": _h("grader"),
            "grader_contract_sha256": CONTRACT,
            "launch_checks_passed": True,
            "approved": False,
            "shadow_only": True,
            "weight_eligible": False,
        },
        "release_index": {
            "schema": "dittobench-coding-native-release-set-v2",
            "source_revision": REVISION,
            "images": {
                language: {
                    "archive": f"{language}/runtime.oci.tar",
                    "approval": f"{language}/approval.json",
                    "approval_sha256": _h(f"image-approval-{language}"),
                    "archive_sha256": _h(f"archive-{language}"),
                    "image_ref": _image_ref(language),
                    "config_digest": "sha256:" + _h(f"config-{language}"),
                    "driver_profile": profile,
                }
                for language, profile in LANGUAGE_PROFILES.items()
            },
            "runtime": {"archive_sha256": _h("runtime")},
            "independent_approval_required": True,
            "native_imported": False,
            "runtime_qualification": False,
            "canary_completed": False,
            "shadow_only": True,
            "weight_eligible": False,
        },
        "native_approval": {
            "schema": "dittobench-coding-native-controls-approval-v2",
            "purpose": "private-compatibility-once",
            "source_revision": REVISION,
            "plan_sha256": _h("plan"),
            "helper_sha256": _h("helper"),
            "runner_sha256": _h("runner"),
            "binding_sha256": _h("binding"),
            "machine_id_sha256": _h("machine"),
            "boot_id": "01234567-89ab-cdef-0123-456789abcdef",
            "issued_at_unix": 1_900_000_000,
            "expires_at_unix": 1_900_003_600,
            "controls": 16,
            "max_jobs": 2,
            "evidence_sha256": evidence,
            "shadow_only": True,
            "weight_eligible": False,
        },
        "native_provenance": {
            "source_sha": REVISION,
            "plan_sha256": _h("plan"),
            "image_references_sha256": _h("image-references"),
            "helper_sha256": _h("helper"),
            "runner_sha256": _h("runner"),
            "kernel": "6.8.0-test",
            "runtime_qualification": False,
            "production_api_approval": False,
            "image_binding_kind": NATIVE,
        },
        "native_summary": {
            "schema": "dittobench-private-compatibility-summary-v1",
            "source_sha": REVISION,
            "controls": 16,
            "cases": 8,
            "replicates": 2,
            "languages": sorted(LANGUAGE_PROFILES),
            "groups_by_language": dict.fromkeys(LANGUAGE_PROFILES, 1),
            "repeat_results_equal": True,
            "failed_controls": 0,
            "private_controls_passed": True,
            "runtime_qualification": False,
            "production_api_approval": False,
            "native_host_ready": False,
            "canary_completed": False,
            "weight_eligible": False,
            "native_controls_passed": True,
            "image_binding_kind": NATIVE,
        },
        "request": {
            "schema": REQUEST_SCHEMA,
            "catalog_index": INDEX,
            "language": LANGUAGE,
            "source_revision": REVISION,
            "grader_contract_sha256": CONTRACT,
            "curator_signing_key_sha256": curator_sha256,
            "shadow_only": True,
            "weight_eligible": False,
        },
    }


Mutation = Callable[[dict[str, Any]], None]
BytesMutation = Callable[[dict[str, bytes]], None]


def _inputs(
    curator_sha256: str,
    *,
    before: Mutation | None = None,
    transform: dict[str, Callable[[bytes], bytes]] | None = None,
    tamper: BytesMutation | None = None,
) -> ApprovalInputs:
    """Encode consistent inputs; dependents link to each input's final bytes.

    ``before`` edits objects before linking, ``transform`` rewrites one input's
    bytes before its dependents are linked, and ``tamper`` edits bytes after all
    linking so no dependent digest follows.
    """

    objects = _objects(curator_sha256)
    if before is not None:
        before(objects)
    transforms = transform or {}
    out: dict[str, bytes] = {}

    def emit(name: str) -> dict[str, Any]:
        body = ENCODERS[name](objects[name])
        out[name] = transforms.get(name, lambda value: value)(body)
        return json.loads(out[name])

    emit("execution_profile")
    emit("grading_profile")
    payload = objects["payload_authority"]
    payload["payload_sha256"] = _sha(
        _canonical({k: v for k, v in payload.items() if k != "payload_sha256"})
    )
    payload = emit("payload_authority")
    registration = objects["registration"]
    registration.update(
        payload_sha256=payload["payload_sha256"],
        catalog_sha256=payload["catalog_sha256"],
        catalog_merkle_root=payload["catalog_merkle_root"],
    )
    registration["registration_sha256"] = _sha(
        _canonical(
            {k: v for k, v in registration.items() if k != "registration_sha256"}
        )
    )
    registration = emit("registration")
    objects["profile_receipt"].update(
        execution_profile_sha256=_sha(out["execution_profile"]),
        grading_profile_sha256=_sha(out["grading_profile"]),
        payload_authority_file_sha256=_sha(out["payload_authority"]),
        payload_sha256=payload["payload_sha256"],
    )
    emit("profile_receipt")
    release = emit("release_index")
    approval = objects["native_approval"]
    approval["release_manifest_sha256"] = _sha(out["release_index"])
    approval["images"] = {
        language: {
            field: release["images"][language][field]
            for field in (
                "image_ref",
                "config_digest",
                "approval_sha256",
                "driver_profile",
            )
        }
        for language in LANGUAGE_PROFILES
    }
    approval = emit("native_approval")
    authority = {
        "approval_sha256": _sha(out["native_approval"]),
        "release_manifest_sha256": _sha(out["release_index"]),
        "machine_id_sha256": approval["machine_id_sha256"],
        "boot_id": approval["boot_id"],
        "evidence_sha256": approval["evidence_sha256"],
        "daemon_identity_sha256": _h("daemon"),
    }
    objects["native_provenance"].setdefault(
        "images",
        {
            language: {
                "id": "sha256:" + _h(f"id-{language}"),
                "descriptor": None,
                "repo_digests": [release["images"][language]["image_ref"]],
            }
            for language in LANGUAGE_PROFILES
        },
    )
    objects["native_provenance"]["native_control_authority"] = authority
    objects["native_summary"]["native_control_authority"] = authority
    emit("native_provenance")
    emit("native_summary")
    objects["request"].update(
        registration_sha256=registration["registration_sha256"],
        release_manifest_sha256=_sha(out["release_index"]),
        native_controls_approval_sha256=_sha(out["native_approval"]),
    )
    emit("request")
    if tamper is not None:
        tamper(out)
    return ApprovalInputs(**out)


def _patch(name: str, mutate: Mutation) -> BytesMutation:
    def apply(out: dict[str, bytes]) -> None:
        value = json.loads(out[name])
        mutate(value)
        out[name] = ENCODERS[name](value)

    return apply


def _local_matrix_summary(value: dict[str, Any]) -> None:
    for name in (
        "native_control_authority",
        "native_controls_passed",
        "image_binding_kind",
    ):
        value.pop(name)


def _replace(body: bytes) -> Callable[[bytes], bytes]:
    return lambda _old: body


def _pretty(body: bytes) -> bytes:
    return json.dumps(json.loads(body), indent=2, sort_keys=True).encode() + b"\n"


@pytest.fixture
def curator(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path, str]:
    private = Ed25519PrivateKey.generate()
    return (private, *_public_key_file(tmp_path, private, "curator"))


def _public_key_file(
    root: Path, private: Ed25519PrivateKey, name: str
) -> tuple[Path, str]:
    path = root / f"{name}-public.pem"
    path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return path, load_curator_signing_public_key(path)[1]


def test_build_is_deterministic_and_binds_every_recomputed_digest(curator) -> None:
    _private, _path, key_sha256 = curator
    inputs = _inputs(key_sha256)
    body = build_profile_approval(inputs, curator_signing_key_sha256=key_sha256)
    assert body == build_profile_approval(inputs, curator_signing_key_sha256=key_sha256)
    assert body.endswith(b"\n") and body.count(b"\n") == 1
    document = parse_profile_approval_document(body)
    assert set(document) == DOCUMENT_FIELDS
    assert "approved" not in document
    registration = json.loads(inputs.registration)
    payload = json.loads(inputs.payload_authority)
    approval = json.loads(inputs.native_approval)
    release = json.loads(inputs.release_index)
    assert document == {
        "schema": DOCUMENT_SCHEMA,
        "coding_contract_version": 2,
        "catalog_index": INDEX,
        "task_version_id": f"task-version-{INDEX}",
        "task_commitment_sha256": _h(f"commitment-{INDEX}"),
        "corpus_release_id": "coding-private-v2-test",
        "registration_sha256": registration["registration_sha256"],
        "private_release_sha256": _h("private-release"),
        "payload_sha256": payload["payload_sha256"],
        "payload_authority_file_sha256": _sha(inputs.payload_authority),
        "profile_receipt_sha256": _sha(inputs.profile_receipt),
        "execution_profile_sha256": _sha(inputs.execution_profile),
        "grading_profile_sha256": _sha(inputs.grading_profile),
        "grader_contract_sha256": CONTRACT,
        "grader_bundle_sha256": _h("grader"),
        "image_digest": IMAGE_DIGEST,
        "language": LANGUAGE,
        "source_revision": REVISION,
        "release_manifest_sha256": _sha(inputs.release_index),
        "image_approval_sha256": release["images"][LANGUAGE]["approval_sha256"],
        "native_controls_approval_sha256": _sha(inputs.native_approval),
        "native_controls_plan_sha256": approval["plan_sha256"],
        "native_controls_provenance_sha256": _sha(inputs.native_provenance),
        "native_controls_summary_sha256": _sha(inputs.native_summary),
        "curator_signing_key_sha256": key_sha256,
        "shadow_only": True,
        "weight_eligible": False,
    }


def _receipt(mutate: Mutation) -> dict[str, Any]:
    return {"tamper": _patch("profile_receipt", mutate)}


def _summary(**changes: Any) -> dict[str, Any]:
    return {"tamper": _patch("native_summary", lambda value: value.update(changes))}


def _provenance(**changes: Any) -> dict[str, Any]:
    return {"tamper": _patch("native_provenance", lambda value: value.update(changes))}


def _request(**changes: Any) -> dict[str, Any]:
    return {"tamper": _patch("request", lambda value: value.update(changes))}


def _before(name: str, mutate: Mutation) -> dict[str, Any]:
    return {"before": lambda objects: mutate(objects[name])}


def _transform(name: str, change: Callable[[bytes], bytes]) -> dict[str, Any]:
    return {"transform": {name: change}}


def _edit_json(**changes: Any) -> Callable[[bytes], bytes]:
    def apply(body: bytes) -> bytes:
        value = json.loads(body)
        value.update(changes)
        return _canonical(value)

    return apply


REJECTIONS = [
    # Profile set and receipt.
    pytest.param(
        {
            "tamper": _patch(
                "execution_profile",
                lambda value: value["budgets"].update(wall_time_seconds=601),
            )
        },
        "execution profile bytes differ from the receipt",
        id="tampered-execution-profile",
    ),
    pytest.param(
        _receipt(lambda value: value.update(grading_profile_sha256=_h("other"))),
        "grading profile bytes differ from the receipt",
        id="receipt-grading-digest-mismatch",
    ),
    pytest.param(
        _receipt(lambda value: value.update(approved=True)),
        "profile receipt claims approval",
        id="receipt-approved-true",
    ),
    pytest.param(
        _receipt(lambda value: value.update(launch_checks_passed=False)),
        "profile launch checks failed",
        id="launch-checks-false",
    ),
    pytest.param(
        _receipt(lambda value: value.update(weight_eligible=True)),
        "profile receipt is not shadow-only",
        id="receipt-weight-eligible",
    ),
    pytest.param(
        _receipt(lambda value: value.update(test_manifest_sha256=_h("tests"))),
        "profile receipt schema is invalid",
        id="receipt-unknown-field",
    ),
    pytest.param(
        {
            "tamper": lambda out: out.update(
                profile_receipt=_pretty(out["profile_receipt"])
            )
        },
        "profile receipt is not canonical",
        id="receipt-noncanonical",
    ),
    pytest.param(
        _transform("execution_profile", _pretty),
        "execution profile is not canonical",
        id="execution-profile-noncanonical-relinked",
    ),
    pytest.param(
        _before(
            "grading_profile",
            lambda value: value.update(test_manifest_sha256=_h("tests")),
        ),
        "profile schema is invalid",
        id="grading-profile-test-manifest",
    ),
    pytest.param(
        _before(
            "grading_profile",
            lambda value: value.update(image_digest="sha256:" + _h("other")),
        ),
        "profile image digests disagree",
        id="grading-image-drift",
    ),
    pytest.param(
        _request(grader_contract_sha256=_h("other-contract")),
        "grader contract differs from the reviewed pin",
        id="grader-contract-pin",
    ),
    pytest.param(
        _before(
            "grading_profile",
            lambda value: value.update(grader_bundle_sha256=_h("other")),
        ),
        "grader bundle differs from the receipt",
        id="grader-bundle-drift",
    ),
    pytest.param(
        _before(
            "execution_profile",
            lambda value: value["resource_policy"]["CandidateLimits"].update(
                MaxPatchBytes=2
            ),
        ),
        "patch limit differs from the receipt",
        id="patch-limit-drift",
    ),
    # Catalog index and private-v2 identity.
    pytest.param(
        _request(catalog_index=INDEX + 1),
        "profile catalog index differs from the reviewed index",
        id="catalog-index-mismatch",
    ),
    pytest.param(
        {
            "before": lambda objects: (
                objects["request"].update(catalog_index=99),
                objects["profile_receipt"].update(catalog_index=99),
            )
        },
        "reviewed catalog index is not exactly one payload task",
        id="catalog-index-absent-from-payload",
    ),
    pytest.param(
        {
            "tamper": _patch(
                "payload_authority",
                lambda value: value.update(catalog_sha256=_h("other")),
            )
        },
        "payload authority bytes differ from the receipt",
        id="tampered-payload-authority",
    ),
    pytest.param(
        _transform("payload_authority", _edit_json(task_version_count=6)),
        "payload authority digest is invalid",
        id="payload-self-digest-stale",
    ),
    pytest.param(
        _before(
            "payload_authority",
            lambda value: value["task_assets"][INDEX].update(
                task_commitment_sha256=_h("other")
            ),
        ),
        "payload task differs from the profile receipt",
        id="payload-task-commitment-drift",
    ),
    pytest.param(
        _request(registration_sha256=_h("live-registration")),
        "registration differs from the reviewed pin",
        id="registration-pin",
    ),
    pytest.param(
        _before(
            "registration",
            lambda value: value.update(private_release_sha256=_h("other-release")),
        ),
        "registered release differs from the profile task",
        id="registration-private-release-drift",
    ),
    pytest.param(
        _transform("registration", _edit_json(corpus_release_id="other-release")),
        "registration authority is invalid",
        id="registration-self-digest-stale",
    ),
    # Release set.
    pytest.param(
        _request(release_manifest_sha256=_h("other-release-index")),
        "release index differs from the reviewed pin",
        id="release-index-pin",
    ),
    pytest.param(
        _before("release_index", lambda value: value.update(source_revision="d" * 40)),
        "release revision differs from the reviewed revision",
        id="release-revision-mismatch",
    ),
    pytest.param(
        _request(source_revision="d" * 40),
        "release revision differs from the reviewed revision",
        id="request-revision-mismatch",
    ),
    pytest.param(
        _before("release_index", lambda value: value.update(native_imported=True)),
        "release index is invalid",
        id="release-readiness-edited",
    ),
    pytest.param(
        _transform("release_index", _pretty),
        "release index is not its writer's exact encoding",
        id="release-index-noncanonical-relinked",
    ),
    pytest.param(
        _request(language="node"),
        "profile image is not the reviewed language's release image",
        id="language-image-mismatch",
    ),
    # Native compatibility controls.
    pytest.param(
        _request(native_controls_approval_sha256=_h("other-approval")),
        "native approval differs from the reviewed pin",
        id="native-approval-pin",
    ),
    pytest.param(
        _before(
            "native_approval", lambda value: value.update(source_revision="d" * 40)
        ),
        "native approval revision differs from the reviewed revision",
        id="native-approval-revision-mismatch",
    ),
    pytest.param(
        _transform(
            "native_approval",
            lambda body: (
                _compact({**json.loads(body), "release_manifest_sha256": _h("other")})
                + b"\n"
            ),
        ),
        "native approval names another release set",
        id="native-approval-other-release",
    ),
    pytest.param(
        _before("native_approval", lambda value: value.update(extra=True)),
        "native approval is invalid",
        id="native-approval-unknown-field",
    ),
    pytest.param(
        _provenance(source_sha="d" * 40),
        "native control revision differs from the reviewed revision",
        id="provenance-revision-mismatch",
    ),
    pytest.param(
        _summary(source_sha="d" * 40),
        "native control revision differs from the reviewed revision",
        id="summary-revision-mismatch",
    ),
    pytest.param(
        {
            "tamper": _patch(
                "native_summary",
                lambda value: value["native_control_authority"].update(
                    daemon_identity_sha256=_h("other-daemon")
                ),
            )
        },
        "native control authority differs from the approval",
        id="native-authority-drift",
    ),
    pytest.param(
        _provenance(plan_sha256=_h("other-plan")),
        "native control inputs differ from the approval",
        id="native-plan-drift",
    ),
    pytest.param(
        {
            "tamper": _patch(
                "native_summary",
                _local_matrix_summary,
            )
        },
        "native control outputs are not native-bound",
        id="local-matrix-summary",
    ),
    pytest.param(
        _provenance(image_binding_kind="local_config_id_not_native_import_approval"),
        "native provenance is not an approved native binding",
        id="local-image-binding",
    ),
    pytest.param(
        {
            "tamper": _patch(
                "native_provenance",
                lambda value: value["images"][LANGUAGE].update(repo_digests=[]),
            )
        },
        "native controls did not run the profile image",
        id="native-image-not-run",
    ),
    pytest.param(
        _summary(native_controls_passed=False),
        "native compatibility controls did not pass",
        id="native-controls-failed",
    ),
    pytest.param(
        _summary(private_controls_passed=False),
        "native compatibility controls did not pass",
        id="private-controls-failed",
    ),
    pytest.param(
        _summary(failed_controls=1),
        "native compatibility controls did not pass",
        id="native-failed-control-count",
    ),
    pytest.param(
        _summary(repeat_results_equal=False),
        "native compatibility controls did not pass",
        id="native-repeats-unequal",
    ),
    pytest.param(
        _summary(controls=14),
        "native compatibility controls did not pass",
        id="native-controls-incomplete",
    ),
    pytest.param(
        _summary(native_host_ready=True),
        "native compatibility controls did not pass",
        id="native-summary-readiness-edited",
    ),
    # Reviewed request.
    pytest.param(
        _request(approved=True),
        "approval request is invalid",
        id="request-unknown-field",
    ),
    pytest.param(
        _request(weight_eligible=True),
        "approval request is invalid",
        id="request-weight-eligible",
    ),
    pytest.param(
        _request(catalog_index=True),
        "approval request is invalid",
        id="request-boolean-index",
    ),
]


@pytest.mark.parametrize(("case", "reason"), REJECTIONS)
def test_builder_rejects_drift(curator, case: dict[str, Any], reason: str) -> None:
    _private, _path, key_sha256 = curator
    inputs = _inputs(key_sha256, **case)
    with pytest.raises(ProfileApprovalError) as error:
        build_profile_approval(inputs, curator_signing_key_sha256=key_sha256)
    assert str(error.value) == reason


def test_builder_rejects_a_curator_key_other_than_the_reviewed_pin(curator) -> None:
    _private, _path, key_sha256 = curator
    with pytest.raises(ProfileApprovalError, match="differs from the reviewed pin"):
        build_profile_approval(
            _inputs(key_sha256), curator_signing_key_sha256=_h("other-key")
        )


def _signed(curator) -> tuple[bytes, bytes, Path, str]:
    private, path, key_sha256 = curator
    document = build_profile_approval(
        _inputs(key_sha256), curator_signing_key_sha256=key_sha256
    )
    return document, private.sign(document), path, key_sha256


def test_verifier_accepts_the_pinned_curator_signature(curator) -> None:
    document, signature, path, key_sha256 = _signed(curator)
    verified = verify_profile_approval(
        document=document,
        signature=signature,
        curator_public_key_path=path,
        curator_signing_key_sha256=key_sha256,
    )
    assert verified.document_sha256 == _sha(document)
    assert verified.signature_sha256 == _sha(signature)
    assert verified.document["curator_signing_key_sha256"] == key_sha256


def test_verifier_rejects_wrong_keys_pins_and_modified_bytes(curator, tmp_path) -> None:
    document, signature, path, key_sha256 = _signed(curator)
    other = Ed25519PrivateKey.generate()
    other_path, other_sha256 = _public_key_file(tmp_path, other, "other")

    def rejects(reason: str, **overrides: Any) -> None:
        arguments: dict[str, Any] = {
            "document": document,
            "signature": signature,
            "curator_public_key_path": path,
            "curator_signing_key_sha256": key_sha256,
            **overrides,
        }
        with pytest.raises(ProfileApprovalError) as error:
            verify_profile_approval(**arguments)
        assert str(error.value) == reason

    rejects("curator signature does not verify", signature=other.sign(document))
    rejects(
        "approval document names another curator key",
        signature=other.sign(document),
        curator_public_key_path=other_path,
        curator_signing_key_sha256=other_sha256,
    )
    rejects(
        "curator public key differs from the pinned identity",
        curator_signing_key_sha256=other_sha256,
    )
    rejects(
        "curator public key differs from the pinned identity",
        curator_public_key_path=other_path,
    )
    position = document.index(b'"execution_profile_sha256":"') + 28
    flipped = bytearray(document)
    flipped[position] = ord("0") if flipped[position] != ord("0") else ord("1")
    rejects("curator signature does not verify", document=bytes(flipped))
    modified = bytearray(signature)
    modified[0] ^= 1
    rejects("curator signature does not verify", signature=bytes(modified))
    rejects("pinned curator key digest is invalid", curator_signing_key_sha256="aa")


def test_verifier_never_accepts_an_unsigned_document(curator) -> None:
    document, _signature, path, key_sha256 = _signed(curator)
    for signature in (b"", b"\x00" * 64, document[:64], document):
        with pytest.raises(ProfileApprovalError):
            verify_profile_approval(
                document=document,
                signature=signature,
                curator_public_key_path=path,
                curator_signing_key_sha256=key_sha256,
            )


def test_verifier_rejects_validly_signed_documents_outside_the_schema(
    curator,
) -> None:
    private, path, key_sha256 = curator
    document = parse_profile_approval_document(
        build_profile_approval(
            _inputs(key_sha256), curator_signing_key_sha256=key_sha256
        )
    )
    publication = private_v2_publication_signing_message(
        manifest={
            "schema": "dittobench-coding-private-v2-transport-v1",
            "coding_contract_version": 2,
            "weight_eligible": False,
            "transport_sha256": _h("transport"),
            "payload_sha256": _h("payload"),
            "catalog_sha256": _h("catalog"),
            "catalog_merkle_root": _h("merkle"),
            "wrapping_key_sha256": _h("wrapping-key"),
            "objects": [{}],
        },
        source_sha=REVISION,
        probe_receipt_payload_sha256=_h("probe"),
        private_input_authority_sha256=_h("authority"),
        curator_signing_key_sha256=key_sha256,
    )
    candidates = {
        "publication signing message": publication,
        "noncanonical": json.dumps(document, indent=2).encode(),
        "approved field": _canonical({**document, "approved": True}),
        "weight eligible": _canonical({**document, "weight_eligible": True}),
        "shadow only false": _canonical({**document, "shadow_only": False}),
        "missing field": _canonical(
            {k: v for k, v in document.items() if k != "grader_contract_sha256"}
        ),
        "other schema": _canonical({**document, "schema": "other"}),
    }
    for body in candidates.values():
        with pytest.raises(ProfileApprovalError):
            verify_profile_approval(
                document=body,
                signature=private.sign(body),
                curator_public_key_path=path,
                curator_signing_key_sha256=key_sha256,
            )


def _write_inputs(root: Path, inputs: ApprovalInputs) -> dict[str, Path]:
    root.mkdir(mode=0o700)
    profiles = root / "profiles"
    profiles.mkdir(mode=0o700)
    (profiles / "execution-profile.json").write_bytes(inputs.execution_profile)
    (profiles / "grading-profile.json").write_bytes(inputs.grading_profile)
    (profiles / "receipt.json").write_bytes(inputs.profile_receipt)
    paths = {"profiles": profiles}
    for name in (
        "request",
        "payload_authority",
        "registration",
        "release_index",
        "native_approval",
        "native_summary",
        "native_provenance",
    ):
        paths[name] = root / f"{name}.json"
        paths[name].write_bytes(getattr(inputs, name))
    return paths


def _build_argv(paths: dict[str, Path], key: Path, output: Path) -> list[str]:
    argv = ["build"]
    for name, path in paths.items():
        argv += [f"--{name.replace('_', '-')}", str(path)]
    return [*argv, "--curator-public-key", str(key), "--output", str(output)]


def test_cli_builds_a_draft_and_verifies_only_a_signed_document(
    curator, tmp_path, capsys
) -> None:
    private, key_path, key_sha256 = curator
    paths = _write_inputs(tmp_path / "inputs", _inputs(key_sha256))
    output = tmp_path / "inputs" / "approval-document.json"
    assert main(_build_argv(paths, key_path, output)) == 0
    document = output.read_bytes()
    assert capsys.readouterr().out == (
        f"approved=false\ndocument_sha256={_sha(document)}\n"
    )
    assert output.stat().st_mode & 0o777 == 0o600
    assert main(_build_argv(paths, key_path, output)) == 70
    assert "output must be new" in capsys.readouterr().err

    verify = [
        "verify",
        "--document",
        str(output),
        "--curator-public-key",
        str(key_path),
        "--curator-signing-key-sha256",
        key_sha256,
    ]
    assert main(verify) == 70
    assert capsys.readouterr().err == (
        "hosted profile approval rejected: arguments are invalid\n"
    )
    signature = tmp_path / "inputs" / "approval-signature.bin"
    signature.write_bytes(b"")
    assert main([*verify, "--signature", str(signature)]) == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "curator signature is missing or malformed" in captured.err

    signature.write_bytes(private.sign(document))
    assert main([*verify, "--signature", str(signature)]) == 0
    lines = capsys.readouterr().out.splitlines()
    printed = dict(line.split("=", 1) for line in lines)
    assert printed["document_sha256"] == _sha(document)
    assert printed["signature_sha256"] == _sha(signature.read_bytes())
    assert printed["curator_signing_key_sha256"] == key_sha256
    assert all(name.endswith("_sha256") or name == "image_digest" for name in printed)


def test_cli_rejects_a_profile_directory_that_is_not_exact_helper_output(
    curator, tmp_path, capsys
) -> None:
    _private, key_path, key_sha256 = curator
    paths = _write_inputs(tmp_path / "inputs", _inputs(key_sha256))
    (paths["profiles"] / "request.json").write_bytes(b"{}")
    output = tmp_path / "inputs" / "approval-document.json"
    assert main(_build_argv(paths, key_path, output)) == 70
    assert "profile directory is not an exact helper output" in capsys.readouterr().err
    assert not output.exists()


def test_real_profile_helper_output_builds_and_verifies(curator, tmp_path) -> None:
    private, key_path, key_sha256 = curator
    root = tmp_path / "helper"
    root.mkdir(mode=0o700)
    binary = root / "dittobench-coding-hosted-profiles"
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/dittobench-coding-hosted-profiles"],
        cwd=REPO / "services/dittobench-api",
        check=True,
        capture_output=True,
        timeout=600,
    )
    release_path, groups = _bound_fixture(root, native_authoring=True)
    compile_private_catalog_v2(
        release_authority=release_path, groups_root=groups, output=root / "catalog"
    )
    build_private_v2_payload(
        catalog_directory=root / "catalog", groups_root=groups, output=root / "payload"
    )
    result, output = profiles_helper.run(
        binary, root, root / "payload", profiles_helper.request()
    )
    assert result.returncode == 0, result.stderr
    real = {
        "execution_profile": (output / "execution-profile.json").read_bytes(),
        "grading_profile": (output / "grading-profile.json").read_bytes(),
        "profile_receipt": (output / "receipt.json").read_bytes(),
        "payload_authority": (root / "payload/payload-authority.json").read_bytes(),
    }
    receipt = json.loads(real["profile_receipt"])
    grading = json.loads(real["grading_profile"])

    def before(objects: dict[str, Any]) -> None:
        objects["request"].update(
            catalog_index=receipt["catalog_index"],
            grader_contract_sha256=grading["grader_contract_sha256"],
        )
        objects["registration"].update(
            corpus_release_id=receipt["corpus_release_id"],
            private_release_sha256=receipt["private_release_sha256"],
        )

    inputs = _inputs(
        key_sha256,
        before=before,
        transform={name: _replace(body) for name, body in real.items()},
    )
    document = build_profile_approval(inputs, curator_signing_key_sha256=key_sha256)
    value = parse_profile_approval_document(document)
    assert value["execution_profile_sha256"] == receipt["execution_profile_sha256"]
    assert value["grading_profile_sha256"] == receipt["grading_profile_sha256"]
    assert value["grader_contract_sha256"] == receipt["grader_contract_sha256"]
    assert value["task_commitment_sha256"] == receipt["task_commitment_sha256"]
    verified = verify_profile_approval(
        document=document,
        signature=private.sign(document),
        curator_public_key_path=key_path,
        curator_signing_key_sha256=key_sha256,
    )
    assert verified.document_sha256 == _sha(document)
    unapproved = copy.deepcopy(receipt)
    unapproved["approved"] = True
    with pytest.raises(ProfileApprovalError, match="claims approval"):
        build_profile_approval(
            _inputs(
                key_sha256,
                before=before,
                transform={
                    **{name: _replace(body) for name, body in real.items()},
                    "profile_receipt": _replace(_canonical(unapproved)),
                },
            ),
            curator_signing_key_sha256=key_sha256,
        )
