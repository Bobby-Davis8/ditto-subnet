"""Native PostgreSQL environment materialization is default-off, unforgeable, silent."""

import copy
import grp
import json
import os
import pwd
import re
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment"
TASKS = (ROLE / "tasks/main.yml").read_text()
# Parsed tasks without YAML comments, for token scans.
PARSED = yaml.safe_dump(yaml.safe_load(TASKS), width=10_000)
PLAYBOOK = ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment.yml"
# The stacked removal role (#1897). Its guards are duplicated, not shared, so a
# parity test compares them whenever both roles are on the same tree.
CLEANUP_ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment_cleanup"
CLEANUP_PLAYBOOK = (
    ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment-cleanup.yml"
)

PASSWORD_LOOKUP = "lookup('env', 'DITTO_CODING_PG_PASSWORD')"
PREFIX = "coding_hosted_postgres_environment_"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
    f"{PREFIX}host": "",
}
CONFIRMATION = "MATERIALIZE NATIVE CODING POSTGRES ENVIRONMENT"
REVISION = "0123456789abcdef0123456789abcdef01234567"
DATABASE_HOST = "10.30.0.5"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"

CUSTODY = "/var/lib/ditto-coding-custody/private/postgres-environment.json"
HOSTED = "/var/lib/ditto-coding-hosted/private/postgres-environment.json"
COPIES = [
    {"path": CUSTODY, "owner": "ditto-coding-custody"},
    {"path": HOSTED, "owner": "ditto-coding-hosted"},
]
OWNERS = ("ditto-coding-custody", "ditto-coding-hosted")

EXPLAIN = "Explain dormant native PostgreSQL environment materialization"
PASSWORD_VARIABLE = "Refuse a password supplied as an Ansible variable"
PRESET = "Refuse preset registered results and undocumented role inputs"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the exact host, source, database address and confirmation"
PASSWORD = "Require one bounded single-line password from the controller environment"
ACCOUNTS = "Inspect host accounts once"
ACCOUNT_CHECK = "Require the distinct worker and custodian identities"
LISTING = "List live worker and custody units"
LIVE = "Refuse to replace credentials unless every listed unit is inactive or failed"
DIRECTORIES = "Create owner-only credential directories for each reader"
WRITE = "Write each reader's own PostgreSQL environment copy without printing it"
REINSPECT = "Reinspect both copies as ownership, mode and digest metadata only"
SEALED = "Require owner-only regular single-link copies"
DIGEST = "Require both copies to hold exactly the rendered document"
REPORT = "Report only that the copies exist"


def _documents(text: str = TASKS) -> list[dict]:
    return yaml.safe_load(text)


def _guards() -> list[dict]:
    return _documents()[0]["block"]


def _block() -> list[dict]:
    return _documents()[1]["block"]


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    (task,) = [task for task in _walk(tasks or _documents()) if task["name"] == name]
    return task


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _registered(documents: list[dict]) -> list[str]:
    return [task["register"] for task in _walk(documents) if "register" in task]


def _scoped(documents: list[dict]) -> list[str]:
    return [name for task in _walk(documents) for name in task.get("vars", {})]


def test_default_off_and_password_only_from_the_controller_environment() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == INPUTS
    assert CONFIRMATION in TASKS
    guards, outer = _documents()
    # The refusals sit in their own top-level block, outside the document scope.
    assert set(guards) == {"name", "block"}
    assert [task["name"] for task in guards["block"]] == [
        EXPLAIN,
        PASSWORD_VARIABLE,
        PRESET,
    ]
    enabled = f"{PREFIX}enabled | bool"
    assert _task(EXPLAIN)["when"] == f"not ({enabled})"
    assert _task(PASSWORD_VARIABLE)["when"] == enabled
    assert _task(PRESET)["when"] == enabled
    assert outer["when"] == enabled
    assert [task["name"] for task in _block()] == [
        IDENTITY,
        HOST,
        PASSWORD,
        ACCOUNTS,
        ACCOUNT_CHECK,
        LISTING,
        LIVE,
        DIRECTORIES,
        WRITE,
        REINSPECT,
        SEALED,
        DIGEST,
        REPORT,
    ]
    # No variable can carry the password; one supplied with -e is refused first.
    assert _task(PASSWORD_VARIABLE)["ansible.builtin.assert"]["that"] == [
        f"{PREFIX}password is not defined"
    ]
    assert set(outer["vars"]) == {f"{PREFIX}entries", f"{PREFIX}document"}
    assert not any("PASSWORD" in entry for entry in outer["vars"][f"{PREFIX}entries"])
    assert PASSWORD_LOOKUP in outer["vars"][f"{PREFIX}document"]
    assert f"{PREFIX}password:" not in TASKS


