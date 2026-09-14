"""Fixed-host materializer for one hosted-v2 Platform runtime configuration.

Authority comes from committed Platform state, read in one read-only snapshot.
Transferred public authorities are selected by the assignment's own digests and
verified before use. Owner-only secret files are referenced at fixed paths and
checked by metadata only; the worker's PostgreSQL environment is the single
exception, parsed in memory to open that read-only connection. This module never
starts a unit, admits or creates an assignment, issues a grant, contacts a
provider or writes to PostgreSQL. The runtime rechecks everything under locks.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import json
import os
import platform
import pwd
import re
import stat
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.agent_status import SCOREABLE_AGENT_STATUSES
from ditto.api_models.coding_hosted_budget import HostedBudgetProfile
from ditto.api_models.coding_hosted_inference import HostedInferencePolicy
from ditto.api_models.coding_hosted_runtime import HostedPlatformRuntimeInput
from ditto.api_models.coding_inference import (
    _decode_json_document,
    parse_coding_inference_json,
)
from ditto.api_models.coding_private_v2_registry import (
    CodingPrivateV2RegistrationAuthority,
)
from ditto.api_server.coding_hippius_custody import RsaOaepHippiusEvidenceKeyWrapper
from ditto.api_server.coding_hippius_probe import load_hippius_probe_receipt
from ditto.api_server.coding_hosted_authoring_evidence import canonical, sha
from ditto.api_server.coding_hosted_budget import ProfiledBudgetEstimator
from ditto.api_server.coding_hosted_installed_worker import (
    NAME as WORKER_NAME,
)
from ditto.api_server.coding_hosted_installed_worker import (
    ROOT as RUNTIME_PARENT,
)
from ditto.api_server.coding_hosted_installed_worker import (
    require_installed_worker,
)
from ditto.api_server.coding_hosted_runtime_config import postgres_config
from ditto.api_server.coding_hosted_runtime_io import (
    private_directory,
    protected_helper,
    read_json,
    read_private,
    write_private,
)
from ditto.api_server.coding_private_v2_retrieval import PrivateV2InputAuthority
from ditto.db.factory import create_db_engine, create_session_maker
from ditto.db.models import (
    Agent,
    CodingHostedAssignment,
    CodingHostedPrivateTask,
    CodingPrivateV2Release,
    CodingPrivateV2ReleaseEvent,
)
from ditto.db.queries.coding_hosted_admission import HostedAssignmentAuthority, _now
from ditto.db.queries.coding_hosted_private import _selection_matches
from ditto_screening_protocol import SCREENING_POLICY_VERSION

RECEIPT_SCHEMA = "dittobench-coding-hosted-attempt-config-receipt-v2"
WORKER_USER = "ditto-coding-hosted"
HOSTNAME = "ditto-coding-hosted-v2"
HOME = Path("/var/lib/ditto-coding-hosted")
PREREQUISITES = Path("/usr/local/lib/ditto-coding-hosted/host-prerequisites.json")
DOCKER = Path("/usr/bin/docker")
DOCKER_SOCKET = Path("/run/ditto-coding-hosted/docker.sock")
CUSTODY_SOCKET = Path("/run/ditto-coding-custody/custody.sock")
PREREQUISITES_SCHEMA = "dittobench-coding-hosted-host-prerequisites-v2"
EXECUTOR_LANGUAGES = ("go", "node", "python", "rust")
# Leave room for custody prepare/start, connectivity and worker start.
MINIMUM_REMAINING_SECONDS = 300
# The runtime invocation's bounded post-deadline finalization window; evidence
# publication inside it still requires a probe receipt younger than 24 hours.
FINALIZATION_SECONDS = 3600
PROBE_MAX_AGE_SECONDS = 86400
UNITS = ("ditto-coding-hosted-worker.service", "ditto-coding-custody@*.service")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_NETWORK = re.compile(r"[a-z0-9_][a-z0-9_-]{0,127}")
_PORT = re.compile(r"[1-9][0-9]{3,4}")
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
# Go net.IP.IsPrivate for IPv4, as in the host-prerequisites record check.
_PRIVATE = tuple(
    ipaddress.IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class HostedAttemptConfigError(ValueError):
    """Fixed refusal stage; never carries a path, value or child output."""


def require(condition: bool, stage: str) -> None:
    if not condition:
        raise HostedAttemptConfigError(stage)


@contextmanager
def refusal(stage: str):
    try:
        yield
    except HostedAttemptConfigError:
        raise
    except Exception:
        raise HostedAttemptConfigError(stage) from None


@dataclass(frozen=True)
class AttemptRequest:
    evaluation_id: UUID
    assignment_sha256: str
    probe_receipt_sha256: str
    evidence_wrapping_key_sha256: str

    @classmethod
    def parse(
        cls,
        *,
        evaluation_id: str,
        assignment_sha256: str,
        probe_receipt_sha256: str,
        evidence_wrapping_key_sha256: str,
    ) -> AttemptRequest:
        with refusal("attempt request invalid"):
            parsed = UUID(evaluation_id)
            require(
                parsed.int != 0 and str(parsed) == evaluation_id,
                "attempt request invalid",
            )
            for value in (
                assignment_sha256,
                probe_receipt_sha256,
                evidence_wrapping_key_sha256,
            ):
                require(
                    type(value) is str and _DIGEST.fullmatch(value) is not None,
                    "attempt request invalid",
                )
            return cls(
                parsed,
                assignment_sha256,
                probe_receipt_sha256,
                evidence_wrapping_key_sha256,
            )


@dataclass(frozen=True)
class HostLayout:
    """Fixed host locations; the CLI never accepts an override for any of them."""

    home: Path
    worker_executable: Path
    python_executable: Path
    prerequisites_file: Path
    docker_executable: Path
    docker_socket: Path
    custody_socket: Path
    # Owner required for host-installed public files (root in production).
    trusted_uid: int = 0

    @property
    def attempts(self) -> Path:
        return self.home / "attempts"

    @property
    def inbox(self) -> Path:
        return self.home / "inbox"

    @property
    def private(self) -> Path:
        return self.home / "private"

    @property
    def evidence_public_key(self) -> Path:
        return self.home / "authority" / "evidence-public.pem"

    @property
    def unwrap_executable(self) -> Path:
        return self.home / "custody" / "unwrap"

    def release(self, registration_sha256: str) -> Path:
        return self.home / "release" / registration_sha256

    def secrets(self) -> dict[str, tuple[Path, int]]:
        return {
            "postgres_environment_file": (
                self.private / "postgres-environment.json",
                128 << 10,
            ),
            "hippius_environment_file": (
                self.private / "hippius-environment.json",
                65536,
            ),
            "image_storage_file": (self.private / "image-storage.json", 65536),
            "provider_key_file": (self.private / "provider-key", 4096),
        }


@dataclass(frozen=True)
class LaunchableAssignment:
    authority: HostedAssignmentAuthority
    registration: CodingPrivateV2RegistrationAuthority
    reader_authority_sha256: str
    catalog_index: int
    max_patch_bytes: int


def production_layout() -> HostLayout:
    """Bind the dedicated host, worker identity and running installed runtime."""
    with refusal("hosted attempt host refused"):
        user = pwd.getpwnam(WORKER_USER)
        require(
            os.getuid() == os.geteuid() == user.pw_uid >= 1000
            and os.getgid() == os.getegid() == user.pw_gid
            and set(os.getgroups()) <= {user.pw_gid}
            and platform.node() == HOSTNAME
            and platform.system() == "Linux",
            "hosted attempt host refused",
        )
        venv = Path(sys.prefix)
        prefix = venv.parents[2]
        require(
            prefix.parent == RUNTIME_PARENT
            and re.fullmatch(r"[0-9a-f]{40}", prefix.name) is not None
            and venv == prefix / "apps/platform/.venv"
            and Path(__file__).resolve().is_relative_to(prefix),
            "hosted attempt host refused",
        )
        worker = prefix / "bin" / WORKER_NAME
        require_installed_worker(worker)
        return HostLayout(
            home=HOME,
            worker_executable=worker,
            python_executable=venv / "bin/python",
            prerequisites_file=PREREQUISITES,
            docker_executable=DOCKER,
            docker_socket=DOCKER_SOCKET,
            custody_socket=CUSTODY_SOCKET,
        )


def idle_units(listing: bytes) -> bool:
    """`systemctl list-units --plain --no-legend` columns: UNIT LOAD ACTIVE SUB."""
    for line in listing.decode("utf-8").splitlines():
        columns = line.split()
        if columns and (len(columns) < 4 or columns[2] not in {"inactive", "failed"}):
            return False
    return True


def refuse_live_units() -> None:
    with refusal("hosted worker or custody unit is live"):
        result = subprocess.run(
            [
                "/usr/bin/systemctl",
                "list-units",
                "--all",
                "--plain",
                "--no-legend",
                "--full",
                *UNITS,
            ],
            env=_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=True,
        )
        require(
            len(result.stdout) <= 65536 and idle_units(result.stdout),
            "hosted worker or custody unit is live",
        )


def unique_repository(digests: set[str], present: Callable[[str], bool]) -> str:
    """The one approved local runtime repository holding every profile image."""
    matches = [
        repository
        for repository in (
            f"coding-runtime.invalid/{language}/runtime"
            for language in EXECUTOR_LANGUAGES
        )
        if all(present(f"{repository}@{digest}") for digest in sorted(digests))
    ]
    require(len(matches) == 1, "hosted executor image unavailable")
    return matches[0]


def docker_repository(layout: HostLayout, digests: set[str]) -> str:
    def present(reference: str) -> bool:
        result = subprocess.run(
            [
                str(layout.docker_executable),
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                reference,
            ],
            env={
                **_ENV,
                "DOCKER_HOST": f"unix://{layout.docker_socket}",
                "DOCKER_CONFIG": str(layout.home / "empty-client"),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 65536:
            return False
        value = json.loads(result.stdout)
        return isinstance(value, list) and reference in value

    with refusal("hosted executor image unavailable"):
        return unique_repository(digests, present)


def secret_metadata(path: Path, maximum: int) -> None:
    """The runtime's owner-only file rules, without opening or reading the file."""
    private_directory(path.parent)
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.parent.resolve() / path.name != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or not 0 < info.st_size <= maximum
    ):
        raise ValueError("owner-only file metadata refused")


