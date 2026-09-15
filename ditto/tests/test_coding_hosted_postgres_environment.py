"""Native PostgreSQL environment materialization is default-off, unforgeable, silent."""

import copy
import grp
import hashlib
import json
import os
import pwd
import re
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment"
MAIN = (ROLE / "tasks/main.yml").read_text()
MATERIALIZE_TEXT = (ROLE / "tasks/materialize.yml").read_text()
# Parsed materialize tasks without YAML comments, for token scans.
PARSED = yaml.safe_dump(yaml.safe_load(MATERIALIZE_TEXT), width=10_000)
PLAYBOOK = ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment.yml"
FIXTURE = ROOT / "infra/ansible/tests/coding-hosted-postgres-environment.yml"
# The stacked removal role (#1897). Its guards are duplicated, not shared; the
# two roles have deliberately diverged (this one is hardened further), so only
# the parts that must stay identical are compared, when both are on the tree.
CLEANUP_ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment_cleanup"

PASSWORD_LOOKUP = "lookup('env', 'DITTO_CODING_PG_PASSWORD')"
PFX = "coding_hosted_postgres_environment_"
INPUTS = {
    f"{PFX}enabled": False,
    f"{PFX}confirmation": "",
    f"{PFX}source_revision": "",
    f"{PFX}host": "",
}
CONFIRMATION = "MATERIALIZE NATIVE CODING POSTGRES ENVIRONMENT"
REVISION = "0123456789abcdef0123456789abcdef01234567"
DATABASE_HOST = "10.30.0.5"
CAPTURED_GATE = f"{PFX}captured_enabled"
GATE = f"{CAPTURED_GATE} is sameas true"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"

CUSTODY = "/var/lib/ditto-coding-custody/private/postgres-environment.json"
HOSTED = "/var/lib/ditto-coding-hosted/private/postgres-environment.json"
COPIES = [
    {"path": CUSTODY, "owner": "ditto-coding-custody"},
    {"path": HOSTED, "owner": "ditto-coding-hosted"},
]
OWNERS = ("ditto-coding-custody", "ditto-coding-hosted")
ENTRIES = [
    "POSTGRES_HOST={{ coding_hosted_postgres_environment_captured_host }}",
    "POSTGRES_PORT=5432",
    "POSTGRES_USER=ditto",
    "POSTGRES_DB=ditto_platform_prod",
    "POSTGRES_COMMAND_TIMEOUT=30",
    "POSTGRES_POOL_MIN_SIZE=1",
    "POSTGRES_POOL_MAX_SIZE=4",
]

CAPTURE_GATE = "Capture the materialization gate once, neutralising templates and loops"
INCLUDE = "Materialize the native PostgreSQL environment only when explicitly enabled"
EXPLAIN = "Explain dormant native PostgreSQL environment materialization"
PASSWORD_VARIABLE = "Refuse a password supplied as an Ansible variable"
PRESET = "Refuse preset registered results and undocumented role inputs"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
ACCOUNTS = "Inspect host accounts once"
CAPTURE = "Capture the operator inputs once, neutralising templates and loops"
HOST = "Require the exact host, source, database address and confirmation"
PASSWORD = "Require one bounded single-line password from the controller environment"
ACCOUNT_CHECK = "Require the distinct worker and custodian identities"
LISTING = "List live worker and custody units"
LIVE = "Refuse to replace credentials unless every listed unit is inactive or failed"
ASSEMBLE = "Assemble the fixed environment entries from the captured host"
RENDER = "Render the environment document once with the controller-only password"
DIRECTORIES = "Create owner-only credential directories for each reader"
WRITE = "Write each reader's own PostgreSQL environment copy without printing it"
RELIST_WRITE = "Re-list live worker and custody units after writing"
LIVE_WRITE = "Refuse if any unit became active during the write"
REINSPECT = "Reinspect both copies as ownership, mode and digest metadata only"
SEALED = "Require owner-only regular single-link copies"
DIGEST = "Require both copies to hold exactly the rendered document"
RELIST_VERIFY = "Re-list live worker and custody units after verifying"
LIVE_VERIFY = "Refuse if any unit became active during verification"
REPORT = "Report only that the copies exist"

# The one allow-list regex, shared with the initial, post-write and post-verify
# live-unit checks and with the cleanup role.
UNIT_PATTERN = (
    "^(ditto-coding-hosted-worker[.]service|ditto-coding-custody@\\\\S+[.]service)"
    "\\\\s+\\\\S+\\\\s+(inactive|failed)(\\\\s|$)"
)
LISTING_ARGV = [
    "/usr/bin/systemctl",
    "list-units",
    "--all",
    "--plain",
    "--no-legend",
    "--full",
    "ditto-coding-hosted-worker.service",
    "ditto-coding-custody@*.service",
]


def _materialize() -> list[dict]:
    return yaml.safe_load(MATERIALIZE_TEXT)


def _main() -> list[dict]:
    return yaml.safe_load(MAIN)


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    (task,) = [t for t in _walk(tasks or _materialize()) if t.get("name") == name]
    return task


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def _live_check(task: dict) -> str:
    (that,) = task["ansible.builtin.assert"]["that"]
    return _flat(that)