def test_preset_results_and_document_variables_are_refused_outside_their_scope() -> (
    None
):
    documents = _documents()
    guards = _guards()
    # Nothing is registered and no variable is scoped before the refusals, so
    # every role-prefixed name they see came from defaults or an outside source.
    assert not any("register" in task or "vars" in task for task in _walk(guards))
    assert "vars" not in documents[0]
    registered = _registered(documents)
    assert registered == [
        f"{PREFIX}identity",
        f"{PREFIX}accounts",
        f"{PREFIX}units",
        f"{PREFIX}files",
    ]
    scoped = _scoped(documents)
    assert scoped == [f"{PREFIX}entries", f"{PREFIX}document"]
    that = _task(PRESET)["ansible.builtin.assert"]["that"]
    assert that[:-1] == [f"{name} is not defined" for name in registered + scoped]
    assert _flat(that[-1]) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PREFIX}', wantlist=True) | sort == "
        + "["
        + ", ".join(f"'{name}'" for name in sorted(INPUTS))
        + "]"
    )
    # Every role-prefixed name the tasks use is an input, a registered result, a
    # scoped document variable or the refused password: the list above is total.
    used = set(re.findall(rf"\b{PREFIX}\w+", PARSED))
    assert used == {*INPUTS, *registered, *scoped, f"{PREFIX}password"}
    message = _task(PRESET)["ansible.builtin.assert"]["fail_msg"]
    assert "Nothing was written" in message and "{{" not in message
    assert "{{" not in _task(PASSWORD_VARIABLE)["ansible.builtin.assert"]["fail_msg"]


PROBED_IDENTITY = {
    "hostname": "ditto-coding-hosted-v2",
    "architecture": "x86_64",
    "distribution": "Debian",
    "distribution_major_version": "13",
}


def test_identity_comes_only_from_registered_probes_and_no_facts_are_gathered() -> None:
    identity = _task(IDENTITY)
    assert identity["ansible.builtin.setup"] == {
        "gather_subset": ["!all", "!min", "platform", "distribution"]
    }
    assert identity["register"] == f"{PREFIX}identity"
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    assert that[:5] == [
        "inventory_hostname in groups.get('role_coding_hosted', [])",
        *(
            f"{PREFIX}identity.ansible_facts.ansible_{key} == '{value}'"
            for key, value in PROBED_IDENTITY.items()
        ),
    ]
    accounts = _task(ACCOUNTS)
    assert accounts["ansible.builtin.getent"] == {"database": "passwd"}
    assert accounts["register"] == f"{PREFIX}accounts"
    lines = _task(ACCOUNT_CHECK)["ansible.builtin.assert"]["that"]
    assert len(lines) == 5
    assert all(
        line.startswith(f"{PREFIX}accounts.ansible_facts.getent_passwd")
        for line in lines
    )
    # A -e ansible_facts value replaces gathered facts, so no guard reads them.
    assert PARSED.count("ansible_facts") == (
        PARSED.count(f"{PREFIX}identity.ansible_facts")
        + PARSED.count(f"{PREFIX}accounts.ansible_facts")
    )
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["gather_facts"] is False