def trusted_path(path: Path, uid: int) -> os.stat_result:
    """Installed public host file owned by the trusted principal, never replaceable."""
    require(
        path.is_absolute() and path.resolve() == path, "hosted attempt host refused"
    )
    for parent in path.parents:
        info = parent.lstat()
        require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid in {0, uid}
            and (
                not info.st_mode & 0o022
                or (info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX))
            ),
            "hosted attempt host refused",
        )
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == uid
        and info.st_nlink == 1
        and not info.st_mode & 0o022,
        "hosted attempt host refused",
    )
    return info


def trusted_file(path: Path, uid: int, maximum: int) -> bytes:
    trusted_path(path, uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == uid
            and info.st_nlink == 1
            and not info.st_mode & 0o022
            and 0 < info.st_size <= maximum,
            "hosted attempt host refused",
        )
        body = source.read(maximum + 1)
        require(len(body) == info.st_size, "hosted attempt host refused")
        return body


def _endpoint(value: object) -> tuple[str, int]:
    require(type(value) is str and value.count(":") == 1, "hosted attempt host refused")
    address, port = str(value).split(":")
    require(_PORT.fullmatch(port) is not None, "hosted attempt host refused")
    ip = ipaddress.IPv4Address(address)
    require(
        str(ip) == address
        and any(ip in network for network in _PRIVATE)
        and 1024 <= int(port) <= 65535,
        "hosted attempt host refused",
    )
    return address, int(port)


