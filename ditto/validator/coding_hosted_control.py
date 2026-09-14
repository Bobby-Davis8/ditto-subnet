"""Default-off validator command for one Platform-hosted shadow Coding assignment.

Run it inside the validator container so the configured hotkey wallet is used in
place: the keypair is loaded by ``load_validator_keypair`` and only its public
address and ``sign`` are touched. The command never exports key material, never
retries an exchange, and is not imported by the validator worker. Every accepted
receipt is shadow-only and non-weightable.

Operations:

- ``evaluate`` signs one admission request for the pinned assignment.
- ``status`` signs a status request, optionally polling a bounded time, and
  writes a verified terminal result to a new owner-only file.
- ``acknowledge`` re-verifies that exact result file and acknowledges it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID, uuid4

import httpx

from ditto.api_models.coding_hosted import (
    HostedCodingRequest,
    HostedCodingResult,
    HostedCodingStatus,
    hosted_message_digest,
    hosted_signing_bytes,
)
from ditto.validator.coding_hosted import (
    MAX_HOSTED_RESULT_BYTES,
    HostedResultExpectation,
    SignatureVerifier,
)
from ditto.validator.coding_hosted_transport import HostedCodingTransport

ENABLED_ENV = "VALIDATOR_CODING_HOSTED_CONTROL_ENABLED"
PLATFORM_HOTKEY_ENV = "VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY"
ASSIGNMENT_SCHEMA = "dittobench-coding-hosted-assignment-v2"
REQUEST_LIFETIME_SECONDS = 60
POLL_INTERVAL_SECONDS = 20
MAX_WAIT_SECONDS = 3600
MAX_ASSIGNMENT_BYTES = 16384
EXIT_REFUSED = 70

_HOTKEY = re.compile(r"[1-9A-HJ-NP-Za-km-z]{47,48}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TRUTHY = {"1", "true", "yes"}
# The exact Platform assignment projection (``HostedAssignmentAuthority``).
_ASSIGNMENT_FIELDS = {
    "schema": str,
    "coding_contract_version": int,
    "shadow_only": bool,
    "weight_eligible": bool,
    "evaluation_id": UUID,
    "attempt_id": UUID,
    "release_row_id": UUID,
    "registration_sha256": str,
    "agent_id": UUID,
    "validator_hotkey": str,
    "artifact_sha256": str,
    "screened_image_sha256": str,
    "selection_sha256": str,
    "policy_sha256": str,
    "execution_profile_sha256": str,
    "grading_profile_sha256": str,
    "deadline_unix": int,
}


class HostedControlCommandError(RuntimeError):
    """A fixed, redacted refusal. It never carries remote or private bytes."""


@dataclass(frozen=True)
class HostedAssignment:
    evaluation_id: UUID
    attempt_id: UUID
    validator_hotkey: str
    artifact_sha256: str
    assignment_sha256: str
    policy_sha256: str
    execution_profile_sha256: str
    grading_profile_sha256: str
    deadline_unix: int


def assignment_sha256(projection: dict[str, Any]) -> str:
    """Hash the projection exactly as Platform's ``coding_canonical_sha256``."""
    body = (
        (
            json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .encode()
    )
    if len(body) > MAX_ASSIGNMENT_BYTES:
        raise HostedControlCommandError("hosted assignment is invalid")
    return hashlib.sha256(body).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite number")


def load_assignment(path: Path, pinned_sha256: str) -> HostedAssignment:
    """Load the operator-copied assignment projection and bind it to its digest."""
    try:
        if not _SHA256.fullmatch(pinned_sha256):
            raise ValueError("pin")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ASSIGNMENT_BYTES:
            raise ValueError("file")
        projection: Any = json.loads(
            path.read_bytes(),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
        if not isinstance(projection, dict) or set(projection) != set(
            _ASSIGNMENT_FIELDS
        ):
            raise ValueError("fields")
        for name, kind in _ASSIGNMENT_FIELDS.items():
            value = projection[name]
            if kind is UUID:
                if type(value) is not str or str(UUID(value)) != value:
                    raise ValueError("uuid")
                if UUID(value).int == 0:
                    raise ValueError("uuid")
            elif type(value) is not kind:
                raise ValueError("type")
            elif name.endswith("_sha256") and not (
                isinstance(value, str) and _SHA256.fullmatch(value)
            ):
                raise ValueError("digest")
        if (
            projection["schema"] != ASSIGNMENT_SCHEMA
            or projection["coding_contract_version"] != 2
            or projection["shadow_only"] is not True
            or projection["weight_eligible"] is not False
            or not _HOTKEY.fullmatch(projection["validator_hotkey"])
            or not 0 < projection["deadline_unix"] <= 253402300799
            or assignment_sha256(projection) != pinned_sha256
        ):
            raise ValueError("authority")
    except HostedControlCommandError:
        raise
    except Exception:
        raise HostedControlCommandError("hosted assignment is invalid") from None
    return HostedAssignment(
        evaluation_id=UUID(projection["evaluation_id"]),
        attempt_id=UUID(projection["attempt_id"]),
        validator_hotkey=projection["validator_hotkey"],
        artifact_sha256=projection["artifact_sha256"],
        assignment_sha256=pinned_sha256,
        policy_sha256=projection["policy_sha256"],
        execution_profile_sha256=projection["execution_profile_sha256"],
        grading_profile_sha256=projection["grading_profile_sha256"],
        deadline_unix=projection["deadline_unix"],
    )


@dataclass(frozen=True)
class HostedControlContext:
    """Configuration and the in-place signing key for one command."""

    platform_origin: str
    platform_hotkey: str
    keypair: Any
    verifier: SignatureVerifier


def load_context(
    environ: dict[str, str] | os._Environ[str],
    *,
    pinned_validator_hotkey: str,
    load_config: Callable[[], Any],
    load_keypair: Callable[[Any], Any],
    make_verifier: Callable[[str], SignatureVerifier],
) -> HostedControlContext:
    """Refuse unless enabled, trusted and signing as the pinned validator."""
    if environ.get(ENABLED_ENV, "").strip().lower() not in _TRUTHY:
        raise HostedControlCommandError("hosted Coding validator control is disabled")
    platform_hotkey = environ.get(PLATFORM_HOTKEY_ENV, "").strip()
    if not _HOTKEY.fullmatch(platform_hotkey):
        raise HostedControlCommandError(
            "hosted Coding Platform signer is not configured"
        )
    try:
        config = load_config()
        keypair = load_keypair(config)
        address = keypair.ss58_address
    except Exception:
        raise HostedControlCommandError(
            "validator signing configuration is invalid"
        ) from None
    if not (
        _HOTKEY.fullmatch(pinned_validator_hotkey)
        and config.validator_hotkey == pinned_validator_hotkey
        and address == pinned_validator_hotkey
    ):
        raise HostedControlCommandError(
            "validator signing key does not match the pinned hotkey"
        )
    if platform_hotkey == pinned_validator_hotkey:
        raise HostedControlCommandError(
            "hosted Coding Platform signer is not configured"
        )
    return HostedControlContext(
        platform_origin=config.platform_api_url,
        platform_hotkey=platform_hotkey,
        keypair=keypair,
        verifier=make_verifier(platform_hotkey),
    )


def signed_request(
    context: HostedControlContext,
    assignment: HostedAssignment,
    operation: str,
    *,
    now: int,
    result_sha256: str | None = None,
) -> HostedCodingRequest:
    unsigned = HostedCodingRequest.model_validate(
        {
            "schema": "dittobench-coding-hosted-request-v2",
            "coding_contract_version": 2,
            "shadow_only": True,
            "weight_eligible": False,
            "evaluation_id": assignment.evaluation_id,
            "validator_hotkey": assignment.validator_hotkey,
            "artifact_sha256": assignment.artifact_sha256,
            "assignment_sha256": assignment.assignment_sha256,
            "policy_sha256": assignment.policy_sha256,
            "operation": operation,
            "result_sha256": result_sha256,
            "nonce": uuid4(),
            "issued_at_unix": now,
            "expires_at_unix": now + REQUEST_LIFETIME_SECONDS,
            "signature": "0" * 128,
        }
    )
    signature = context.keypair.sign(hosted_signing_bytes(unsigned))
    return HostedCodingRequest.model_validate(
        {
            **unsigned.model_dump(mode="json", by_alias=True),
            "signature": bytes(signature).hex(),
        }
    )


def expectation(
    context: HostedControlContext, assignment: HostedAssignment, request_sha256: str
) -> HostedResultExpectation:
    return HostedResultExpectation(
        evaluation_id=assignment.evaluation_id,
        attempt_id=assignment.attempt_id,
        validator_hotkey=assignment.validator_hotkey,
        platform_hotkey=context.platform_hotkey,
        artifact_sha256=assignment.artifact_sha256,
        assignment_sha256=assignment.assignment_sha256,
        policy_sha256=assignment.policy_sha256,
        execution_profile_sha256=assignment.execution_profile_sha256,
        grading_profile_sha256=assignment.grading_profile_sha256,
        request_sha256=request_sha256,
    )


def _canonical(value: HostedCodingResult) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _write_result(path: Path, result: HostedCodingResult) -> None:
    try:
        parent = path.parent.lstat()
        if (
            not path.is_absolute()
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise ValueError("result directory")
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
    except Exception:
        raise HostedControlCommandError("result output path is invalid") from None
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical(result))
        handle.flush()
        os.fsync(handle.fileno())


def _read_result(path: Path) -> HostedCodingResult:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or not 0 < info.st_size <= MAX_HOSTED_RESULT_BYTES
            ):
                raise ValueError("result file")
            body = handle.read(MAX_HOSTED_RESULT_BYTES + 1)
        result = HostedCodingResult.model_validate_json(body)
        if _canonical(result) != body:
            raise ValueError("noncanonical")
    except Exception:
        raise HostedControlCommandError("result file is invalid") from None
    return result


async def run_command(
    args: argparse.Namespace,
    context: HostedControlContext,
    *,
    clock: Callable[[], int] = lambda: int(time.time()),
    sleep: Callable[[float], Any] = asyncio.sleep,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    assignment = load_assignment(args.assignment, args.assignment_sha256)
    if assignment.validator_hotkey != context.keypair.ss58_address:
        raise HostedControlCommandError(
            "hosted assignment belongs to another validator"
        )
    async with HostedCodingTransport(
        platform_origin=context.platform_origin,
        trusted_verifiers={context.platform_hotkey: context.verifier},
        clock=clock,
        transport=transport,
    ) as client:
        if args.operation == "acknowledge":
            result = _read_result(args.result)
            request = signed_request(
                context,
                assignment,
                "acknowledge",
                now=clock(),
                result_sha256=hosted_message_digest(result),
            )
            await client.acknowledge(
                request=request,
                result=result,
                expected=expectation(context, assignment, result.request_sha256),
            )
            return {
                "operation": "acknowledge",
                "acknowledged": True,
                "result_sha256": hosted_message_digest(result),
                "shadow_only": True,
                "weight_eligible": False,
            }

        if args.operation == "evaluate" and clock() >= assignment.deadline_unix:
            raise HostedControlCommandError("hosted assignment has expired")
        if args.operation == "status" and args.result_out is None:
            raise HostedControlCommandError("result output path is required")
        wait_until = clock() + (args.wait_seconds if args.operation == "status" else 0)
        while True:
            request = signed_request(context, assignment, args.operation, now=clock())
            received = await client.exchange(
                request=request,
                expected=expectation(
                    context, assignment, hosted_message_digest(request)
                ),
            )
            if isinstance(received, HostedCodingResult):
                if args.result_out is None:
                    raise HostedControlCommandError("result output path is required")
                _write_result(args.result_out, received)
                return {
                    "operation": args.operation,
                    "terminal": True,
                    "outcome": received.outcome,
                    "result_sha256": hosted_message_digest(received),
                    "evidence_sha256": received.evidence_sha256,
                    "shadow_only": True,
                    "weight_eligible": False,
                }
            assert isinstance(received, HostedCodingStatus)
            if clock() + POLL_INTERVAL_SECONDS > wait_until:
                return {
                    "operation": args.operation,
                    "terminal": False,
                    "state": received.state,
                    "shadow_only": True,
                    "weight_eligible": False,
                }
            await sleep(POLL_INTERVAL_SECONDS)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise HostedControlCommandError("hosted control arguments are invalid")


def parser() -> argparse.ArgumentParser:
    root = _Parser(prog="python -m ditto.validator.coding_hosted_control")
    commands = root.add_subparsers(dest="operation", required=True)
    for name in ("evaluate", "status", "acknowledge"):
        command = commands.add_parser(name)
        command.add_argument("--validator-hotkey", required=True)
        command.add_argument("--assignment", type=Path, required=True)
        command.add_argument("--assignment-sha256", required=True)
        if name == "status":
            command.add_argument("--result-out", type=Path, required=True)
            command.add_argument("--wait-seconds", type=int, default=0)
        elif name == "acknowledge":
            command.add_argument("--result", type=Path, required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        args.result_out = getattr(args, "result_out", None)
        if (
            args.operation == "status"
            and not 0 <= args.wait_seconds <= MAX_WAIT_SECONDS
        ):
            raise HostedControlCommandError("hosted control arguments are invalid")
        import bittensor

        from ditto.validator.config import parse_validator_config_from_env
        from ditto.validator.signing import load_validator_keypair

        context = load_context(
            os.environ,
            pinned_validator_hotkey=args.validator_hotkey,
            load_config=parse_validator_config_from_env,
            load_keypair=load_validator_keypair,
            make_verifier=lambda address: bittensor.Keypair(ss58_address=address),
        )
        summary = asyncio.run(run_command(args, context))
    except HostedControlCommandError as error:
        print(str(error), file=sys.stderr)
        return EXIT_REFUSED
    except Exception:
        print("hosted Coding validator control failed", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