def test_revision_and_database_host_refuse_a_trailing_newline() -> None:
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    host_pattern = "^10[.]30[.]0[.]([2-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-3])$"
    assert that[5:] == [
        f"{PREFIX}source_revision is string",
        f"{PREFIX}source_revision is match('^[0-9a-f]{{40}}$')",
        f"{PREFIX}source_revision | length == 40",
        f"{PREFIX}confirmation == '{CONFIRMATION}'",
        f"{PREFIX}host is string",
        f"{PREFIX}host == {PREFIX}host | trim",
        f"{PREFIX}host is match('{host_pattern}')",
    ]
    # Ansible's match test is re.match, whose '$' also accepts one trailing
    # newline; the exact length and the trim comparison are what refuse it.
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
UNIT_PATTERN = (
    "^(ditto-coding-hosted-worker[.]service|ditto-coding-custody@\\\\S+[.]service)"
    "\\\\s+\\\\S+\\\\s+(inactive|failed)(\\\\s|$)"
)


def test_live_unit_refusal_is_an_allow_list() -> None:
    names = [task["name"] for task in _block()]
    assert names.index(LIVE) == names.index(LISTING) + 1
    listing = _task(LISTING)
    assert listing["ansible.builtin.command"]["argv"] == [
        "/usr/bin/systemctl",
        "list-units",
        "--all",
        "--plain",
        "--no-legend",
        "--full",
        "ditto-coding-hosted-worker.service",
        "ditto-coding-custody@*.service",
    ]
    assert listing["register"] == f"{PREFIX}units"
    assert listing["check_mode"] is False and listing["changed_when"] is False
    (refuse,) = _task(LIVE)["ansible.builtin.assert"]["that"]
    assert _flat(refuse) == _flat(
        f"{PREFIX}units.stdout_lines | reject('match', '{UNIT_PATTERN}') "
        "| list | length == 0"
    )
    # Jinja decodes the doubled backslashes; Ansible's match test is re.match.
    regex = re.compile(UNIT_PATTERN.replace("\\\\", "\\"))
    for line in ALLOWED_UNIT_LINES:
        assert regex.match(line), line
    for line in REFUSED_UNIT_LINES:
        assert not regex.match(line), line
    # The listing is the only systemctl use; nothing is stopped or started.
    assert PARSED.count("systemctl") == 1
    for forbidden in ("systemd:", "service:", "state: stopped", "state: started"):
        assert forbidden not in PARSED, forbidden


def test_credentials_are_never_logged_read_back_or_diffed() -> None:
    write = _task(WRITE)
    assert write["no_log"] is True and write["diff"] is False
    for forbidden in (
        "slurp",
        "fetch",
        "set -x",
        "gcloud secrets",
        "extra_vars",
    ):
        assert forbidden not in TASKS
    report = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert "password" not in report.lower()
    assert "{{" not in report
    secret_uses = [
        task["name"]
        for task in _walk(_documents())
        if "block" not in task
        and (
            PASSWORD_LOOKUP in yaml.safe_dump(task, width=10_000)
            or f"{PREFIX}document" in yaml.safe_dump(task)
            or f"{PREFIX}files" in yaml.safe_dump(task)
        )
    ]
    # The preset guard names these variables only to require them undefined.
    assert secret_uses == [PRESET, PASSWORD, WRITE, REINSPECT, SEALED, DIGEST]
    for name in secret_uses[1:]:
        task = _task(name)
        # The owner/mode assert prints only the path label and never the digest.
        if name == SEALED:
            assert "checksum" not in yaml.safe_dump(task)
            assert task["loop_control"]["label"] == "{{ item.item.path }}"
            continue
        assert task.get("no_log") is True, name


