"""Atomic private artifact pinning. No lease, public reveal or activation rights.

Callers own their transaction; never issue a lease until it has committed.
Transform/semantic validation must happen before this module is called, outside
any long-lived database transaction. A receipt is provenance, not qualification.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.db.models import PrivateBenchmarkDataset

MAX_ARTIFACT_BYTES = 32 << 20
MAX_RECEIPT_BYTES = 32 << 20  # Full surface receipts plus bounded retry history.
_DIGEST = re.compile(r"[0-9a-f]{64}")


class PrivateDatasetError(ValueError):
    """Sanitized private-artifact validation error."""


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class PrivateDatasetIdentity:
    """Scope is a trusted work-set identity, shared by both sides of CRN.

    It must not be derived from a miner-selected name, or reused for unrelated
    work. A transformation-profile change deliberately creates a new identity.
    """

    scope: str
    seed: int
    run_size: str
    transform_profile_sha256: str
    bench_version: int = 13
    generation_mode: str = "legacy-rewrite"

    def digest(self) -> str:
        if (
            self.bench_version != 13
            or self.generation_mode not in {"legacy-rewrite", "fact-world-v1"}
            or type(self.seed) is not int
            or not 0 <= self.seed < 2**63
            or self.run_size not in {"small", "medium", "full"}
            or not 1 <= len(self.scope) <= 256
            or any(ord(c) < 32 for c in self.scope)
            or not _DIGEST.fullmatch(self.transform_profile_sha256)
        ):
            raise PrivateDatasetError("invalid private dataset identity")
        parts = [
            "private-benchmark-dataset-v1",
            self.scope,
            self.bench_version,
            self.seed,
            self.run_size,
            self.transform_profile_sha256,
        ]
        if self.generation_mode != "legacy-rewrite":
            parts[0] = "private-benchmark-dataset-v2"
            parts.append(self.generation_mode)
        return _sha(
            json.dumps(
                parts,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        )


@dataclass(frozen=True)
class PrivateDatasetBytes:
    dataset_id: UUID
    identity: PrivateDatasetIdentity
    dataset_sha256: str
    base_sha256: str
    validation_receipt_sha256: str
    dataset_bytes: bytes = field(repr=False)
    base_bytes: bytes = field(repr=False)
    validation_receipt_bytes: bytes = field(repr=False)


def _object(body: bytes, maximum: int) -> dict:
    if not 0 < len(body) <= maximum:
        raise PrivateDatasetError("private artifact size invalid")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PrivateDatasetError("duplicate private artifact field")
            result[key] = value
        return result

    def constant(_value):
        raise PrivateDatasetError("non-finite private artifact value")

    try:
        value = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise PrivateDatasetError("private artifact JSON invalid") from None
    if not isinstance(value, dict):
        raise PrivateDatasetError("private artifact must be an object")
    return value


def _validate(identity, base_bytes, dataset_bytes, receipt_bytes) -> None:
    identity.digest()
    if identity.generation_mode == "fact-world-v1":
        _validate_fact(identity, base_bytes, dataset_bytes, receipt_bytes)
        return
    base = _object(base_bytes, MAX_ARTIFACT_BYTES)
    dataset = _object(dataset_bytes, MAX_ARTIFACT_BYTES)
    for obj in (base, dataset):
        if (
            type(obj.get("seed")) is not int
            or obj["seed"] != identity.seed
            or obj.get("bench_version") != identity.bench_version
            or type(obj.get("surface_salt")) is not int
            or not 0 < obj["surface_salt"] < 2**64
        ):
            raise PrivateDatasetError("private artifact identity mismatch")
    if base["surface_salt"] != dataset["surface_salt"] or base == dataset:
        raise PrivateDatasetError("private artifact transformation missing or invalid")
    receipt = _object(receipt_bytes, MAX_RECEIPT_BYTES)
    if (
        receipt.get("schema") != "private-surface-validation-v1"
        or receipt.get("accepted") is not True
        or receipt.get("base_sha256") != _sha(base_bytes)
        or receipt.get("dataset_sha256") != _sha(dataset_bytes)
        or receipt.get("transform_profile_sha256") != identity.transform_profile_sha256
    ):
        raise PrivateDatasetError("private artifact validation receipt mismatch")


FACT_GENERATION_REVISION = "v13-fact-generation-v8"


def _validate_fact(identity, manifest_bytes, dataset_bytes, receipt_bytes) -> None:
    # base_bytes is the existing private source-provenance storage column. In
    # fact mode it contains generation.json, NOT a legacy pre-rewrite dataset.
    manifest = _object(manifest_bytes, MAX_ARTIFACT_BYTES)
    dataset = _object(dataset_bytes, MAX_ARTIFACT_BYTES)
    receipt = _object(receipt_bytes, MAX_RECEIPT_BYTES)
    generation = dataset.get("fact_generation")
    if not isinstance(generation, dict):
        raise PrivateDatasetError("private fact generation provenance missing")
    for key in ("world_seed", "presentation_seed"):
        value = manifest.get(key)
        if (
            type(value) is not int
            or not -(2**63) <= value < 2**63
            or value == 0
            or type(generation.get(key)) is not int
            or generation[key] != value
        ):
            raise PrivateDatasetError("private fact entropy mismatch")
    events = generation.get("events")
    if (
        manifest.get("revision") != FACT_GENERATION_REVISION
        or generation.get("revision") != FACT_GENERATION_REVISION
        or manifest["world_seed"] in {identity.seed, manifest["presentation_seed"]}
        or type(manifest.get("seed")) is not int
        or manifest["seed"] != identity.seed
        or type(dataset.get("seed")) is not int
        or dataset["seed"] != identity.seed
        or type(dataset.get("bench_version")) is not int
        or dataset["bench_version"] != 13
        or type(dataset.get("surface_salt", 0)) is not int
        or dataset.get("surface_salt", 0) != 0
        or manifest.get("run_size") != identity.run_size
        or manifest.get("profile_sha256") != identity.transform_profile_sha256
        or manifest.get("qualified") is not False
        or not isinstance(events, list)
        or not events
        or receipt.get("schema") != "private-fact-generation-validation-v1"
        or receipt.get("accepted") is not True
        or receipt.get("qualified") is not False
        or receipt.get("replay_verified") is not True
        or receipt.get("generation_revision") != FACT_GENERATION_REVISION
        or receipt.get("generation_sha256") != _sha(manifest_bytes)
        or receipt.get("dataset_sha256") != _sha(dataset_bytes)
        or receipt.get("transform_profile_sha256") != identity.transform_profile_sha256
        or receipt.get("run_size") != identity.run_size
        or type(receipt.get("render_event_count")) is not int
        or receipt["render_event_count"] != len(events)
    ):
        raise PrivateDatasetError("private fact generation receipt mismatch")
    # Native replay is authoritative. Counterfactual programs reuse token plans
    # with different bindings, yielding extra checks, not alternating pairs.
    # Every newly authored plan must still be checked immediately. Native replay
    # verifies the exact cached-plan reuse and complete generation schedule.
    pending = None
    have_program_plan = False
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("phase"), str):
            raise PrivateDatasetError("private fact render audit invalid")
        for key in ("request_sha256", "plan_sha256"):
            if not isinstance(event.get(key), str) or not _DIGEST.fullmatch(event[key]):
                raise PrivateDatasetError("private fact render audit invalid")
        phase = event["phase"]
        if pending is not None:
            expected = "check" if pending["phase"] == "plan" else "document_check"
            if (
                phase != expected
                or event["request_sha256"] != pending["request_sha256"]
            ):
                raise PrivateDatasetError("private fact render audit invalid")
            have_program_plan = have_program_plan or expected == "check"
            pending = None
        elif phase in {"plan", "document_plan"}:
            pending = event
        elif phase != "check" or not have_program_plan:
            raise PrivateDatasetError("private fact render audit invalid")
    if pending is not None:
        raise PrivateDatasetError("private fact render audit incomplete")


def _read(
    row: PrivateBenchmarkDataset, identity: PrivateDatasetIdentity
) -> PrivateDatasetBytes:
    if (
        row.identity_sha256 != identity.digest()
        or row.generation_mode != identity.generation_mode
        or row.scope != identity.scope
        or row.seed != identity.seed
        or row.bench_version != identity.bench_version
        or row.run_size != identity.run_size
        or row.transform_profile_sha256 != identity.transform_profile_sha256
        or _sha(row.base_bytes) != row.base_sha256
        or _sha(row.dataset_bytes) != row.dataset_sha256
        or _sha(row.validation_receipt_bytes) != row.validation_receipt_sha256
    ):
        raise PrivateDatasetError("stored private artifact integrity mismatch")
    _validate(identity, row.base_bytes, row.dataset_bytes, row.validation_receipt_bytes)
    return PrivateDatasetBytes(
        dataset_id=row.dataset_id,
        identity=identity,
        dataset_sha256=row.dataset_sha256,
        base_sha256=row.base_sha256,
        validation_receipt_sha256=row.validation_receipt_sha256,
        dataset_bytes=row.dataset_bytes,
        base_bytes=row.base_bytes,
        validation_receipt_bytes=row.validation_receipt_bytes,
    )


async def find_private_dataset(
    session: AsyncSession, *, identity: PrivateDatasetIdentity
) -> PrivateDatasetBytes | None:
    row = await session.scalar(
        select(PrivateBenchmarkDataset).where(
            PrivateBenchmarkDataset.identity_sha256 == identity.digest()
        )
    )
    return _read(row, identity) if row is not None else None


async def pin_private_dataset(
    session: AsyncSession,
    *,
    identity: PrivateDatasetIdentity,
    base_bytes: bytes,
    dataset_bytes: bytes,
    validation_receipt_bytes: bytes,
) -> PrivateDatasetBytes:
    """First committed candidate wins; every concurrent loser reads its bytes.

    READ COMMITTED is required (Platform's default). Do not report success from
    an uncommitted transaction. A lost commit response is recovered by lookup,
    not by replacing the existing dataset with another provider response.
    """
    _validate(identity, base_bytes, dataset_bytes, validation_receipt_bytes)
    await _insert_private(
        session,
        insert(PrivateBenchmarkDataset)
        .values(
            dataset_id=uuid4(),
            generation_mode=identity.generation_mode,
            identity_sha256=identity.digest(),
            scope=identity.scope,
            bench_version=identity.bench_version,
            seed=identity.seed,
            run_size=identity.run_size,
            transform_profile_sha256=identity.transform_profile_sha256,
            validation_receipt_sha256=_sha(validation_receipt_bytes),
            validation_receipt_bytes=validation_receipt_bytes,
            base_sha256=_sha(base_bytes),
            dataset_sha256=_sha(dataset_bytes),
            base_bytes=base_bytes,
            dataset_bytes=dataset_bytes,
        )
        .on_conflict_do_nothing(index_elements=["identity_sha256"]),
    )
    winner = await find_private_dataset(session, identity=identity)
    if winner is None:
        raise PrivateDatasetError("private artifact pin unavailable")
    return winner


async def _insert_private(session: AsyncSession, statement) -> None:
    # Driver errors can contain a failing-row DETAIL even when SQLAlchemy bind
    # logging is disabled. Do not propagate those bytes into HTTP error logs.
    try:
        await session.execute(statement)
    except SQLAlchemyError:
        raise PrivateDatasetError("private artifact storage unavailable") from None
