"""Hosted-v2 profile approval document: deterministic builder and curator verifier.

Run only on an owner-controlled machine. ``build`` binds one launch-checked
execution/grading profile pair to its registered private-v2 release, the native
release set and the native compatibility controls, recomputing every digest from
bytes. Its output is an unsigned draft (``approved=false``) and carries no
approval field that could be flipped. Approval exists only as a detached 64-byte
Ed25519 curator signature over the exact canonical document bytes, the format
the private-v2 publication signing message already uses. ``verify`` checks that
signature against a pinned curator key. No private key is read, accepted or
produced here, and nothing is activated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

DOCUMENT_SCHEMA = "dittobench-coding-hosted-profile-approval-v1"
REQUEST_SCHEMA = "dittobench-coding-hosted-profile-approval-request-v1"
MAX_DOCUMENT_BYTES = 16 << 10
SIGNATURE_BYTES = 64
LANGUAGE_PROFILES = {
    "python": "python-call-ast-v2",
    "node": "node-call-ast-v2",
    "go": "go-call-ast-v1",
    "rust": "rust-call-ast-v1",
}
PROFILE_FILES = ("execution-profile.json", "grading-profile.json", "receipt.json")
REQUEST_FIELDS = frozenset(
    {
        "schema",
        "catalog_index",
        "language",
        "source_revision",
        "registration_sha256",
        "release_manifest_sha256",
        "native_controls_approval_sha256",
        "grader_contract_sha256",
        "curator_signing_key_sha256",
        "shadow_only",
        "weight_eligible",
    }
)
_REQUEST_PINS = (
    "registration_sha256",
    "release_manifest_sha256",
    "native_controls_approval_sha256",
    "grader_contract_sha256",
    "curator_signing_key_sha256",
)
# Exactly the dittobench-coding-hosted-profile-receipt-v1 fields that
# cmd/dittobench-coding-hosted-profiles writes.
RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "catalog_index",
        "task_version_id",
        "task_commitment_sha256",
        "corpus_release_id",
        "private_release_sha256",
        "payload_sha256",
        "payload_authority_file_sha256",
        "request_sha256",
        "image_digest",
        "execution_profile_sha256",
        "grading_profile_sha256",
        "max_patch_bytes",
        "grader_bundle_sha256",
        "grader_contract_sha256",
        "launch_checks_passed",
        "approved",
        "shadow_only",
        "weight_eligible",
    }
)
# coding_runtime/qualification/native.py policy() and Binding.provenance().
NATIVE_APPROVAL_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "source_revision",
        "release_manifest_sha256",
        "plan_sha256",
        "helper_sha256",
        "runner_sha256",
        "binding_sha256",
        "machine_id_sha256",
        "boot_id",
        "issued_at_unix",
        "expires_at_unix",
        "controls",
        "max_jobs",
        "images",
        "evidence_sha256",
        "shadow_only",
        "weight_eligible",
    }
)
_NATIVE_IMAGE_FIELDS = (
    "image_ref",
    "config_digest",
    "approval_sha256",
    "driver_profile",
)
_NATIVE_AUTHORITY_FIELDS = frozenset(
    {
        "approval_sha256",
        "release_manifest_sha256",
        "machine_id_sha256",
        "boot_id",
        "evidence_sha256",
        "daemon_identity_sha256",
    }
)
# coding_runtime/qualification/run.py native-bound provenance.json and summary.json.
PROVENANCE_FIELDS = frozenset(
    {
        "source_sha",
        "plan_sha256",
        "image_references_sha256",
        "helper_sha256",
        "runner_sha256",
        "images",
        "kernel",
        "runtime_qualification",
        "production_api_approval",
        "image_binding_kind",
        "native_control_authority",
    }
)
SUMMARY_FIELDS = frozenset(
    {
        "schema",
        "source_sha",
        "controls",
        "cases",
        "replicates",
        "languages",
        "groups_by_language",
        "repeat_results_equal",
        "failed_controls",
        "private_controls_passed",
        "runtime_qualification",
        "production_api_approval",
        "native_host_ready",
        "canary_completed",
        "weight_eligible",
        "native_control_authority",
        "native_controls_passed",
        "image_binding_kind",
    }
)
DOCUMENT_SHA256_FIELDS = (
    "task_commitment_sha256",
    "registration_sha256",
    "private_release_sha256",
    "payload_sha256",
    "payload_authority_file_sha256",
    "profile_receipt_sha256",
    "execution_profile_sha256",
    "grading_profile_sha256",
    "grader_contract_sha256",
    "grader_bundle_sha256",
    "release_manifest_sha256",
    "image_approval_sha256",
    "native_controls_approval_sha256",
    "native_controls_plan_sha256",
    "native_controls_provenance_sha256",
    "native_controls_summary_sha256",
    "curator_signing_key_sha256",
)
DOCUMENT_FIELDS = frozenset(
    {
        *DOCUMENT_SHA256_FIELDS,
        "schema",
        "coding_contract_version",
        "catalog_index",
        "task_version_id",
        "corpus_release_id",
        "image_digest",
        "language",
        "source_revision",
        "shadow_only",
        "weight_eligible",
    }
)
_NATIVE_BINDING = "approved_native_oci_manifest"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")


class ProfileApprovalError(ValueError):
    """Safe rejection with a fixed reason and no input content."""


@dataclass(frozen=True)
class ApprovalInputs:
    request: bytes
    execution_profile: bytes
    grading_profile: bytes
    profile_receipt: bytes
    payload_authority: bytes
    registration: bytes
    release_index: bytes
    native_approval: bytes
    native_summary: bytes
    native_provenance: bytes


@dataclass(frozen=True)
class VerifiedApproval:
    document: dict[str, Any]
    document_sha256: str
    signature_sha256: str


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ProfileApprovalError(reason)


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SHA256.fullmatch(value) is not None
        and value != "0" * 64
    )


def _image_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _digest(value.removeprefix("sha256:"))
    )


def _revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and _REVISION.fullmatch(value) is not None
        and value != "0" * 40
    )


def _identifier(value: object) -> bool:
    from ditto.api_models.coding_evaluation import _bounded_identifier

    if not isinstance(value, str) or not value:
        return False
    try:
        _bounded_identifier(value, 256)
    except ValueError:
        return False
    return True


def _index(value: object) -> bool:
    return type(value) is int and 0 <= value <= 999_999


def _json(body: bytes, maximum: int, label: str) -> dict[str, Any]:
    from ditto.api_models.coding_inference import _decode_json_document

    try:
        value = _decode_json_document(body, maximum_bytes=maximum)
    except ValueError:
        raise ProfileApprovalError(f"{label} is not strict bounded JSON") from None
    _require(isinstance(value, dict), f"{label} is not a JSON object")
    return value


def _canonical(value: dict[str, Any], maximum: int, label: str) -> bytes:
    from ditto.api_models.coding_canonical import coding_canonical_json_bytes

    try:
        return coding_canonical_json_bytes(value, maximum_bytes=maximum, label=label)
    except (TypeError, ValueError):
        raise ProfileApprovalError(f"{label} exceeds its canonical bound") from None


def _canonical_json(body: bytes, maximum: int, label: str) -> dict[str, Any]:
    value = _json(body, maximum, label)
    _require(_canonical(value, maximum, label) == body, f"{label} is not canonical")
    return value


def _compact_json(
    body: bytes, maximum: int, label: str, *, newline: bool
) -> dict[str, Any]:
    """Exact bytes of the release/qualification writers' sorted compact JSON."""

    value = _json(body, maximum, label)
    expected = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    _require(
        body == expected + (b"\n" if newline else b""),
        f"{label} is not its writer's exact encoding",
    )
    return value