def test_copies_are_verified_by_owner_mode_and_digest_only() -> None:
    stat = _task(REINSPECT)
    assert stat["ansible.builtin.stat"] == {
        "path": "{{ item.path }}",
        "follow": False,
        "get_checksum": True,
        "checksum_algorithm": "sha256",
        "get_mime": False,
    }
    # The inspected paths and owners are exactly the written ones.
    assert stat["loop"] == _task(WRITE)["loop"] == COPIES
    assert stat["register"] == f"{PREFIX}files"
    digest = _task(DIGEST)
    assert digest["ansible.builtin.assert"]["that"] == [
        f"item.stat.checksum == {PREFIX}document | hash('sha256')"
    ]
    assert digest["loop"] == f"{{{{ {PREFIX}files.results }}}}"
    sealed = _task(SEALED)
    assert sealed["loop"] == f"{{{{ {PREFIX}files.results }}}}"
    that = sealed["ansible.builtin.assert"]["that"]
    assert "item.stat.pw_name == item.item.owner" in that
    assert "item.stat.mode == '0600'" in that
    assert "item.stat.nlink == 1" in that
    assert "not item.stat.islnk" in that


def test_each_reader_gets_its_own_owner_only_copy_for_the_admitted_principal() -> None:
    write = _task(WRITE)
    assert write["ansible.builtin.copy"]["mode"] == "0600"
    assert write["loop"] == COPIES
    assert write["ansible.builtin.copy"]["content"] == (f"{{{{ {PREFIX}document }}}}")
    entries = _documents()[1]["vars"][f"{PREFIX}entries"]
    assert "POSTGRES_USER=ditto" in entries
    assert "POSTGRES_DB=ditto_platform_prod" in entries
    directories = _task(DIRECTORIES)
    assert directories["ansible.builtin.file"]["mode"] == "0700"
    assert {item["owner"] for item in directories["loop"]} == set(OWNERS)
    homes = dict(
        re.findall(
            r"getent_passwd\['([^']+)'\]\[4\] == '([^']+)'",
            " ".join(_task(ACCOUNT_CHECK)["ansible.builtin.assert"]["that"]),
        )
    )
    for item in directories["loop"]:
        assert str(Path(item["path"]).parent) == homes[item["owner"]]


def test_playbook_group_connection_and_ci_registration() -> None:
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["become"] is True and play["gather_facts"] is False
    assert play["roles"] == ["coding_hosted_postgres_environment"]
    assert set(play) == {"name", "hosts", "become", "gather_facts", "roles"}
    (fixture,) = yaml.safe_load(
        (
            ROOT / "infra/ansible/tests/coding-hosted-postgres-environment.yml"
        ).read_text()
    )
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


def test_docs_describe_every_forgery_guard() -> None:
    docs = (ROOT / "infra/docs/coding-hosted-postgres-v2.md").read_text()
    section = _flat(
        docs.split("## Native environment files", 1)[1].split("\n## ", 1)[0]
    )
    for phrase in (
        "`coding_hosted_postgres_environment_*`",
        "extra vars",
        "`coding_hosted_postgres_environment_units`",
        "`coding_hosted_postgres_environment_document`",
        "gathers no facts",
        "`ansible_facts`",
        "exactly 40 lowercase hex characters",
        "trailing newline",
        "`inactive` or `failed`",
        "`refreshing`",
        "`maintenance`",
        "An empty listing",
    ):
        assert phrase in section, phrase


# Guard parity with the stacked removal role. Both roles carry the same guard
# logic under their own variable prefix; normalizing the prefix must make the
# preset template, identity probe, source-revision lines, unit listing and
# allow-list identical.


def _unscoped(tasks: list[dict], scoped: bool = False) -> Iterator[tuple[dict, bool]]:
    for task in tasks:
        inside = scoped or "vars" in task
        yield task, inside
        for section in ("block", "rescue", "always"):
            yield from _unscoped(task.get(section, []), inside)