def _expected_live_check(register: str) -> str:
    return _flat(
        f"{register}.stdout_lines | reject('match', '{UNIT_PATTERN}') "
        "| list | length == 0"
    )


# --------------------------------------------------------------------------- #
# Structural tests                                                            #
# --------------------------------------------------------------------------- #


def test_default_off_gate_is_decided_once_behind_a_dynamic_include() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == INPUTS
    main = _main()
    # A no_log capture of the flag, a dynamic include gated on the captured fact,
    # and the dormant explanation. No block, no per-task gate to re-evaluate.
    assert [t["name"] for t in main] == [CAPTURE_GATE, INCLUDE, EXPLAIN]
    capture, include, explain = main
    assert capture["no_log"] is True
    assert capture["ansible.builtin.set_fact"] == {
        CAPTURED_GATE: f"{{{{ ({PFX}enabled | default(false, true)) is sameas true }}}}"
    }
    assert include["ansible.builtin.include_tasks"] == "materialize.yml"
    assert _flat(include["when"]) == GATE
    assert _flat(explain["when"]) == f"not ({GATE})"
    parsed_main = yaml.safe_dump(main)
    assert "import_tasks" not in parsed_main  # static would defeat --start-at-task
    assert "block" not in parsed_main
    # The raw flag is rendered only by the capture.
    assert [t["name"] for t in main if f"{PFX}enabled" in json.dumps(t)] == [
        CAPTURE_GATE
    ]
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["gather_facts"] is False


def test_no_bool_filter_can_print_a_coerced_value() -> None:
    # ansible-core 2.21 prints any non-boolean string the bool filter coerces in
    # a deprecation warning, even under no_log, so neither task file uses it.
    for text in (yaml.safe_dump(_main()), PARSED):
        assert not re.search(r"\|\s*bool\b", text)


def test_preset_and_password_guards_run_first_as_the_single_source_of_truth() -> None:
    tasks = _materialize()
    assert [t["name"] for t in tasks[:2]] == [PASSWORD_VARIABLE, PRESET]
    # Nothing is registered or set before the guards, so at guard time only the
    # documented inputs carry the prefix.
    before = tasks[: tasks.index(_task(PRESET))]
    assert not any("register" in t or "ansible.builtin.set_fact" in t for t in before)

    assert _task(PASSWORD_VARIABLE)["ansible.builtin.assert"]["that"] == [
        f"{PFX}password is not defined"
    ]
    that = _task(PRESET)["ansible.builtin.assert"]["that"]
    # A single condition: the varnames equality is the one source of truth, with
    # no duplicate per-name "is not defined" lines.
    assert len(that) == 1
    allowed = sorted([*INPUTS, CAPTURED_GATE])
    assert _flat(that[0]) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PFX}', wantlist=True) "
        f"| reject('match', '^{PFX}cleanup_') "
        "| sort == [" + ", ".join(f"'{name}'" for name in allowed) + "]"
    )
    # main.yml captures only the gate before this guard.
    main_facts = [
        name for task in _main() for name in task.get("ansible.builtin.set_fact", {})
    ]
    assert main_facts == [CAPTURED_GATE]
    # The _cleanup_ exclusion keeps the removal role's variables from producing a
    # misleading refusal here.
    assert f"reject('match', '^{PFX}cleanup_')" in _flat(that[0])
    assert "Nothing was written" in _task(PRESET)["ansible.builtin.assert"]["fail_msg"]


def test_identity_and_accounts_come_from_registered_probes_no_facts_gathered() -> None:
    identity = _task(IDENTITY)
    assert identity["ansible.builtin.setup"] == {
        "gather_subset": ["!all", "!min", "platform", "distribution"]
    }
    assert identity["register"] == f"{PFX}identity"
    accounts = _task(ACCOUNTS)
    assert accounts["ansible.builtin.getent"] == {"database": "passwd"}
    assert accounts["register"] == f"{PFX}accounts"

    that = _task(HOST)["ansible.builtin.assert"]["that"]
    assert that[:5] == [
        "inventory_hostname in groups.get('role_coding_hosted', [])",
        *(
            f"{PFX}identity.ansible_facts.ansible_{key} == '{value}'"
            for key, value in PROBED_IDENTITY.items()
        ),
    ]
    account_lines = _task(ACCOUNT_CHECK)["ansible.builtin.assert"]["that"]
    assert all(
        line.startswith(f"{PFX}accounts.ansible_facts.getent_passwd")
        for line in account_lines
    )
    # No guard reads gathered facts: every ansible_facts reference is via a
    # registered probe.
    assert PARSED.count("ansible_facts") == (
        PARSED.count(f"{PFX}identity.ansible_facts")
        + PARSED.count(f"{PFX}accounts.ansible_facts")
    )