def _registration(body: bytes) -> dict[str, Any]:
    from pydantic import ValidationError

    from ditto.api_models.coding_private_v2_registry import (
        CodingPrivateV2RegistrationAuthority,
    )

    value = _canonical_json(body, 64 << 10, "registration")
    try:
        authority = CodingPrivateV2RegistrationAuthority.model_validate(value)
    except ValidationError:
        raise ProfileApprovalError("registration authority is invalid") from None
    _require(
        set(value) == set(authority.model_dump(mode="json", by_alias=True))
        and value["shadow_only"] is True
        and value["weight_eligible"] is False
        and type(value["coding_contract_version"]) is int,
        "registration authority is invalid",
    )
    return value


def _request(body: bytes) -> dict[str, Any]:
    request = _json(body, 1 << 16, "approval request")
    _require(
        set(request) == REQUEST_FIELDS
        and request["schema"] == REQUEST_SCHEMA
        and request["shadow_only"] is True
        and request["weight_eligible"] is False
        and _index(request["catalog_index"])
        and request["language"] in LANGUAGE_PROFILES
        and _revision(request["source_revision"])
        and all(_digest(request[name]) for name in _REQUEST_PINS),
        "approval request is invalid",
    )
    return request


def build_profile_approval(
    inputs: ApprovalInputs, *, curator_signing_key_sha256: str
) -> bytes:
    """Return the exact canonical unsigned approval document bytes."""

    from ditto.api_server.coding_hosted_grading import GRADING_PROFILE_KEYS

    request = _request(inputs.request)
    _require(
        curator_signing_key_sha256 == request["curator_signing_key_sha256"],
        "curator public key differs from the reviewed pin",
    )
    index = request["catalog_index"]
    language = request["language"]
    revision = request["source_revision"]

    # Profile set: every digest is recomputed from bytes; receipt claims are only
    # compared against those recomputations, never copied on trust.
    receipt = _canonical_json(inputs.profile_receipt, 1 << 16, "profile receipt")
    _require(
        set(receipt) == RECEIPT_FIELDS
        and receipt["schema"] == "dittobench-coding-hosted-profile-receipt-v1",
        "profile receipt schema is invalid",
    )
    _require(receipt["launch_checks_passed"] is True, "profile launch checks failed")
    _require(receipt["approved"] is False, "profile receipt claims approval")
    _require(
        receipt["shadow_only"] is True and receipt["weight_eligible"] is False,
        "profile receipt is not shadow-only",
    )
    _require(
        type(receipt["catalog_index"]) is int and receipt["catalog_index"] == index,
        "profile catalog index differs from the reviewed index",
    )
    execution = _canonical_json(inputs.execution_profile, 16384, "execution profile")
    _require(
        _sha(inputs.execution_profile) == receipt["execution_profile_sha256"],
        "execution profile bytes differ from the receipt",
    )
    grading = _canonical_json(inputs.grading_profile, 65536, "grading profile")
    _require(
        _sha(inputs.grading_profile) == receipt["grading_profile_sha256"],
        "grading profile bytes differ from the receipt",
    )
    _require(
        execution.get("schema") == "dittobench-coding-hosted-authoring-profile-v2"
        and set(grading) == GRADING_PROFILE_KEYS
        and grading["schema"] == "dittobench-coding-hosted-grading-profile-v2",
        "profile schema is invalid",
    )
    image_digest = execution.get("image_digest")
    _require(
        _image_digest(image_digest)
        and grading["image_digest"] == image_digest
        and receipt["image_digest"] == image_digest,
        "profile image digests disagree",
    )
    _require(
        grading["grader_contract_sha256"] == receipt["grader_contract_sha256"]
        and grading["grader_contract_sha256"] == request["grader_contract_sha256"],
        "grader contract differs from the reviewed pin",
    )
    _require(
        _digest(grading["grader_bundle_sha256"])
        and grading["grader_bundle_sha256"] == receipt["grader_bundle_sha256"],
        "grader bundle differs from the receipt",
    )
    policy = execution.get("resource_policy")
    limits = policy.get("CandidateLimits") if isinstance(policy, dict) else None
    _require(
        isinstance(limits, dict)
        and type(limits.get("MaxPatchBytes")) is int
        and limits["MaxPatchBytes"] == receipt["max_patch_bytes"],
        "patch limit differs from the receipt",
    )

    # Private-v2 identity: payload authority task and the registered release.
    _require(
        _sha(inputs.payload_authority) == receipt["payload_authority_file_sha256"],
        "payload authority bytes differ from the receipt",
    )
    payload = _canonical_json(inputs.payload_authority, 8 << 20, "payload authority")
    projection = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    _require(
        payload.get("schema") == "dittobench-coding-private-payload-v2"
        and type(payload.get("coding_contract_version")) is int
        and payload["coding_contract_version"] == 2
        and payload.get("weight_eligible") is False
        and _digest(payload.get("payload_sha256"))
        and _sha(_canonical(projection, 8 << 20, "payload authority"))
        == payload["payload_sha256"]
        and payload["payload_sha256"] == receipt["payload_sha256"],
        "payload authority digest is invalid",
    )
    task_assets = payload.get("task_assets")
    tasks = [
        task
        for task in (task_assets if isinstance(task_assets, list) else [])
        if isinstance(task, dict)
        and type(task.get("catalog_index")) is int
        and task["catalog_index"] == index
    ]
    _require(len(tasks) == 1, "reviewed catalog index is not exactly one payload task")
    artifacts = tasks[0].get("artifacts")
    _require(
        tasks[0].get("task_version_id") == receipt["task_version_id"]
        and tasks[0].get("task_commitment_sha256") == receipt["task_commitment_sha256"]
        and isinstance(artifacts, dict)
        and artifacts.get("grader_bundle") == grading["grader_bundle_sha256"],
        "payload task differs from the profile receipt",
    )
    registration = _registration(inputs.registration)
    _require(
        registration["registration_sha256"] == request["registration_sha256"],
        "registration differs from the reviewed pin",
    )
    _require(
        registration["corpus_release_id"] == receipt["corpus_release_id"]
        and registration["private_release_sha256"] == receipt["private_release_sha256"]
        and registration["payload_sha256"] == payload["payload_sha256"]
        and registration["catalog_sha256"] == payload.get("catalog_sha256"),
        "registered release differs from the profile task",
    )

    # Native release set: the pinned index names the profile image for the language.
    _require(
        _sha(inputs.release_index) == request["release_manifest_sha256"],
        "release index differs from the reviewed pin",
    )
    release = _compact_json(
        inputs.release_index, 64 << 10, "release index", newline=False
    )
    _require(
        release.get("schema") == "dittobench-coding-native-release-set-v2"
        and release.get("independent_approval_required") is True
        and release.get("shadow_only") is True
        and all(
            release.get(field) is False
            for field in (
                "native_imported",
                "runtime_qualification",
                "canary_completed",
                "weight_eligible",
            )
        ),
        "release index is invalid",
    )
    _require(
        release.get("source_revision") == revision,
        "release revision differs from the reviewed revision",
    )
    raw_images = release.get("images")
    images: dict[str, Any] = raw_images if isinstance(raw_images, dict) else {}
    _require(
        set(images) == set(LANGUAGE_PROFILES)
        and all(
            isinstance(images[name], dict)
            and images[name].get("driver_profile") == profile
            and _digest(images[name].get("approval_sha256"))
            for name, profile in LANGUAGE_PROFILES.items()
        ),
        "release images are invalid",
    )
    image = images[language]
    _require(
        image.get("image_ref")
        == f"coding-runtime.invalid/{language}/runtime@{image_digest}",
        "profile image is not the reviewed language's release image",
    )

    # Native compatibility controls: consumed approval, provenance and summary.
    _require(
        _sha(inputs.native_approval) == request["native_controls_approval_sha256"],
        "native approval differs from the reviewed pin",
    )
    approval = _json(inputs.native_approval, 65536, "native approval")
    _require(
        set(approval) == NATIVE_APPROVAL_FIELDS
        and approval["schema"] == "dittobench-coding-native-controls-approval-v2"
        and approval["purpose"] == "private-compatibility-once"
        and approval["shadow_only"] is True
        and approval["weight_eligible"] is False
        and type(approval["controls"]) is int
        and all(
            _digest(approval[name])
            for name in ("plan_sha256", "helper_sha256", "runner_sha256")
        ),
        "native approval is invalid",
    )
    _require(
        approval["source_revision"] == revision,
        "native approval revision differs from the reviewed revision",
    )
    _require(
        approval["release_manifest_sha256"] == request["release_manifest_sha256"],
        "native approval names another release set",
    )
    approved_images = approval["images"]
    _require(
        isinstance(approved_images, dict)
        and set(approved_images) == set(LANGUAGE_PROFILES)
        and all(
            isinstance(approved_images[name], dict)
            and set(approved_images[name]) == set(_NATIVE_IMAGE_FIELDS)
            and all(
                approved_images[name][field] == images[name].get(field)
                for field in _NATIVE_IMAGE_FIELDS
            )
            for name in LANGUAGE_PROFILES
        ),
        "native approval images differ from the release set",
    )
    provenance = _compact_json(
        inputs.native_provenance, 1 << 20, "native provenance", newline=True
    )
    summary = _compact_json(
        inputs.native_summary, 1 << 20, "native summary", newline=True
    )
    _require(
        set(provenance) == PROVENANCE_FIELDS and set(summary) == SUMMARY_FIELDS,
        "native control outputs are not native-bound",
    )
    _require(
        provenance["source_sha"] == revision and summary["source_sha"] == revision,
        "native control revision differs from the reviewed revision",
    )
    authority = provenance["native_control_authority"]
    _require(
        isinstance(authority, dict)
        and set(authority) == _NATIVE_AUTHORITY_FIELDS
        and summary["native_control_authority"] == authority
        and authority["approval_sha256"] == request["native_controls_approval_sha256"]
        and authority["release_manifest_sha256"] == request["release_manifest_sha256"]
        and all(
            authority[name] == approval[name]
            for name in ("machine_id_sha256", "boot_id", "evidence_sha256")
        ),
        "native control authority differs from the approval",
    )
    _require(
        all(
            provenance[name] == approval[name]
            for name in ("plan_sha256", "helper_sha256", "runner_sha256")
        ),
        "native control inputs differ from the approval",
    )
    _require(
        provenance["image_binding_kind"] == _NATIVE_BINDING
        and summary["image_binding_kind"] == _NATIVE_BINDING
        and provenance["runtime_qualification"] is False
        and provenance["production_api_approval"] is False,
        "native provenance is not an approved native binding",
    )
    inspected = provenance["images"]
    inspected_image = inspected.get(language) if isinstance(inspected, dict) else None
    repo_digests = (
        inspected_image.get("repo_digests")
        if isinstance(inspected_image, dict)
        else None
    )
    _require(
        isinstance(repo_digests, list) and image["image_ref"] in repo_digests,
        "native controls did not run the profile image",
    )
    cases = summary["cases"]
    _require(
        summary["schema"] == "dittobench-private-compatibility-summary-v1"
        and summary["private_controls_passed"] is True
        and summary["native_controls_passed"] is True
        and summary["repeat_results_equal"] is True
        and type(summary["failed_controls"]) is int
        and summary["failed_controls"] == 0
        and type(summary["replicates"]) is int
        and summary["replicates"] == 2
        and type(cases) is int
        and cases > 0
        and type(summary["controls"]) is int
        and summary["controls"] == 2 * cases
        and summary["controls"] == approval["controls"]
        and summary["languages"] == sorted(LANGUAGE_PROFILES)
        and all(
            summary[field] is False
            for field in (
                "runtime_qualification",
                "production_api_approval",
                "native_host_ready",
                "canary_completed",
                "weight_eligible",
            )
        ),
        "native compatibility controls did not pass",
    )

    document = {
        "schema": DOCUMENT_SCHEMA,
        "coding_contract_version": 2,
        "catalog_index": index,
        "task_version_id": receipt["task_version_id"],
        "task_commitment_sha256": receipt["task_commitment_sha256"],
        "corpus_release_id": registration["corpus_release_id"],
        "registration_sha256": registration["registration_sha256"],
        "private_release_sha256": registration["private_release_sha256"],
        "payload_sha256": payload["payload_sha256"],
        "payload_authority_file_sha256": _sha(inputs.payload_authority),
        "profile_receipt_sha256": _sha(inputs.profile_receipt),
        "execution_profile_sha256": _sha(inputs.execution_profile),
        "grading_profile_sha256": _sha(inputs.grading_profile),
        "grader_contract_sha256": grading["grader_contract_sha256"],
        "grader_bundle_sha256": grading["grader_bundle_sha256"],
        "image_digest": image_digest,
        "language": language,
        "source_revision": revision,
        "release_manifest_sha256": _sha(inputs.release_index),
        "image_approval_sha256": image["approval_sha256"],
        "native_controls_approval_sha256": _sha(inputs.native_approval),
        "native_controls_plan_sha256": approval["plan_sha256"],
        "native_controls_provenance_sha256": _sha(inputs.native_provenance),
        "native_controls_summary_sha256": _sha(inputs.native_summary),
        "curator_signing_key_sha256": curator_signing_key_sha256,
        "shadow_only": True,
        "weight_eligible": False,
    }
    body = _canonical(document, MAX_DOCUMENT_BYTES, "approval document")
    parse_profile_approval_document(body)
    return body