def _guard_logic(role: Path, prefix: str, playbook: Path) -> dict:
    documents = _documents((role / "tasks/main.yml").read_text())
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    tasks = [
        (task, scoped) for task, scoped in _unscoped(documents) if "block" not in task
    ]
    registered = _registered(documents)
    scoped_names = _scoped(documents)

    def normalized(value) -> str:
        return _flat(json.dumps(value)).replace(prefix, "<role>_")

    def assertion(fragment: str) -> tuple[int, bool, list[str]]:
        (found,) = [
            (index, scoped, task["ansible.builtin.assert"]["that"])
            for index, (task, scoped) in enumerate(tasks)
            if "ansible.builtin.assert" in task
            and fragment in json.dumps(task["ansible.builtin.assert"]["that"])
        ]
        return found

    preset_index, preset_scoped, preset = assertion("ansible.builtin.varnames")
    inputs = ", ".join(repr(name) for name in sorted(defaults))
    template = [f"{name} is not defined" for name in registered + scoped_names] + [
        f"lookup('ansible.builtin.varnames', '^{prefix}', wantlist=True) | sort == "
        f"[{inputs}]"
    ]
    first_register = min(
        index for index, (task, _) in enumerate(tasks) if "register" in task
    )
    (probe,) = [task for task, _ in tasks if "ansible.builtin.setup" in task]
    (listing,) = [
        task
        for task, _ in tasks
        if task.get("ansible.builtin.command", {}).get("argv", [""])[0]
        == "/usr/bin/systemctl"
    ]
    _, _, host = assertion("inventory_hostname")
    _, _, allow_list = assertion(f"{prefix}units.stdout_lines")
    revision_lines = ("inventory_hostname", f"{prefix}identity.", f"{prefix}source_")
    (play,) = yaml.safe_load(playbook.read_text())
    return {
        "preset_follows_template": [_flat(line) for line in preset]
        == [_flat(line) for line in template],
        "preset_lists_every_role_name": set(
            re.findall(rf"\b{prefix}\w+", json.dumps(documents))
        )
        <= {*defaults, *registered, *scoped_names, f"{prefix}password"},
        "preset_precedes_every_register": preset_index < first_register,
        "preset_outside_every_vars_scope": not preset_scoped,
        "probe": normalized([probe["ansible.builtin.setup"], probe["register"]]),
        "identity_and_revision": normalized(
            [line for line in host if line.startswith(revision_lines)]
        ),
        "listing": normalized(
            [listing["ansible.builtin.command"], listing["register"]]
        ),
        "allow_list": normalized(allow_list),
        "gather_facts": play["gather_facts"],
    }


def test_guard_logic_follows_the_shared_template() -> None:
    logic = _guard_logic(ROLE, PREFIX, PLAYBOOK)
    for key in (
        "preset_follows_template",
        "preset_lists_every_role_name",
        "preset_precedes_every_register",
        "preset_outside_every_vars_scope",
    ):
        assert logic[key] is True, key
    assert logic["gather_facts"] is False
    assert logic["identity_and_revision"] == _flat(
        json.dumps(
            [
                "inventory_hostname in groups.get('role_coding_hosted', [])",
                *(
                    f"<role>_identity.ansible_facts.ansible_{key} == '{value}'"
                    for key, value in PROBED_IDENTITY.items()
                ),
                "<role>_source_revision is string",
                "<role>_source_revision is match('^[0-9a-f]{40}$')",
                "<role>_source_revision | length == 40",
            ]
        )
    )


@pytest.mark.skipif(
    not CLEANUP_ROLE.exists(),
    reason="the coding_hosted_postgres_environment_cleanup role is not on this tree",
)
def test_guard_logic_matches_the_cleanup_role() -> None:
    assert _guard_logic(
        CLEANUP_ROLE, "coding_hosted_postgres_environment_cleanup_", CLEANUP_PLAYBOOK
    ) == _guard_logic(ROLE, PREFIX, PLAYBOOK)


# Local rehearsal: the role's own enabled tasks run through ansible-core 2.21.2
# against a temporary tree. Only this test rewrites paths, owners, the account
# and unit inspections and, outside the forged-identity pass, compares the
# identity probe with an independent probe of this machine. The role has no
# such inputs.

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
# A stand-in, never a real credential; the rehearsal asserts it is never logged.
REHEARSAL_PASSWORD = 'rehearsal-only "stand-in"=pass\\word/42'