def test_inputs_are_captured_once_with_a_template_error_guard() -> None:
    capture = _task(CAPTURE)
    assert capture["no_log"] is True
    assert capture["ansible.builtin.set_fact"] == {
        f"{PFX}captured_host": f"{{{{ {PFX}host | default('', true) }}}}",
        f"{PFX}captured_confirmation": (
            f"{{{{ {PFX}confirmation | default('', true) }}}}"
        ),
        f"{PFX}captured_revision": (
            f"{{{{ {PFX}source_revision | default('', true) }}}}"
        ),
    }
    # After the capture, no task references a raw operator input: every later
    # task reads the captured, validated value, so a lazily templated value
    # cannot render differently inside a loop and no later task can render an
    # operator template into an error message. (The two guards above name the
    # inputs only as string literals in the varnames allow-list.)
    tasks = _materialize()
    after_capture = tasks[tasks.index(_task(CAPTURE)) + 1 :]
    for raw in (f"{PFX}host", f"{PFX}confirmation", f"{PFX}source_revision"):
        offenders = [
            t["name"]
            for t in _walk(after_capture)
            if re.search(rf"\b{raw}\b", json.dumps(t))
        ]
        assert offenders == [], (raw, offenders)
    # The document is built from the captured host, not the raw input.
    assert _set_fact(ASSEMBLE)[f"{PFX}entries"] == ENTRIES
    assert _flat(_set_fact(RENDER)[f"{PFX}document"]) == _flat(
        f"{{{{ ({PFX}entries + ['POSTGRES_PASSWORD=' ~ {PASSWORD_LOOKUP}]) "
        "| to_json }}"
    )


PROBED_IDENTITY = {
    "hostname": "ditto-coding-hosted-v2",
    "architecture": "x86_64",
    "distribution": "Debian",
    "distribution_major_version": "13",
}


def _set_fact(name: str) -> dict:
    return _task(name)["ansible.builtin.set_fact"]


def test_revision_and_database_host_refuse_a_trailing_newline() -> None:
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    host_pattern = "^10[.]30[.]0[.]([2-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-3])$"
    assert that[5:] == [
        f"{PFX}captured_revision is string",
        f"{PFX}captured_revision is match('^[0-9a-f]{{40}}$')",
        f"{PFX}captured_revision | length == 40",
        f"{PFX}captured_confirmation == '{CONFIRMATION}'",
        f"{PFX}captured_host is string",
        f"{PFX}captured_host == {PFX}captured_host | trim",
        f"{PFX}captured_host is match('{host_pattern}')",
    ]
    # A '$' anchor alone accepts one trailing newline; the exact length and the
    # trim comparison are what refuse it.
    assert re.match("^[0-9a-f]{40}$", REVISION + "\n")
    assert len(REVISION + "\n") != 40
    assert re.match(host_pattern, DATABASE_HOST + "\n")
    assert (DATABASE_HOST + "\n").strip() != DATABASE_HOST + "\n"


ALLOWED_UNIT_LINES = [
    "ditto-coding-hosted-worker.service loaded inactive dead Worker",
    "ditto-coding-hosted-worker.service   loaded    failed   failed   Worker",
    "ditto-coding-custody@0.service not-found inactive dead ditto-coding-custody@0",
    "ditto-coding-custody@abc-1.service loaded inactive dead",
]
REFUSED_UNIT_LINES = [
    "ditto-coding-hosted-worker.service loaded active running Worker",
    "ditto-coding-hosted-worker.service loaded activating start Worker",
    "ditto-coding-custody@0.service loaded deactivating stop-sigterm Custody",
    "ditto-coding-custody@0.service loaded reloading reload Custody",
    "ditto-coding-custody@0.service loaded refreshing refresh-extensions Custody",
    "ditto-coding-custody@0.service loaded maintenance cleaning Custody",
    "ditto-coding-custody@0.service loaded quiescent future Custody",
    "ditto-coding-custody@0.service loaded inactivefuture dead Custody",
    "● ditto-coding-custody@0.service loaded inactive dead Custody",
    "other.service loaded inactive dead Other",
    "ditto-coding-custody@.service loaded inactive dead Custody",
    "ditto-coding-custody@0.service inactive",
    " ",
]


def test_live_unit_refusal_is_an_allow_list() -> None:
    listing = _task(LISTING)
    assert listing["ansible.builtin.command"]["argv"] == LISTING_ARGV
    assert listing["register"] == f"{PFX}units"
    assert listing["check_mode"] is False and listing["changed_when"] is False
    assert _live_check(_task(LIVE)) == _expected_live_check(f"{PFX}units")
    regex = re.compile(UNIT_PATTERN.replace("\\\\", "\\"))
    for line in ALLOWED_UNIT_LINES:
        assert regex.match(line), line
    for line in REFUSED_UNIT_LINES:
        assert not regex.match(line), line
    # The listing is the only systemctl use; nothing is stopped or started.
    assert PARSED.count("systemctl") == 3  # initial + after write + after verify
    for forbidden in ("systemd:", "service:", "state: stopped", "state: started"):
        assert forbidden not in PARSED, forbidden