def parse_profile_approval_document(body: bytes) -> dict[str, Any]:
    """Validate exact canonical bytes and the closed document schema."""

    value = _canonical_json(body, MAX_DOCUMENT_BYTES, "approval document")
    _require(set(value) == DOCUMENT_FIELDS, "approval document fields are invalid")
    _require(
        value["schema"] == DOCUMENT_SCHEMA
        and type(value["coding_contract_version"]) is int
        and value["coding_contract_version"] == 2,
        "approval document schema is invalid",
    )
    _require(
        value["shadow_only"] is True and value["weight_eligible"] is False,
        "approval document is not shadow-only",
    )
    _require(
        _index(value["catalog_index"])
        and value["language"] in LANGUAGE_PROFILES
        and _revision(value["source_revision"])
        and _image_digest(value["image_digest"])
        and _identifier(value["corpus_release_id"])
        and _identifier(value["task_version_id"])
        and all(_digest(value[name]) for name in DOCUMENT_SHA256_FIELDS),
        "approval document values are invalid",
    )
    return value


def verify_profile_approval(
    *,
    document: bytes,
    signature: bytes,
    curator_public_key_path: Path,
    curator_signing_key_sha256: str,
) -> VerifiedApproval:
    """Accept only a canonical document with a valid pinned-curator signature."""

    from cryptography.exceptions import InvalidSignature

    from ditto.api_server.coding_hippius_publication import (
        HippiusPrivateInputPublicationError,
        load_curator_signing_public_key,
    )

    _require(
        _digest(curator_signing_key_sha256), "pinned curator key digest is invalid"
    )
    value = parse_profile_approval_document(document)
    _require(
        isinstance(signature, bytes) and len(signature) == SIGNATURE_BYTES,
        "curator signature is missing or malformed",
    )
    try:
        public_key, key_sha256 = load_curator_signing_public_key(
            curator_public_key_path
        )
    except HippiusPrivateInputPublicationError:
        raise ProfileApprovalError("curator public key is invalid") from None
    _require(
        key_sha256 == curator_signing_key_sha256,
        "curator public key differs from the pinned identity",
    )
    _require(
        value["curator_signing_key_sha256"] == key_sha256,
        "approval document names another curator key",
    )
    try:
        public_key.verify(signature, document)
    except InvalidSignature:
        raise ProfileApprovalError("curator signature does not verify") from None
    return VerifiedApproval(
        document=value,
        document_sha256=_sha(document),
        signature_sha256=_sha(signature),
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ProfileApprovalError("arguments are invalid")


def _read(path: Path, maximum: int) -> bytes:
    _require(path.is_absolute(), "input path must be absolute")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ProfileApprovalError("input file is unreadable") from None
    try:
        info = os.fstat(descriptor)
        _require(
            stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum,
            "input file is not a bounded regular file",
        )
        body = bytearray()
        while len(body) <= maximum:
            chunk = os.read(descriptor, maximum + 1 - len(body))
            if not chunk:
                break
            body.extend(chunk)
    finally:
        os.close(descriptor)
    _require(0 < len(body) <= maximum, "input file is not a bounded regular file")
    return bytes(body)


def _read_profiles(directory: Path) -> tuple[bytes, bytes, bytes]:
    _require(
        directory.is_absolute()
        and not directory.is_symlink()
        and directory.is_dir()
        and sorted(os.listdir(directory)) == sorted(PROFILE_FILES),
        "profile directory is not an exact helper output",
    )
    execution, grading, receipt = PROFILE_FILES
    return (
        _read(directory / execution, 16384),
        _read(directory / grading, 65536),
        _read(directory / receipt, 1 << 16),
    )


def _write_new(path: Path, body: bytes) -> None:
    parent = path.parent
    _require(
        path.is_absolute()
        and not os.path.lexists(path)
        and not parent.is_symlink()
        and parent.is_dir()
        and stat.S_IMODE(parent.stat().st_mode) & 0o077 == 0,
        "output must be new in a protected directory",
    )
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except OSError:
        raise ProfileApprovalError("output cannot be created") from None
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)