def _rehearsal_tasks(*, production_identity: bool = False) -> list[dict]:
    tasks = copy.deepcopy(_documents())
    if not production_identity:
        host = _task(HOST, tasks)["ansible.builtin.assert"]
        probed = f"{PREFIX}identity.ansible_facts."
        rewritten = []
        for line in host["that"]:
            match = re.fullmatch(rf"{re.escape(probed)}(ansible_\w+) == '[^']+'", line)
            rewritten.append(
                f"{probed}{match[1]} == rehearsal_local_identity.ansible_facts."
                f"{match[1]}"
                if match
                else line
            )
        assert (
            sum(old != new for old, new in zip(host["that"], rewritten, strict=True))
            == 4
        )
        host["that"] = rewritten
    accounts = _task(ACCOUNTS, tasks)
    assert accounts.pop("ansible.builtin.getent") == {"database": "passwd"}
    # set_fact returns the same ansible_facts.getent_passwd shape as getent.
    accounts["ansible.builtin.set_fact"] = {"getent_passwd": "{{ rehearsal_accounts }}"}
    _task(LISTING, tasks)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_units }}",
    ]
    _task(REPORT, tasks)["register"] = "rehearsal_report"
    for name, module in (
        (DIRECTORIES, "ansible.builtin.file"),
        (WRITE, "ansible.builtin.copy"),
    ):
        arguments = _task(name, tasks)[module]
        assert arguments["group"] == "{{ item.owner }}"
        arguments["group"] = "{{ rehearsal_group }}"

    def rewrite(value):
        if isinstance(value, dict):
            # Guard expressions keep their literals; only task arguments move.
            return {
                key: item if key == "ansible.builtin.assert" else rewrite(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, str) and value in OWNERS:
            return "{{ rehearsal_owner }}"
        if isinstance(value, str):
            return value.replace("/var/lib/", "{{ rehearsal_root }}/var/lib/")
        return value

    tasks = rewrite(tasks)
    unrooted = [
        task["name"]
        for task in _walk(tasks)
        if "block" not in task and re.search(r"(?<!\}\})/var/lib/", json.dumps(task))
    ]
    # Only the account home literals stay; they are compared, never opened.
    assert unrooted == [ACCOUNT_CHECK]
    rendered = json.dumps(tasks)
    assert "systemctl" not in rendered
    assert not any(f'"{owner}"' in rendered for owner in OWNERS)
    assert tasks[0] == _documents()[0]
    assert _task(PRESET, tasks) == _task(PRESET)
    assert tasks[1]["vars"] == _documents()[1]["vars"]
    return tasks


def _play(tasks: list[dict], rehearsal_pass: str, hosts: str = "all") -> dict:
    outcome = "{{ rehearsal_root }}/outcome-{{ rehearsal_pass }}.json"
    return {
        "name": f"Rehearse materialization ({rehearsal_pass})",
        "hosts": hosts,
        "strategy": "free",
        "connection": "local",
        "gather_facts": False,
        "become": False,
        "vars": {
            "ansible_python_interpreter": "{{ ansible_playbook_python }}",
            "rehearsal_pass": rehearsal_pass,
        },
        "tasks": [
            {
                "name": "Probe this machine independently of the role",
                "ansible.builtin.setup": {
                    "gather_subset": ["!all", "!min", "platform", "distribution"]
                },
                "register": "rehearsal_local_identity",
            },
            {
                "name": "Rehearse the materialization tasks",
                "block": [
                    *tasks,
                    {
                        "name": "Record completion",
                        "ansible.builtin.copy": {
                            "dest": outcome,
                            "content": (
                                "{{ {'report': rehearsal_report.msg} | to_json }}"
                            ),
                        },
                    },
                ],
                "rescue": [
                    {
                        "name": "Record refusal",
                        "ansible.builtin.copy": {
                            "dest": outcome,
                            "content": (
                                "{{ {'task': ansible_failed_task.name, 'messages': "
                                "[ansible_failed_result.msg | default('')] + "
                                "(ansible_failed_result.results | default([]) "
                                "| selectattr('failed', 'defined') "
                                "| selectattr('failed') "
                                "| map(attribute='msg') | list)} | to_json }}"
                            ),
                        },
                    }
                ],
            },
        ],
    }


def _run(tmp_path: Path, name: str, hosts: dict, plays: list[dict], *flags) -> None:
    work = tmp_path / name
    work.mkdir()
    # Every rehearsal host is in the real group, so only a guard can refuse it.
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": hosts}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "rehearsal.yml").write_text(yaml.safe_dump(plays, sort_keys=False))
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANSIBLE_") and key != "DITTO_CODING_PG_PASSWORD"
    }
    environment |= {
        "ANSIBLE_HOME": str(work / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(work / "ansible-tmp"),
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
            "-i",
            "inventory.yml",
            *flags,
            "rehearsal.yml",
        ],
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr
    # no_log holds even under -e and on refusal: the stand-in never reaches output.
    assert REHEARSAL_PASSWORD not in completed.stdout + completed.stderr


def _hosts(roots: dict[str, Path]) -> dict[str, dict]:
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    for root in roots.values():
        root.mkdir(parents=True)
    return {
        name: {
            f"{PREFIX}enabled": True,
            f"{PREFIX}confirmation": CONFIRMATION,
            f"{PREFIX}source_revision": REVISION,
            f"{PREFIX}host": DATABASE_HOST,
            "rehearsal_root": str(root),
            "rehearsal_units": STOPPED_UNITS,
            "rehearsal_owner": owner,
            "rehearsal_group": group,
            "rehearsal_accounts": REHEARSAL_ACCOUNTS,
        }
        for name, root in roots.items()
    }


def _outcome(root: Path, rehearsal_pass: str) -> dict:
    return json.loads((root / f"outcome-{rehearsal_pass}.json").read_text())


def _assert_refused(root: Path, task: str, rehearsal_pass: str) -> None:
    outcome = _outcome(root, rehearsal_pass)
    assert outcome.get("task") == task, (root.name, outcome)
    expected = _flat(_task(task)["ansible.builtin.assert"]["fail_msg"])
    assert expected in [_flat(item) for item in outcome["messages"]], (
        root.name,
        outcome,
    )
    # Every refusal precedes directory creation: nothing exists under the root.
    assert not (root / "var").exists(), root.name


def _assert_materialized(root: Path, rehearsal_pass: str) -> None:
    assert _outcome(root, rehearsal_pass) == {
        "report": _flat(_task(REPORT)["ansible.builtin.debug"]["msg"])
    }
    entries = [
        entry.replace(f"{{{{ {PREFIX}host }}}}", DATABASE_HOST)
        for entry in _documents()[1]["vars"][f"{PREFIX}entries"]
    ]
    for item in COPIES:
        path = root / item["path"].lstrip("/")
        assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink == 1
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert json.loads(path.read_text()) == [
            *entries,
            f"POSTGRES_PASSWORD={REHEARSAL_PASSWORD}",
        ]


# The rehearsal needs uvx, network access and coreutils, so root pytest shards
# skip it and run only the structural tests above. The infra-ci Ansible job sets
# the gate; with it set nothing else can skip, so a missing tool fails.
rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)

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