def host_settings(document: object) -> dict[str, Any]:
    """Accept only the fixed host-prerequisites record, never free-form values."""
    with refusal("hosted attempt host refused"):
        require(
            isinstance(document, dict)
            and set(document)
            == {
                "schema",
                "shadow_only",
                "weight_eligible",
                "router_listen",
                "egress_network",
                "egress_proxy",
                "candidate_uid",
                "candidate_gid",
            }
            and document["schema"] == PREREQUISITES_SCHEMA
            and document["shadow_only"] is True
            and document["weight_eligible"] is False,
            "hosted attempt host refused",
        )
        assert isinstance(document, dict)
        router = _endpoint(document["router_listen"])
        proxy = document["egress_proxy"]
        require(
            type(proxy) is str and proxy.startswith("http://"),
            "hosted attempt host refused",
        )
        proxy_address, proxy_port = _endpoint(proxy.removeprefix("http://"))
        network = document["egress_network"]
        require(
            router[0] == proxy_address
            and router[1] != proxy_port
            and type(network) is str
            and _NETWORK.fullmatch(network) is not None
            and not network.startswith("ditto-job-")
            and all(
                type(document[key]) is int and 1 <= document[key] <= 65536
                for key in ("candidate_uid", "candidate_gid")
            ),
            "hosted attempt host refused",
        )
        return {
            key: document[key]
            for key in (
                "router_listen",
                "egress_network",
                "egress_proxy",
                "candidate_uid",
                "candidate_gid",
            )
        }