def test_unit_state_is_rechecked_after_write_and_after_verify() -> None:
    names = [t["name"] for t in _materialize()]
    # Three listings, each with the same argv, and each write-window listing is
    # immediately followed by its allow-list refusal.
    for listing, register, check in (
        (LISTING, f"{PFX}units", LIVE),
        (RELIST_WRITE, f"{PFX}units_after_write", LIVE_WRITE),
        (RELIST_VERIFY, f"{PFX}units_after_verify", LIVE_VERIFY),
    ):
        assert _task(listing)["ansible.builtin.command"]["argv"] == LISTING_ARGV
        assert _task(listing)["register"] == register
        assert names.index(check) == names.index(listing) + 1
        assert _live_check(_task(check)) == _expected_live_check(register)
    # The re-checks straddle the write and the verify, and their refusals warn
    # that a copy may have been read mid-rotation.
    assert names.index(RELIST_WRITE) == names.index(WRITE) + 1
    assert names.index(RELIST_VERIFY) == names.index(DIGEST) + 1
    for check in (LIVE_WRITE, LIVE_VERIFY):
        assert "mid-rotation" in _task(check)["ansible.builtin.assert"]["fail_msg"]


def test_credentials_are_never_logged_read_back_or_diffed() -> None:
    write = _task(WRITE)
    assert write["no_log"] is True and write["diff"] is False
    for forbidden in ("slurp", "fetch", "set -x", "gcloud secrets", "extra_vars"):
        assert forbidden not in MATERIALIZE_TEXT
    report = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert "password" not in report.lower()
    assert "{{" not in report
    # Every task that touches the password, the document, the captured inputs or
    # a stat result (which carries the checksum) is no_log.
    for name in (CAPTURE, PASSWORD, ASSEMBLE, RENDER, WRITE, REINSPECT, SEALED, DIGEST):
        assert _task(name).get("no_log") is True, name
    # The sealed assert loops over whole stat results, so no_log is what keeps
    # item.stat.checksum out of the -v output and failure lines.
    assert _task(SEALED)["loop"] == f"{{{{ {PFX}files.results }}}}"


def test_copies_are_verified_by_owner_mode_and_digest_only() -> None:
    stat = _task(REINSPECT)
    assert stat["ansible.builtin.stat"] == {
        "path": "{{ item.path }}",
        "follow": False,
        "get_checksum": True,
        "checksum_algorithm": "sha256",
        "get_mime": False,
    }
    assert stat["loop"] == _task(WRITE)["loop"] == COPIES
    assert stat["register"] == f"{PFX}files"
    that = _task(SEALED)["ansible.builtin.assert"]["that"]
    assert "item.stat.pw_name == item.item.owner" in that
    assert "item.stat.mode == '0600'" in that
    assert "item.stat.nlink == 1" in that
    assert "not item.stat.islnk" in that
    digest = _task(DIGEST)
    assert digest["ansible.builtin.assert"]["that"] == [
        f"item.stat.checksum == {PFX}document | hash('sha256')"
    ]
    assert digest["loop"] == f"{{{{ {PFX}files.results }}}}"


def test_playbook_group_connection_and_ci_registration() -> None:
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["become"] is True and play["gather_facts"] is False
    assert play["roles"] == ["coding_hosted_postgres_environment"]
    (fixture,) = yaml.safe_load(FIXTURE.read_text())
    assert fixture["hosts"] == "localhost" and fixture["connection"] == "local"
    assert fixture["become"] is False and fixture["gather_facts"] is False
    assert fixture["roles"] == ["coding_hosted_postgres_environment"]
    group = yaml.safe_load(
        (ROOT / "infra/ansible/group_vars/role_coding_hosted.yml").read_text()
    )
    assert set(group) == {
        "gcp_project",
        "gcp_region",
        "gcp_zone",
        "ansible_user",
        "ansible_ssh_common_args",
    }
    assert "gcloud compute start-iap-tunnel" in group["ansible_ssh_common_args"]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-postgres-environment.yml" in workflow
    assert "tests/coding-hosted-postgres-environment.yml" in workflow


def test_rehearsal_runs_only_in_the_infra_ansible_job() -> None:
    workflows = ROOT / ".github/workflows"
    infra = yaml.safe_load((workflows / "infra-ci.yml").read_text())
    this_file = str(Path(__file__).relative_to(ROOT))
    for trigger in ("pull_request", "push"):
        paths = infra[True][trigger]["paths"]
        assert this_file in paths
        # A lockfile or dependency-group change alters what the gate-free run
        # resolves, so both trigger the job.
        assert "pyproject.toml" in paths and "uv.lock" in paths
    (step,) = [
        step
        for job in infra["jobs"].values()
        for step in job["steps"]
        if REHEARSAL_GATE in step.get("env", {}) and this_file in step["run"]
    ]
    assert step in infra["jobs"]["ansible"]["steps"]
    assert step["env"] == {REHEARSAL_GATE: "1"}
    assert step["working-directory"] == "${{ github.workspace }}"
    # The locked dev group already brings PyYAML 6.0.3 (via pre-commit), so no
    # --with is needed.
    assert step["run"].split() == [
        "uv",
        "run",
        "--locked",
        "--only-group",
        "dev",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-rs",
        this_file,
    ]
    uses = [s.get("uses", "") for s in infra["jobs"]["ansible"]["steps"]]
    assert any(action.startswith("astral-sh/setup-uv@") for action in uses)
    for other in workflows.glob("*.yml"):
        if other.name != "infra-ci.yml":
            assert REHEARSAL_GATE not in other.read_text(), other.name