@rehearsal
def test_rehearsal_materializes_only_when_every_guard_passes(tmp_path) -> None:
    names = [
        "materialized",
        "no_units",
        *LIVE_UNITS,
        "revision_newline",
        "host_newline",
        "inventory_result",
        "inventory_document",
        "undocumented_input",
        "password_variable",
    ]
    roots = {name: tmp_path / "hosts" / name for name in names}
    hosts = _hosts(roots)
    hosts["no_units"]["rehearsal_units"] = ""
    for name, line in LIVE_UNITS.items():
        hosts[name]["rehearsal_units"] = STOPPED_UNITS + line + "\n"
    hosts["revision_newline"][f"{PREFIX}source_revision"] = REVISION + "\n"
    hosts["host_newline"][f"{PREFIX}host"] = DATABASE_HOST + "\n"
    # Inventory values are refused exactly like extra vars.
    hosts["inventory_result"][f"{PREFIX}units"] = {"stdout": "", "stdout_lines": []}
    hosts["inventory_document"][f"{PREFIX}document"] = "[]"
    hosts["undocumented_input"][f"{PREFIX}user"] = "postgres"
    hosts["password_variable"][f"{PREFIX}password"] = "rehearsal-variable"

    _run(tmp_path, "run", hosts, [_play(_rehearsal_tasks(), "first")])

    for name in ("materialized", "no_units"):
        _assert_materialized(roots[name], "first")
    refusals = {
        **dict.fromkeys(LIVE_UNITS, LIVE),
        "revision_newline": HOST,
        "host_newline": HOST,
        "inventory_result": PRESET,
        "inventory_document": PRESET,
        "undocumented_input": PRESET,
        "password_variable": PASSWORD_VARIABLE,
    }
    assert set(refusals) | {"materialized", "no_units"} == set(names)
    for name, task in refusals.items():
        _assert_refused(roots[name], task, "first")