def verify_host(layout: HostLayout) -> dict[str, Any]:
    with refusal("hosted attempt host refused"):
        for directory in (layout.home, layout.attempts, layout.inbox):
            private_directory(directory)
        protected_helper(layout.worker_executable)
        protected_helper(layout.unwrap_executable)
        require(
            not layout.worker_executable.samefile(layout.unwrap_executable)
            and layout.python_executable.is_absolute()
            and os.access(layout.python_executable, os.X_OK)
            and layout.docker_executable.name == "docker",
            "hosted attempt host refused",
        )
        docker = trusted_path(layout.docker_executable, layout.trusted_uid)
        require(bool(docker.st_mode & stat.S_IXUSR), "hosted attempt host refused")
        socket = layout.docker_socket.lstat()
        require(
            layout.docker_socket.resolve() == layout.docker_socket
            and stat.S_ISSOCK(socket.st_mode)
            and socket.st_uid == os.geteuid()
            and stat.S_IMODE(socket.st_mode) == 0o600,
            "hosted attempt host refused",
        )
        private_directory(layout.docker_socket.parent)
        record = trusted_file(layout.prerequisites_file, layout.trusted_uid, 4096)
        return host_settings(_decode_json_document(record, maximum_bytes=4096))


def refuse_live_custody(layout: HostLayout) -> None:
    require(
        not os.path.lexists(layout.custody_socket),
        "hosted worker or custody unit is live",
    )