def test_docs_describe_every_forgery_guard() -> None:
    docs = (ROOT / "infra/docs/coding-hosted-postgres-v2.md").read_text()
    section = _flat(
        docs.split("## Native environment files", 1)[1].split("\n## ", 1)[0]
    )
    for phrase in (
        "dynamic",
        "start-at-task",
        "captured once",
        "template",
        "gathers no facts",
        "`ansible_facts`",
        "exactly 40 lowercase hex characters",
        "trailing newline",
        "`inactive` or `failed`",
        "`refreshing`",
        "`maintenance`",
        "re-checked",
        "mid-rotation",
        f"`{REHEARSAL_GATE}=1`",
    ):
        assert phrase in section, phrase


# --------------------------------------------------------------------------- #
# Guard parity with the stacked removal role                                  #
# --------------------------------------------------------------------------- #


def test_guard_logic_follows_the_role_template() -> None:
    # The probe, the source-revision shape and the live-unit allow-list are the
    # unforgeable core; pin them so drift is caught.
    assert _task(IDENTITY)["ansible.builtin.setup"]["gather_subset"] == [
        "!all",
        "!min",
        "platform",
        "distribution",
    ]
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    assert f"{PFX}captured_revision is match('^[0-9a-f]{{40}}$')" in that
    assert f"{PFX}captured_revision | length == 40" in that
    assert _live_check(_task(LIVE)) == _expected_live_check(f"{PFX}units")


@pytest.mark.skipif(
    not CLEANUP_ROLE.exists(),
    reason="the coding_hosted_postgres_environment_cleanup role is not on this tree",
)
def test_unit_listing_and_allow_list_match_the_cleanup_role() -> None:
    # The two roles have diverged (this one adds a dynamic-include gate, capture
    # once and template-error guards the cleanup branch still lacks), so only the
    # parts that must stay byte-identical are compared: the unit listing and the
    # allow-list regex both roles use to decide a copy is safe to touch.
    cleanup = yaml.safe_load((CLEANUP_ROLE / "tasks/main.yml").read_text())
    cleanup_prefix = "coding_hosted_postgres_environment_cleanup_"
    (cleanup_listing,) = [
        t
        for t in _walk(cleanup)
        if t.get("ansible.builtin.command", {}).get("argv", [""])[0]
        == "/usr/bin/systemctl"
    ]
    assert cleanup_listing["ansible.builtin.command"]["argv"] == LISTING_ARGV
    (cleanup_live,) = [
        t
        for t in _walk(cleanup)
        if "ansible.builtin.assert" in t
        and f"{cleanup_prefix}units.stdout_lines"
        in json.dumps(t["ansible.builtin.assert"]["that"])
    ]
    (cleanup_that,) = cleanup_live["ansible.builtin.assert"]["that"]
    # Normalise the prefix and compare the allow-list logic verbatim.
    assert _flat(cleanup_that).replace(cleanup_prefix, PFX) == _expected_live_check(
        f"{PFX}units"
    )


# --------------------------------------------------------------------------- #
# Local rehearsal (gated): run the real, restructured role against a temporary #
# tree through ansible-core 2.21.2, under the repo's yaml callback and -v      #
# --diff, and prove no forged input writes a copy or leaks the password.       #
# --------------------------------------------------------------------------- #

# A stand-in, never a real credential, chosen to contain the characters JSON and
# YAML escape so the leak search must look for the escaped forms too.
REHEARSAL_PASSWORD = 'rehearsal-only "stand-in"=pass\\word/42'
STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
    "ditto-coding-custody@1.service not-found inactive dead ditto-coding-custody@1\n"
)
REHEARSAL_ACCOUNTS = {
    "ditto-coding-hosted": [
        "x",
        "2001",
        "2001",
        "",
        "/var/lib/ditto-coding-hosted",
        "/usr/sbin/nologin",
    ],
    "ditto-coding-custody": [
        "x",
        "2002",
        "2002",
        "",
        "/var/lib/ditto-coding-custody",
        "/usr/sbin/nologin",
    ],
}
LIVE_UNITS = {
    "active": "ditto-coding-custody@0.service loaded active running Custody",
    "activating": "ditto-coding-hosted-worker.service loaded activating start Worker",
    "deactivating": (
        "ditto-coding-hosted-worker.service loaded deactivating stop-sigterm Worker"
    ),
    "reloading": "ditto-coding-custody@0.service loaded reloading reload Custody",
    "refreshing": (
        "ditto-coding-custody@2.service loaded refreshing refresh-extensions Custody"
    ),
    "maintenance": "ditto-coding-custody@3.service loaded maintenance cleaning Custody",
    "unknown_state": "ditto-coding-hosted-worker.service loaded quiescent idle Worker",
    "unparseable": "● ditto-coding-custody@0.service loaded inactive dead Custody",
}

rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)


def _rewrite(node: Any, *, local_identity: bool, mock_accounts: bool) -> Any:
    """Point the role at a temporary tree: rewrite paths, owners, the unit
    listing and (optionally) the identity comparison and the account probe."""
    if isinstance(node, dict):
        out = {
            key: (
                value
                if key == "ansible.builtin.assert"
                else _rewrite(
                    value, local_identity=local_identity, mock_accounts=mock_accounts
                )
            )
            for key, value in node.items()
        }
        command = out.get("ansible.builtin.command")
        if command and command.get("argv", [None])[0] == "/usr/bin/systemctl":
            out["ansible.builtin.command"] = {
                "argv": ["/usr/bin/printf", "%s", "{{ rehearsal_units }}"]
            }
        if mock_accounts and "ansible.builtin.getent" in out:
            del out["ansible.builtin.getent"]
            out["ansible.builtin.set_fact"] = {
                "getent_passwd": "{{ rehearsal_accounts }}"
            }
        if "ansible.builtin.assert" in out:
            that = out["ansible.builtin.assert"]["that"]
            new = []
            for line in that:
                match = re.fullmatch(
                    rf"{PFX}identity\.ansible_facts\.(ansible_\w+) == '[^']+'", line
                )
                if match and local_identity:
                    new.append(
                        f"{PFX}identity.ansible_facts.{match[1]} == "
                        f"rehearsal_probe.ansible_facts.{match[1]}"
                    )
                else:
                    new.append(line)
            out["ansible.builtin.assert"] = dict(
                out["ansible.builtin.assert"], that=new
            )
        return out
    if isinstance(node, list):
        return [
            _rewrite(x, local_identity=local_identity, mock_accounts=mock_accounts)
            for x in node
        ]
    if isinstance(node, str):
        if node in OWNERS:
            return "{{ rehearsal_owner }}"
        return node.replace("/var/lib/", "{{ rehearsal_root }}/var/lib/")
    return node


def _build_role(
    dst: Path, *, local_identity: bool, mock_accounts: bool, wrong_mode: bool = False
) -> None:
    tasks = dst / "roles/coding_hosted_postgres_environment/tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(MAIN)
    materialize = _rewrite(
        copy.deepcopy(_materialize()),
        local_identity=local_identity,
        mock_accounts=mock_accounts,
    )
    if wrong_mode:
        # Force the sealed check to fail after a successful write so the test can
        # prove the checksum is not printed when the owner/mode assert fails.
        _task(WRITE, materialize)["ansible.builtin.copy"]["mode"] = "0640"
    rendered = json.dumps(materialize)
    assert "systemctl" not in rendered
    assert not any(f'"{owner}"' in rendered for owner in OWNERS)
    (tasks / "materialize.yml").write_text(yaml.safe_dump(materialize, sort_keys=False))


def _play() -> dict:
    return {
        "name": "Rehearse materialization",
        "hosts": "all",
        "strategy": "free",
        "gather_facts": False,
        "become": False,
        "vars": {"ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "tasks": [
            {
                "name": "Probe this machine for the local-identity comparison",
                "ansible.builtin.setup": {
                    "gather_subset": ["!all", "!min", "platform", "distribution"]
                },
                "register": "rehearsal_probe",
            },
            {
                "name": "Rehearse the role",
                "block": [
                    {
                        "name": "Include the role",
                        "ansible.builtin.include_role": {
                            "name": "coding_hosted_postgres_environment"
                        },
                    },
                    {
                        "name": "Record completion",
                        "ansible.builtin.copy": {
                            "dest": "{{ rehearsal_root }}/outcome.json",
                            "content": "{{ {'ok': true} | to_json }}",
                        },
                    },
                ],
                "rescue": [
                    {
                        "name": "Record refusal",
                        "ansible.builtin.copy": {
                            "dest": "{{ rehearsal_root }}/outcome.json",
                            "content": (
                                "{{ {'task': ansible_failed_task.name, 'msg': "
                                "ansible_failed_result.msg | default('')} | to_json }}"
                            ),
                        },
                    }
                ],
            },
        ],
    }


def _hostvars(root: Path, **overrides: object) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    base = {
        "ansible_connection": "local",
        f"{PFX}enabled": True,
        f"{PFX}confirmation": CONFIRMATION,
        f"{PFX}source_revision": REVISION,
        f"{PFX}host": DATABASE_HOST,
        "rehearsal_root": str(root),
        "rehearsal_units": STOPPED_UNITS,
        "rehearsal_owner": pwd.getpwuid(os.getuid()).pw_name,
        "rehearsal_group": grp.getgrgid(os.getgid()).gr_name,
        "rehearsal_accounts": REHEARSAL_ACCOUNTS,
    }
    base.update(overrides)
    return base