def _build(argv: list[str]) -> str:
    from ditto.api_server.coding_hippius_publication import (
        HippiusPrivateInputPublicationError,
        load_curator_signing_public_key,
    )

    parser = _Parser(add_help=False)
    for name in (
        "request",
        "profiles",
        "payload-authority",
        "registration",
        "release-index",
        "native-approval",
        "native-summary",
        "native-provenance",
        "curator-public-key",
        "output",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args(argv)
    execution, grading, receipt = _read_profiles(args.profiles)
    try:
        _public_key, key_sha256 = load_curator_signing_public_key(
            args.curator_public_key
        )
    except HippiusPrivateInputPublicationError:
        raise ProfileApprovalError("curator public key is invalid") from None
    body = build_profile_approval(
        ApprovalInputs(
            request=_read(args.request, 1 << 16),
            execution_profile=execution,
            grading_profile=grading,
            profile_receipt=receipt,
            payload_authority=_read(args.payload_authority, 8 << 20),
            registration=_read(args.registration, 64 << 10),
            release_index=_read(args.release_index, 64 << 10),
            native_approval=_read(args.native_approval, 65536),
            native_summary=_read(args.native_summary, 1 << 20),
            native_provenance=_read(args.native_provenance, 1 << 20),
        ),
        curator_signing_key_sha256=key_sha256,
    )
    _write_new(args.output, body)
    return f"approved=false\ndocument_sha256={_sha(body)}\n"


def _verify(argv: list[str]) -> str:
    parser = _Parser(add_help=False)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--curator-public-key", type=Path, required=True)
    parser.add_argument("--curator-signing-key-sha256", required=True)
    args = parser.parse_args(argv)
    document = _read(args.document, MAX_DOCUMENT_BYTES)
    try:
        signature = _read(args.signature, SIGNATURE_BYTES)
    except ProfileApprovalError:
        raise ProfileApprovalError(
            "curator signature is missing or malformed"
        ) from None
    verified = verify_profile_approval(
        document=document,
        signature=signature,
        curator_public_key_path=args.curator_public_key,
        curator_signing_key_sha256=args.curator_signing_key_sha256,
    )
    digests = {
        "document_sha256": verified.document_sha256,
        "signature_sha256": verified.signature_sha256,
        "image_digest": verified.document["image_digest"],
        **{name: verified.document[name] for name in DOCUMENT_SHA256_FIELDS},
    }
    return "".join(f"{name}={value}\n" for name, value in digests.items())


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        _require(
            bool(arguments) and arguments[0] in {"build", "verify"},
            "arguments are invalid",
        )
        command = _build if arguments[0] == "build" else _verify
        output = command(arguments[1:])
    except ProfileApprovalError as error:
        print(f"hosted profile approval rejected: {error}", file=sys.stderr)
        return 70
    except Exception:
        print("hosted profile approval rejected: internal error", file=sys.stderr)
        return 70
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