@asynccontextmanager
async def read_only_snapshot(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One repeatable, read-only transaction; PostgreSQL refuses any write or lock."""
    async with sessions() as session:
        try:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            yield session
        finally:
            await session.rollback()


async def read_launchable(
    sessions: async_sessionmaker[AsyncSession],
    *,
    evaluation_id: UUID,
    assignment_sha256: str,
) -> tuple[LaunchableAssignment, datetime]:
    async with asyncio.timeout(20), read_only_snapshot(sessions) as session:
        now = await _now(session)
        row = await session.get(CodingHostedAssignment, evaluation_id)
        require(row is not None, "hosted assignment unavailable")
        assert row is not None
        with refusal("hosted assignment authority differs"):
            names = HostedAssignmentAuthority.__dataclass_fields__
            values = {name: row.authority[name] for name in names}
            for name in names:
                if name.endswith("_id"):
                    values[name] = UUID(values[name])
            authority = HostedAssignmentAuthority(**values)
            require(
                authority.projection() == row.authority
                and authority.digest() == row.assignment_sha256 == assignment_sha256
                and (
                    authority.evaluation_id,
                    authority.attempt_id,
                    authority.release_row_id,
                    authority.registration_sha256,
                    authority.agent_id,
                    authority.validator_hotkey,
                    authority.artifact_sha256,
                    authority.screened_image_sha256,
                )
                == (
                    row.evaluation_id,
                    row.attempt_id,
                    row.release_row_id,
                    row.registration_sha256,
                    row.agent_id,
                    row.validator_hotkey,
                    row.artifact_sha256,
                    row.screened_image_sha256,
                )
                and row.expires_at.timestamp() == authority.deadline_unix
                and row.shadow_only is True
                and row.weight_eligible is False,
                "hosted assignment authority differs",
            )
        # The runtime requires an admitted, unstarted assignment at start. A config
        # is written only for that state, never ahead of validator admission.
        require(
            row.admitted_at is not None
            and row.started_at is None
            and row.worker_id is None
            and MINIMUM_REMAINING_SECONDS
            <= (row.expires_at - now).total_seconds()
            <= 3600,
            "hosted assignment is not launchable",
        )
        release = await session.get(CodingPrivateV2Release, row.release_row_id)
        inactive = await session.scalar(
            select(
                exists().where(
                    CodingPrivateV2ReleaseEvent.release_row_id == row.release_row_id
                )
            )
        )
        require(
            release is not None
            and not inactive
            and release.registration_sha256 == row.registration_sha256
            and release.shadow_only is True
            and release.weight_eligible is False,
            "hosted assignment release unavailable",
        )
        assert release is not None
        with refusal("hosted assignment release unavailable"):
            registration = CodingPrivateV2RegistrationAuthority.model_validate(
                release.registration_authority
            )
            require(
                registration.registration_sha256 == row.registration_sha256,
                "hosted assignment release unavailable",
            )
        agent = await session.get(Agent, row.agent_id)
        require(
            agent is not None
            and agent.sha256 == row.artifact_sha256
            and agent.screened_image_sha256 == row.screened_image_sha256
            and agent.status in SCOREABLE_AGENT_STATUSES
            and agent.screened_image_verified_at is not None
            and agent.screened_image_upload_id is not None
            and agent.screened_image_size_bytes is not None
            and agent.screened_image_id is not None
            and agent.screened_image_ref is not None
            and agent.screening_policy_version is not None
            and agent.screening_policy_version >= SCREENING_POLICY_VERSION,
            "hosted assignment artifact unavailable",
        )
        task = await session.scalar(
            select(CodingHostedPrivateTask).where(
                CodingHostedPrivateTask.evaluation_id == evaluation_id
            )
        )
        require(
            task is not None
            and task.closed_at is None
            and task.frozen_at is None
            and _selection_matches(task, row),
            "hosted assignment task unavailable",
        )
        assert task is not None
        return (
            LaunchableAssignment(
                authority,
                registration,
                release.private_input_authority_sha256,
                task.catalog_index,
                task.max_patch_bytes,
            ),
            now,
        )


def _profile(path: Path, digest: str, maximum: int, stage: str) -> tuple[bytes, dict]:
    with refusal(stage):
        body = read_private(path, maximum)
        value = _decode_json_document(body, maximum_bytes=maximum)
        require(
            sha(body) == digest
            and isinstance(value, dict)
            and canonical(value, maximum) == body
            and isinstance(value.get("image_digest"), str)
            and _IMAGE_DIGEST.fullmatch(value["image_digest"]) is not None,
            stage,
        )
        return body, value


def _stable(path: Path, maximum: int, stage: str, parse: Callable[[Path], Any]):
    """Bracket a path-based loader between identical protected reads."""
    with refusal(stage):
        before = read_private(path, maximum)
        parsed = parse(path)
        require(read_private(path, maximum) == before, stage)
        return before, parsed


@dataclass(frozen=True)
class VerifiedAuthorities:
    files: dict[str, bytes]
    policy: HostedInferencePolicy
    budget: HostedBudgetProfile
    image_digests: set[str]


def verify_authorities(
    layout: HostLayout,
    request: AttemptRequest,
    launchable: LaunchableAssignment,
    now: float,
) -> VerifiedAuthorities:
    authority = launchable.authority
    execution, execution_value = _profile(
        layout.inbox / f"execution-profile-{authority.execution_profile_sha256}.json",
        authority.execution_profile_sha256,
        16384,
        "hosted execution profile refused",
    )
    with refusal("hosted execution profile refused"):
        limits = execution_value["resource_policy"]["candidate_limits"]
        require(
            limits["max_patch_bytes"] == launchable.max_patch_bytes,
            "hosted execution profile refused",
        )
    grading, grading_value = _profile(
        layout.inbox / f"grading-profile-{authority.grading_profile_sha256}.json",
        authority.grading_profile_sha256,
        65536,
        "hosted grading profile refused",
    )
    with refusal("hosted inference policy refused"):
        policy_body = read_private(
            layout.inbox / f"inference-policy-{authority.policy_sha256}.json", 16384
        )
        policy = HostedInferencePolicy.model_validate(
            _decode_json_document(policy_body, maximum_bytes=16384)
        )
        require(
            canonical(policy.model_dump(mode="json", by_alias=True)) == policy_body
            and policy.digest() == sha(policy_body) == authority.policy_sha256
            and policy.runtime_profile_sha256 is not None,
            "hosted inference policy refused",
        )
    with refusal("hosted budget profile refused"):
        budget_body = read_private(
            layout.inbox / f"budget-profile-{policy.runtime_profile_sha256}.json",
            16384,
        )
        budget = parse_coding_inference_json(
            HostedBudgetProfile, budget_body, maximum_bytes=16384
        )
        ProfiledBudgetEstimator(
            profile_bytes=budget_body, policy=policy, now=lambda: now
        )
        require(
            budget.canonical_bytes() == budget_body
            and budget.valid_from_unix <= now < budget.valid_until_unix
            and authority.deadline_unix <= budget.valid_until_unix,
            "hosted budget profile refused",
        )
    probe_body, (probe, _) = _stable(
        layout.inbox / f"probe-receipt-{request.probe_receipt_sha256}.json",
        1 << 20,
        "hosted probe receipt refused",
        load_hippius_probe_receipt,
    )
    require(
        sha(probe_body) == request.probe_receipt_sha256,
        "hosted probe receipt refused",
    )
    with refusal("hosted probe receipt is stale"):
        checked = datetime.fromisoformat(
            probe.checked_at.replace("Z", "+00:00")
        ).timestamp()
        require(
            0 <= now - checked < PROBE_MAX_AGE_SECONDS
            and checked + PROBE_MAX_AGE_SECONDS
            > authority.deadline_unix + FINALIZATION_SECONDS,
            "hosted probe receipt is stale",
        )
    require(
        probe.private_input_authority_sha256 == launchable.reader_authority_sha256,
        "hosted probe receipt authority differs",
    )
    evidence_body, wrapper = _stable(
        layout.evidence_public_key,
        65536,
        "hosted evidence key refused",
        RsaOaepHippiusEvidenceKeyWrapper,
    )
    require(
        wrapper.wrapping_key_sha256 == request.evidence_wrapping_key_sha256,
        "hosted evidence key refused",
    )
    return VerifiedAuthorities(
        files={
            "execution-profile.json": execution,
            "grading-profile.json": grading,
            "inference-policy.json": policy_body,
            "budget-profile.json": budget_body,
            "probe-receipt.json": probe_body,
            "evidence-public.pem": evidence_body,
        },
        policy=policy,
        budget=budget,
        image_digests={execution_value["image_digest"], grading_value["image_digest"]},
    )


def verify_release(layout: HostLayout, launchable: LaunchableAssignment) -> dict:
    root = layout.release(launchable.authority.registration_sha256)
    files = {
        "transport_manifest_file": (root / "transport-manifest.json", 16 << 20),
        "payload_authority_file": (root / "payload-authority.json", 16 << 20),
        "publication_receipt_file": (root / "publication-receipt.json", 16 << 20),
        "curator_public_key_file": (root / "curator-public.pem", 65536),
    }
    with refusal("hosted release authorities refused"):
        for path, maximum in files.values():
            secret_metadata(path, maximum)
        verifier = PrivateV2InputAuthority(
            registration=launchable.registration,
            transport_manifest=files["transport_manifest_file"][0],
            payload_authority=files["payload_authority_file"][0],
            publication_receipt=files["publication_receipt_file"][0],
            trusted_curator_public_key_path=files["curator_public_key_file"][0],
            reader_authority_sha256=launchable.reader_authority_sha256,
            audience="platform-authoring",
        )
        selection = verifier.describe_selection(launchable.catalog_index)
        require(
            selection.registration_sha256 == launchable.authority.registration_sha256,
            "hosted release authorities refused",
        )
    return {name: str(path) for name, (path, _) in files.items()}


def refuse_existing_attempt(layout: HostLayout, attempt_id: UUID) -> Path:
    root = layout.attempts / str(attempt_id)
    if os.path.lexists(root):
        consumed = any(
            os.path.lexists(root / marker)
            for marker in ("runtime/platform-consumed", "runtime/worker/consumed")
        )
        raise HostedAttemptConfigError(
            "hosted attempt already consumed"
            if consumed
            else "hosted attempt already materialized"
        )
    return root


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_attempt(
    layout: HostLayout, root: Path, files: dict[str, bytes], body: bytes
) -> Path:
    """Reserve the attempt exclusively, then publish the config by no-clobber link."""
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        raise HostedAttemptConfigError("hosted attempt already materialized") from None
    _fsync_directory(layout.attempts)
    private_directory(root)
    for name in ("authority", "runtime", "unwrap"):
        (root / name).mkdir(mode=0o700)
    for name, content in files.items():
        write_private(root / "authority" / name, content)
    partial, config = root / "runtime.json.partial", root / "runtime.json"
    write_private(partial, body)
    os.link(partial, config, follow_symlinks=False)
    os.unlink(partial)
    _fsync_directory(root)
    require(read_private(config, 65536) == body, "hosted attempt config write failed")
    return config


async def materialize(
    layout: HostLayout,
    request: AttemptRequest,
    *,
    units: Callable[[], None] = refuse_live_units,
    repository: Callable[[HostLayout, set[str]], str] = docker_repository,
) -> dict[str, Any]:
    units()
    refuse_live_custody(layout)
    host = verify_host(layout)
    secrets = layout.secrets()
    for path, maximum in secrets.values():
        with refusal("hosted secret file metadata refused"):
            secret_metadata(path, maximum)
    postgres_path, postgres_maximum = secrets["postgres_environment_file"]
    with refusal("hosted database environment refused"):
        database, _ = postgres_config(read_json(postgres_path, postgres_maximum))
        database = dataclasses.replace(database, pool_min_size=1, pool_max_size=1)
    engine = create_db_engine(database)
    try:
        sessions = create_session_maker(engine)
        launchable, _ = await read_launchable(
            sessions,
            evaluation_id=request.evaluation_id,
            assignment_sha256=request.assignment_sha256,
        )
        authority = launchable.authority
        root = refuse_existing_attempt(layout, authority.attempt_id)
        now = time.time()
        verified = verify_authorities(layout, request, launchable, now)
        release = verify_release(layout, launchable)
        executor_repository = repository(layout, verified.image_digests)
        worker = uuid4()
        wire = {
            "schema": "dittobench-coding-hosted-platform-runtime-v2",
            "shadow_only": True,
            "weight_eligible": False,
            "worker_id": str(worker),
            "evaluation_id": str(authority.evaluation_id),
            "attempt_id": str(authority.attempt_id),
            "assignment_sha256": authority.digest(),
            "runtime_root": str(root / "runtime"),
            "worker_executable": str(layout.worker_executable),
            "python_executable": str(layout.python_executable),
            "unwrap_executable": str(layout.unwrap_executable),
            "unwrap_work_root": str(root / "unwrap"),
            **{name: str(path) for name, (path, _) in secrets.items()},
            "execution_profile_file": str(root / "authority/execution-profile.json"),
            "grading_profile_file": str(root / "authority/grading-profile.json"),
            "budget_profile_file": str(root / "authority/budget-profile.json"),
            "policy_file": str(root / "authority/inference-policy.json"),
            **release,
            "evidence_public_key_file": str(root / "authority/evidence-public.pem"),
            "evidence_wrapping_key_sha256": request.evidence_wrapping_key_sha256,
            "probe_receipt_file": str(root / "authority/probe-receipt.json"),
            "host": {
                "docker_executable": str(layout.docker_executable),
                "docker_socket": str(layout.docker_socket),
                "executor_repository": executor_repository,
                "seccomp_profile": "",
                "apparmor_profile": "",
                **host,
            },
            "spool_max_bytes": 2 << 30,
            "spool_max_objects": 4096,
        }
        with refusal("hosted attempt config invalid"):
            HostedPlatformRuntimeInput.model_validate(wire)
            body = canonical(wire, 65536)
        # Recheck live state and authority immediately before the exclusive write.
        units()
        refuse_live_custody(layout)
        again, _ = await read_launchable(
            sessions,
            evaluation_id=request.evaluation_id,
            assignment_sha256=request.assignment_sha256,
        )
        require(again == launchable, "hosted assignment changed during materialization")
    finally:
        await engine.dispose()
    config = write_attempt(layout, root, verified.files, body)
    return {
        "schema": RECEIPT_SCHEMA,
        "evaluation_id": str(authority.evaluation_id),
        "attempt_id": str(authority.attempt_id),
        "worker_id": str(worker),
        "assignment_sha256": authority.digest(),
        "deadline_unix": authority.deadline_unix,
        "registration_sha256": authority.registration_sha256,
        "execution_profile_sha256": authority.execution_profile_sha256,
        "grading_profile_sha256": authority.grading_profile_sha256,
        "policy_sha256": authority.policy_sha256,
        "budget_profile_sha256": verified.budget.digest(),
        "probe_receipt_sha256": request.probe_receipt_sha256,
        "evidence_wrapping_key_sha256": request.evidence_wrapping_key_sha256,
        "config_file": str(config),
        "config_sha256": sha(body),
        "runtime_root": str(root / "runtime"),
        "admitted": True,
        "services_started": False,
        "shadow_only": True,
        "weight_eligible": False,
    }


async def materialize_on_host(request: AttemptRequest) -> dict[str, Any]:
    return await materialize(production_layout(), request)