def _run(
    tmp_path: Path,
    name: str,
    hosts: dict[str, dict],
    *flags: str,
    local_identity: bool = True,
    mock_accounts: bool = True,
    wrong_mode: bool = False,
) -> str:
    work = tmp_path / name
    work.mkdir()
    _build_role(
        work,
        local_identity=local_identity,
        mock_accounts=mock_accounts,
        wrong_mode=wrong_mode,
    )
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": hosts}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "play.yml").write_text(yaml.safe_dump([_play()], sort_keys=False))
    (work / "ansible.cfg").write_text(
        "[defaults]\nstdout_callback = default\ncallback_result_format = yaml\n"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANSIBLE_") and key != "DITTO_CODING_PG_PASSWORD"
    }
    environment |= {
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_HOME": str(work / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(work / "ansible-tmp"),
        "ANSIBLE_ROLES_PATH": str(work / "roles"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "DITTO_CODING_PG_PASSWORD": REHEARSAL_PASSWORD,
    }
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-f",
            "20",
            "-v",
            "--diff",
            "-i",
            "inventory.yml",
            *flags,
            "play.yml",
        ],
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output[-6000:]
    # Multi-form leak search: the raw stand-in, its JSON- and YAML-escaped forms,
    # and the SHA-256 of the rendered document (which would enable offline
    # guessing) must never reach the wire, even under -v --diff.
    for form in _leak_forms():
        assert form not in output, (name, form[:24])
    return output


def _document(host: str = DATABASE_HOST, password: str = REHEARSAL_PASSWORD) -> str:
    # to_json is json.dumps with default separators and ASCII escapes, matching
    # what the role renders and writes.
    values = [
        f"POSTGRES_HOST={host}",
        "POSTGRES_PORT=5432",
        "POSTGRES_USER=ditto",
        "POSTGRES_DB=ditto_platform_prod",
        "POSTGRES_COMMAND_TIMEOUT=30",
        "POSTGRES_POOL_MIN_SIZE=1",
        "POSTGRES_POOL_MAX_SIZE=4",
        f"POSTGRES_PASSWORD={password}",
    ]
    return json.dumps(values)


def _leak_forms() -> set[str]:
    document = _document()
    digest = hashlib.sha256(document.encode()).hexdigest()
    return {
        REHEARSAL_PASSWORD,
        json.dumps(REHEARSAL_PASSWORD)[1:-1],
        yaml.safe_dump(REHEARSAL_PASSWORD).strip(),
        digest,
    }


def _outcome(root: Path) -> dict | None:
    path = root / "outcome.json"
    return json.loads(path.read_text()) if path.exists() else None


def _written(root: Path, path: str) -> list | None:
    copy_path = root / path.lstrip("/")
    return json.loads(copy_path.read_text()) if copy_path.exists() else None


def _refused_at(root: Path, task: str) -> None:
    outcome = _outcome(root)
    assert outcome is not None and outcome.get("task") == task, (root.name, outcome)
    assert _written(root, CUSTODY) is None and _written(root, HOSTED) is None
    assert not (root / "var").exists(), root.name


def _materialized(root: Path, host: str = DATABASE_HOST) -> None:
    expected = json.loads(_document(host=host))
    for item in COPIES:
        assert _written(root, item["path"]) == expected, root.name
        copy_path = root / item["path"].lstrip("/")
        assert copy_path.stat().st_mode & 0o777 == 0o600
        assert copy_path.stat().st_nlink == 1
        assert copy_path.parent.stat().st_mode & 0o777 == 0o700