@rehearsal
def test_rehearsal_refuses_extra_vars_that_preset_results_documents_or_facts(
    tmp_path,
) -> None:
    forged_results = ("forged_units", "forged_identity", "forged_document")
    roots = {
        name: tmp_path / "hosts" / name
        for name in (*forged_results, "forged_host", "forged_accounts")
    }
    hosts = _hosts(roots)
    hosts["forged_units"]["rehearsal_units"] = LIVE_UNITS["refreshing"] + "\n"

    # Each forged value would satisfy the guard it replaces if the preset
    # refusal were missing, on a host whose other guards pass: an empty listing
    # over a refreshing unit, the dedicated host's probe against the production
    # identity check, and an attacker-chosen document the digest would match.
    forged = {
        "forged_units": (
            {f"{PREFIX}units": {"stdout": "", "stdout_lines": [], "rc": 0}},
            False,
        ),
        "forged_identity": (
            {
                f"{PREFIX}identity": {
                    "ansible_facts": {
                        f"ansible_{key}": value
                        for key, value in PROBED_IDENTITY.items()
                    }
                }
            },
            True,
        ),
        "forged_document": (
            {f"{PREFIX}document": json.dumps(["POSTGRES_HOST=203.0.113.9"])},
            False,
        ),
    }
    assert tuple(forged) == forged_results
    for name, (extra_vars, production_identity) in forged.items():
        tasks = _rehearsal_tasks(production_identity=production_identity)
        _run(
            tmp_path,
            name,
            {name: hosts[name]},
            [_play(tasks, "forged", name)],
            "-e",
            json.dumps(extra_vars),
        )
        _assert_refused(roots[name], PRESET, "forged")

    # Forged facts that describe the dedicated host and valid accounts must not
    # pass the checks that read registered probes. The accounts host really has
    # no custodian, so only the forged getent_passwd could satisfy its check.
    assert socket.gethostname().split(".")[0] != PROBED_IDENTITY["hostname"]
    hosts["forged_accounts"]["rehearsal_accounts"] = {
        "ditto-coding-hosted": REHEARSAL_ACCOUNTS["ditto-coding-hosted"]
    }
    forged_facts = {
        **PROBED_IDENTITY,
        **{f"ansible_{key}": value for key, value in PROBED_IDENTITY.items()},
        "getent_passwd": REHEARSAL_ACCOUNTS,
    }
    _run(
        tmp_path,
        "facts",
        {name: hosts[name] for name in ("forged_host", "forged_accounts")},
        [
            _play(_rehearsal_tasks(production_identity=True), "facts", "forged_host"),
            _play(_rehearsal_tasks(), "facts", "forged_accounts"),
        ],
        "-e",
        json.dumps({"ansible_facts": forged_facts}),
    )
    _assert_refused(roots["forged_host"], HOST, "facts")
    _assert_refused(roots["forged_accounts"], ACCOUNT_CHECK, "facts")