@rehearsal
def test_rehearsal_materializes_and_refuses_every_forged_inventory_input(
    tmp_path,
) -> None:
    inventory_cases = [
        "materialized",
        "no_units",
        *LIVE_UNITS,
        "lazy_host",
        "lazy_enabled",
        "leak_enabled",
        "enabled_password",
        "enabled_string_true",
        "leak_host_error",
        "leak_host_value",
        "revision_newline",
        "host_newline",
        "preset_result",
        "undocumented_input",
        "password_variable",
    ]
    roots = {name: tmp_path / "hosts" / name for name in inventory_cases}
    hosts = {name: _hostvars(roots[name]) for name in inventory_cases}
    hosts["no_units"]["rehearsal_units"] = ""
    for name, line in LIVE_UNITS.items():
        hosts[name]["rehearsal_units"] = STOPPED_UNITS + line + "\n"
    # A lazily templated host renders 203.0.113.9 only inside a loop; captured
    # once with no item it must resolve to the safe address and write that.
    hosts["lazy_host"][f"{PFX}host"] = (
        '{{ "203.0.113.9" if item is defined else "10.30.0.5" }}'
    )
    # A lazily templated gate is false with no loop item and must not write.
    hosts["lazy_enabled"][f"{PFX}enabled"] = "{{ item is defined }}"
    # A flag that renders to the password: the bool filter would print it in a
    # deprecation warning; sameas refuses it silently. Only a boolean true opens.
    hosts["enabled_password"][f"{PFX}enabled"] = (
        '{{ lookup("env", "DITTO_CODING_PG_PASSWORD") }}'
    )
    hosts["enabled_string_true"][f"{PFX}enabled"] = "true"
    # A gate whose template errors while reading the password must resolve to
    # false (default guard) and write nothing, without leaking the value.
    hosts["leak_enabled"][f"{PFX}enabled"] = (
        '{{ {}[lookup("env", "DITTO_CODING_PG_PASSWORD")] }}'
    )
    # A template that errors while reading the password, and one that resolves to
    # the password: both must be caught at capture and fail validation, silently.
    hosts["leak_host_error"][f"{PFX}host"] = (
        '{{ {}[lookup("env", "DITTO_CODING_PG_PASSWORD")] }}'
    )
    hosts["leak_host_value"][f"{PFX}host"] = (
        '{{ lookup("env", "DITTO_CODING_PG_PASSWORD") }}'
    )
    hosts["revision_newline"][f"{PFX}source_revision"] = REVISION + "\n"
    hosts["host_newline"][f"{PFX}host"] = DATABASE_HOST + "\n"
    # Inventory values are refused exactly like extra vars.
    hosts["preset_result"][f"{PFX}units"] = {"stdout": "", "stdout_lines": []}
    hosts["undocumented_input"][f"{PFX}user"] = "postgres"
    hosts["password_variable"][f"{PFX}password"] = "rehearsal-variable"

    _run(tmp_path, "inventory", hosts)

    _materialized(roots["materialized"])
    _materialized(roots["no_units"])
    _materialized(roots["lazy_host"])  # wrote the safe captured address, not 203.
    # A lazy gate and a gate whose template errors both wrote nothing.
    for name in (
        "lazy_enabled",
        "leak_enabled",
        "enabled_password",
        "enabled_string_true",
    ):
        assert _written(roots[name], CUSTODY) is None
        assert not (roots[name] / "var").exists()

    refusals = {
        **dict.fromkeys(LIVE_UNITS, LIVE),
        "leak_host_error": HOST,
        "leak_host_value": HOST,
        "revision_newline": HOST,
        "host_newline": HOST,
        "preset_result": PRESET,
        "undocumented_input": PRESET,
        "password_variable": PASSWORD_VARIABLE,
    }
    for name, task in refusals.items():
        _refused_at(roots[name], task)


@rehearsal
def test_rehearsal_extra_vars_cannot_forge_the_gate_facts_or_accounts(
    tmp_path,
) -> None:
    # Forged gathered facts describing the dedicated host and valid accounts must
    # not satisfy the checks, which read only registered probes.
    assert socket.gethostname().split(".")[0] != PROBED_IDENTITY["hostname"]
    forged_facts = {
        **{f"ansible_{key}": value for key, value in PROBED_IDENTITY.items()},
        "getent_passwd": REHEARSAL_ACCOUNTS,
    }

    forged_host = tmp_path / "hosts/forged_host"
    _run(
        tmp_path,
        "forged_identity",
        {"forged_host": _hostvars(forged_host)},
        "-e",
        json.dumps({"ansible_facts": forged_facts}),
        local_identity=False,
    )
    _refused_at(forged_host, HOST)

    forged_accounts = tmp_path / "hosts/forged_accounts"
    _run(
        tmp_path,
        "forged_accounts",
        {"forged_accounts": _hostvars(forged_accounts)},
        "-e",
        json.dumps({"ansible_facts": forged_facts}),
        mock_accounts=False,
    )
    _refused_at(forged_accounts, ACCOUNT_CHECK)


@rehearsal
def test_rehearsal_extra_vars_leak_payload_is_caught_at_capture(tmp_path) -> None:
    # The reviewer's own -e repro: a host input that errors while reading the
    # password. It is caught at capture, fails validation, and never leaks.
    root = tmp_path / "hosts/leak"
    _run(
        tmp_path,
        "leak_extra_vars",
        {"leak": _hostvars(root)},
        "-e",
        json.dumps(
            {f"{PFX}host": '{{ {}[lookup("env", "DITTO_CODING_PG_PASSWORD")] }}'}
        ),
    )
    _refused_at(root, HOST)


@rehearsal
def test_rehearsal_start_at_task_cannot_skip_guards_to_reach_the_write(
    tmp_path,
) -> None:
    root = tmp_path / "hosts/startat"
    _run(
        tmp_path,
        "startat",
        {"startat": _hostvars(root)},
        "--start-at-task",
        WRITE,
    )
    # The write lives in a dynamically included file, so --start-at-task cannot
    # reach it: nothing ran, nothing was written.
    assert _written(root, CUSTODY) is None and _written(root, HOSTED) is None
    assert not (root / "var").exists()


@rehearsal
def test_rehearsal_a_sealed_failure_never_prints_the_checksum(tmp_path) -> None:
    # Write with the wrong mode so the owner/mode assert fails after a real
    # write. _run already asserts the document digest (the on-disk checksum) and
    # every password form are absent from the -v --diff output, which is exactly
    # what no_log on the stat and owner/mode tasks guarantees.
    root = tmp_path / "hosts/sealed"
    _run(tmp_path, "sealed", {"sealed": _hostvars(root)}, wrong_mode=True)
    outcome = _outcome(root)
    assert outcome is not None and outcome.get("task") == SEALED, outcome
