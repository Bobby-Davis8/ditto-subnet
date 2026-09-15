"""Native PostgreSQL environment removal stays default-off, surgical and silent."""

import copy
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
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment_cleanup"
TASKS = (ROLE / "tasks/main.yml").read_text()
# Parsed tasks without YAML comments, for forbidden-token scans.
PARSED = yaml.safe_dump(yaml.safe_load(TASKS), width=10_000)
# The materialization role's guarded tasks live in its dynamically included file.
MATERIALIZE = yaml.safe_load(
    (
        ROOT
        / "infra/ansible/roles/coding_hosted_postgres_environment/tasks/materialize.yml"
    ).read_text()
)
MATERIALIZE_HOST = "Require the exact host, source, database address and confirmation"

CUSTODY = "/var/lib/ditto-coding-custody/private/postgres-environment.json"
HOSTED = "/var/lib/ditto-coding-hosted/private/postgres-environment.json"
COPIES = [CUSTODY, HOSTED]
PARENTS = [
    "/var/lib/ditto-coding-custody",
    "/var/lib/ditto-coding-custody/private",
    "/var/lib/ditto-coding-hosted",
    "/var/lib/ditto-coding-hosted/private",
]
OWNERS = ("ditto-coding-custody", "ditto-coding-hosted")
PREFIX = "coding_hosted_postgres_environment_cleanup_"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
}
REVISION_VAR = f"{PREFIX}source_revision"
VALIDATED_REVISION = (
    f"{{{{ {REVISION_VAR} if {REVISION_VAR} is string and {REVISION_VAR} is "
    f"match('^[0-9a-f]{{40}}$') and {REVISION_VAR} | length == 40 else 'invalid' }}}}"
)
REVISION = "0123456789abcdef0123456789abcdef01234567"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"
PLAYBOOK = (
    ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment-cleanup.yml"
)

PRESET = "Refuse preset registered results and undocumented role inputs"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the exact host, source and removal confirmation"
LISTING = "List live worker and custody units"
LIVE = "Refuse to remove credentials unless every listed unit is inactive or failed"
PARENT_STAT = "Inspect the reader directories without following links"
PARENT_CHECK = "Refuse a linked or non-directory parent"
COPY_STAT = "Inspect both exact copies as link metadata only"
COPY_CHECK = (
    "Refuse anything but an absent copy or a regular single-link copy owned by its "
    "reader"
)
REMOVAL = "Unlink the present copies and report any partial removal"
UNLINK = "Unlink each exact copy that exists and nothing else"
PARTIAL = "Report the copies unlinked before the removal failure and stop"
AFTER_STAT = "Reinspect both exact paths without following links"
AFTER_CHECK = "Require both exact copies to be absent"
REPORT = "Report only the source revision and exact paths removed or already absent"


def _block() -> list[dict]:
    return yaml.safe_load(TASKS)[1]["block"]


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    (task,) = [task for task in _walk(tasks or _block()) if task["name"] == name]
    return task


def _module(task: dict) -> str:
    (module,) = set(task) - {
        "name",
        "loop",
        "loop_control",
        "register",
        "when",
        "changed_when",
        "check_mode",
        "rescue",
    }
    return module


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def test_default_off_with_exact_confirmation_source_and_probed_host() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == INPUTS
    explain, outer = yaml.safe_load(TASKS)
    assert explain["when"] == (
        "not (coding_hosted_postgres_environment_cleanup_enabled | bool)"
    )
    assert "no credential file is removed" in explain["ansible.builtin.debug"]["msg"]
    assert outer["when"] == "coding_hosted_postgres_environment_cleanup_enabled | bool"
    assert "vars" not in outer
    assert [task["name"] for task in _block()[:3]] == [PRESET, IDENTITY, HOST]

    identity = _task(IDENTITY)
    assert identity["ansible.builtin.setup"] == {
        "gather_subset": ["!all", "!min", "platform", "distribution"]
    }
    assert identity["register"] == f"{PREFIX}identity"

    # Same literals as the materialization host check, both read from a
    # registered probe: a -e ansible_facts value replaces gathered facts.
    materialize_host = _task(MATERIALIZE_HOST, MATERIALIZE)
    inventory, *facts = materialize_host["ansible.builtin.assert"]["that"][:5]
    assert inventory == "inventory_hostname in groups.get('role_coding_hosted', [])"
    probed = []
    for line in facts:
        match = re.fullmatch(
            r"coding_hosted_postgres_environment_identity\.ansible_facts\."
            r"ansible_(\w+) == '([^']+)'",
            line,
        )
        assert match, line
        probed.append(
            f"{PREFIX}identity.ansible_facts.ansible_{match[1]} == '{match[2]}'"
        )
    assert _task(HOST)["ansible.builtin.assert"]["that"] == [
        inventory,
        *probed,
        f"{REVISION_VAR} is string",
        f"{REVISION_VAR} is match('^[0-9a-f]{{40}}$')",
        f"{REVISION_VAR} | length == 40",
        f"{PREFIX}confirmation == 'REMOVE NATIVE CODING POSTGRES ENVIRONMENT'",
    ]
    hostname = "ansible_hostname == 'ditto-coding-hosted-v2'"
    assert f"{PREFIX}identity.ansible_facts.{hostname}" in probed
    assert PARSED.count("ansible_facts") == PARSED.count(
        f"{PREFIX}identity.ansible_facts"
    )
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["gather_facts"] is False


def test_preset_results_and_undocumented_inputs_are_refused_before_any_register() -> (
    None
):
    block = _block()
    preset = block[0]
    assert preset["name"] == PRESET
    assert not any("register" in task for task in _walk([preset]))
    registered = [task["register"] for task in _walk(block) if "register" in task]
    assert len(registered) == len(set(registered)) == 6
    assert all(name.startswith(PREFIX) for name in registered)
    that = preset["ansible.builtin.assert"]["that"]
    assert that[:-1] == [f"{name} is not defined" for name in registered]
    assert _flat(that[-1]) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PREFIX}', wantlist=True) | sort == "
        + "["
        + ", ".join(f"'{name}'" for name in sorted(INPUTS))
        + "]"
    )
    assert f"source_revision={VALIDATED_REVISION}" in _flat(
        preset["ansible.builtin.assert"]["fail_msg"]
    )
    assert "Nothing was removed" in preset["ansible.builtin.assert"]["fail_msg"]


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


def test_live_unit_refusal_is_an_allow_list_over_the_materialization_listing() -> None:
    names = [task["name"] for task in _block()]
    assert names[3:5] == [LISTING, LIVE]
    listing = _task(LISTING)
    original = _task("List live worker and custody units", MATERIALIZE)
    assert listing["ansible.builtin.command"] == original["ansible.builtin.command"]
    assert listing["register"] == f"{PREFIX}units"
    assert listing["check_mode"] is False and listing["changed_when"] is False
    (refuse,) = _task(LIVE)["ansible.builtin.assert"]["that"]
    pattern = (
        "^(ditto-coding-hosted-worker[.]service|ditto-coding-custody@\\\\S+[.]service)"
        "\\\\s+\\\\S+\\\\s+(inactive|failed)(\\\\s|$)"
    )
    assert _flat(refuse) == _flat(
        f"{PREFIX}units.stdout_lines | reject('match', '{pattern}') "
        "| list | length == 0"
    )
    # Jinja decodes the doubled backslashes; Ansible's match test is re.match.
    regex = re.compile(pattern.replace("\\\\", "\\"))
    for line in ALLOWED_UNIT_LINES:
        assert regex.match(line), line
    for line in REFUSED_UNIT_LINES:
        assert not regex.match(line), line
    # The listing is the only systemctl use; nothing is stopped, killed or disabled.
    assert PARSED.count("systemctl") == 1
    for forbidden in ("systemd", "service:", "state: stopped", "kill", "is-active"):
        assert forbidden not in PARSED, forbidden


def test_only_the_two_exact_copies_are_ever_removed() -> None:
    tasks = list(_walk(yaml.safe_load(TASKS)))
    modules = {_module(task) for task in tasks}
    # No file, shell, copy, find or other module that could create, recurse or read.
    assert modules == {
        "block",
        "ansible.builtin.assert",
        "ansible.builtin.command",
        "ansible.builtin.debug",
        "ansible.builtin.fail",
        "ansible.builtin.setup",
        "ansible.builtin.stat",
    }
    commands = [task["name"] for task in tasks if "ansible.builtin.command" in task]
    assert commands == [LISTING, UNLINK]
    unlink = _task(UNLINK)
    assert unlink["ansible.builtin.command"] == {
        "argv": ["/usr/bin/unlink", "{{ item }}"]
    }
    assert unlink["loop"] == COPIES
    assert unlink["changed_when"] is True
    literals = set(re.findall(r"/var/lib/[^\s'\",\]}]*", TASKS))
    assert literals == {*COPIES, *PARENTS}
    for forbidden in (
        "state: absent",
        "ansible.builtin.file",
        "rmtree",
        "rm ",
        "rmdir",
        "recurse",
        "find",
        "fileglob",
        "with_",
        "removes:",
        "*.json",
    ):
        assert forbidden not in PARSED, forbidden


def _only(tasks: list[dict], module: str) -> dict:
    (task,) = [task for task in tasks if module in task]
    return task


def test_targets_and_owners_equal_the_materialization_write_loop() -> None:
    # The cleanup literals are copied from coding_hosted_postgres_environment;
    # parse that role so any drift in a path, owner or home fails here.
    written = _only(MATERIALIZE, "ansible.builtin.copy")["loop"]
    created = _only(MATERIALIZE, "ansible.builtin.file")["loop"]
    asserted = " ".join(
        line
        for task in MATERIALIZE
        if "ansible.builtin.assert" in task
        for line in task["ansible.builtin.assert"]["that"]
    )
    homes = dict(re.findall(r"getent_passwd\['([^']+)'\]\[4\] == '([^']+)'", asserted))
    assert set(homes) == set(OWNERS)
    assert [item["owner"] for item in written] == list(OWNERS)
    assert [item["path"] for item in written] == COPIES

    assert _task(COPY_STAT)["loop"] == written
    assert _task(UNLINK)["loop"] == [item["path"] for item in written]
    assert _task(AFTER_STAT)["loop"] == [item["path"] for item in written]

    parents: list[str] = []
    for item in written:
        (directory,) = [
            entry
            for entry in created
            if entry["path"] == str(Path(item["path"]).parent)
        ]
        assert directory["owner"] == item["owner"]
        assert str(Path(directory["path"]).parent) == homes[item["owner"]]
        parents += [homes[item["owner"]], directory["path"]]
    assert _task(PARENT_STAT)["loop"] == parents == PARENTS
    assert set(re.findall(r"/var/lib/[^\s'\",\]}]*", TASKS)) == {*parents, *COPIES}


def test_lstat_safety_checks_precede_removal_and_never_read_contents() -> None:
    block = _block()
    assert [task["name"] for task in block] == [
        PRESET,
        IDENTITY,
        HOST,
        LISTING,
        LIVE,
        PARENT_STAT,
        PARENT_CHECK,
        COPY_STAT,
        COPY_CHECK,
        REMOVAL,
        AFTER_STAT,
        AFTER_CHECK,
        REPORT,
    ]
    for name in (PARENT_STAT, COPY_STAT, AFTER_STAT):
        stat = _task(name)["ansible.builtin.stat"]
        assert stat["follow"] is False
        assert stat["get_checksum"] is False
        assert stat["get_mime"] is False
        assert stat["get_attributes"] is False
    assert _task(PARENT_STAT)["loop"] == PARENTS
    assert _task(PARENT_CHECK)["ansible.builtin.assert"]["that"] == [
        "not item.stat.exists or (item.stat.isdir and not item.stat.islnk)"
    ]
    assert _task(COPY_STAT)["loop"] == [
        {"path": CUSTODY, "owner": "ditto-coding-custody"},
        {"path": HOSTED, "owner": "ditto-coding-hosted"},
    ]
    (sealed,) = _task(COPY_CHECK)["ansible.builtin.assert"]["that"]
    assert _flat(sealed) == (
        "not item.stat.exists or (item.stat.isreg and not item.stat.islnk and "
        "item.stat.nlink == 1 and item.stat.pw_name | default('') == item.item.owner)"
    )
    # Removal is limited to the copies whose lstat result proved them present.
    assert _flat(_task(UNLINK)["when"]) == (
        "item in (coding_hosted_postgres_environment_cleanup_copies.results | "
        "selectattr('stat.exists') | map(attribute='item.path') | list)"
    )
    assert _task(AFTER_STAT)["loop"] == COPIES
    assert _task(AFTER_CHECK)["ansible.builtin.assert"]["that"] == [
        "not item.stat.exists"
    ]
    for forbidden in (
        "slurp",
        "fetch",
        "checksum: true",
        "content",
        "DITTO_CODING_PG_PASSWORD",
        "coding_hosted_postgres_environment_password",
        "gcloud",
        "set -x",
    ):
        assert forbidden not in PARSED, forbidden
    # The only lookup lists variable names; nothing reads env, files or pipes.
    assert PARSED.count("lookup(") == 1
    assert PARSED.count("lookup('ansible.builtin.varnames'") == 1


def test_removal_failure_reports_partial_progress_and_still_fails() -> None:
    removal = _task(REMOVAL)
    assert set(removal) == {"name", "block", "rescue"}
    assert [task["name"] for task in removal["block"]] == [UNLINK]
    assert [task["name"] for task in removal["rescue"]] == [PARTIAL]
    unlinked = f"{PREFIX}unlinked"
    assert _task(UNLINK)["register"] == unlinked
    succeeded = (
        f"{unlinked}.results | default([]) | selectattr('rc', 'defined') | "
        "selectattr('rc', 'equalto', 0) | map(attribute='item')"
    )
    # ansible.builtin.fail re-raises: a rescued removal failure still fails the host.
    assert _flat(_task(PARTIAL)["ansible.builtin.fail"]["msg"]) == _flat(
        "Native Coding PostgreSQL environment cleanup failed during removal; "
        f"source_revision={{{{ {REVISION_VAR} }}}}; "
        f"removed={{{{ {succeeded} | list | to_json }}}}; "
        f"not_removed={{{{ {PREFIX}copies.results | selectattr('stat.exists') | "
        f"map(attribute='item.path') | reject('in', {succeeded} | list) "
        "| list | to_json }}; "
        "reinspect both paths and reconcile by hand before re-running."
    )


def test_revision_is_reported_on_success_and_in_every_failure_message() -> None:
    for name in (PRESET, HOST):
        message = _flat(_task(name)["ansible.builtin.assert"]["fail_msg"])
        assert message.endswith(f"source_revision={VALIDATED_REVISION}"), name
    # Later guards run only after HOST validated the revision's exact shape.
    for name in (LIVE, PARENT_CHECK, COPY_CHECK, AFTER_CHECK):
        message = _flat(_task(name)["ansible.builtin.assert"]["fail_msg"])
        assert message.endswith(f"source_revision={{{{ {REVISION_VAR} }}}}"), name
    report = _flat(_task(REPORT)["ansible.builtin.debug"]["msg"])
    assert report == _flat(
        "Native Coding PostgreSQL environment cleanup; "
        f"source_revision={{{{ {REVISION_VAR} }}}}; "
        "{{ 'would_remove' if ansible_check_mode else 'removed' }}={{ "
        f"{PREFIX}copies.results | selectattr('stat.exists') | "
        "map(attribute='item.path') | list | to_json }}; already_absent={{ "
        f"{PREFIX}copies.results | rejectattr('stat.exists') | "
        "map(attribute='item.path') | list | to_json }}; "
        "directories_kept=true; services_stopped=false; password_read=false."
    )


def test_playbook_fixture_ci_and_docs_registration() -> None:
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["become"] is True and play["gather_facts"] is False
    assert play["roles"] == ["coding_hosted_postgres_environment_cleanup"]
    assert set(play) == {"name", "hosts", "become", "gather_facts", "roles"}
    (fixture,) = yaml.safe_load(
        (
            ROOT / "infra/ansible/tests/coding-hosted-postgres-environment-cleanup.yml"
        ).read_text()
    )
    assert fixture["hosts"] == "localhost" and fixture["connection"] == "local"
    assert fixture["become"] is False and fixture["gather_facts"] is False
    assert fixture["roles"] == ["coding_hosted_postgres_environment_cleanup"]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-postgres-environment-cleanup.yml" in workflow
    assert "tests/coding-hosted-postgres-environment-cleanup.yml" in workflow
    docs = (ROOT / "infra/docs/coding-hosted-postgres-v2.md").read_text()
    section = _flat(docs.split("## Removal and rotation", 1)[1])
    assert "REMOVE NATIVE CODING POSTGRES ENVIRONMENT" in section
    assert "platform-db-password" in section
    assert "does not rotate" in section
    assert "`inactive` or `failed`" in section
    assert "`refreshing`" in section
    assert "extra vars" in section


def test_docs_name_every_password_holder_and_order_rotation() -> None:
    docs = (ROOT / "infra/docs/coding-hosted-postgres-v2.md").read_text()
    section = _flat(docs.split("## Removal and rotation", 1)[1])
    assert "does not revoke any credential" in section
    runtime = (
        ROOT / "apps/platform/ditto/api_server/coding_hosted_runtime.py"
    ).read_text()
    # The per-run copy the docs name is still written with the full entry list.
    assert '"postgres.json",' in runtime and "config.postgres_entries" in runtime
    plan_apply = (ROOT / ".github/workflows/infra-plan-apply.yml").read_text()
    assert "TF_VAR_db_password: ${{ secrets.PLATFORM_DB_PASSWORD }}" in plan_apply
    for holder in (
        *COPIES,
        "`<runtime_root>/postgres.json`",
        "`write_worker_config`",
        "retained evidence",
        "`PLATFORM_DB_PASSWORD`",
        "`infra-plan`",
        "`TF_VAR_db_password`",
        "`platform-db-password`",
        "gs://ditto-app-dev-tfstate/gcp-platform",
        "gs://ditto-app-dev-tfstate/ci-plans/gcp-platform/",
        "/opt/ditto/secrets/postgres-ditto.password",
        "`apps/platform/.env`",
        "`DITTO_PG_PASSWORD`",
        "`DITTO_CODING_PG_PASSWORD`",
    ):
        assert holder in section, holder
    steps = [
        "1. **Stop.**",
        "2. **Rotate the `ditto` password.**",
        "3. **Clean up.**",
        "4. **Re-materialize.**",
        "5. **Verify.**",
    ]
    positions = [section.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "it is not complete without step 2" in section
    assert "must not be extended to" in section


def test_rehearsal_runs_only_in_the_infra_ansible_job() -> None:
    workflows = ROOT / ".github/workflows"
    infra = yaml.safe_load((workflows / "infra-ci.yml").read_text())
    this_file = str(Path(__file__).relative_to(ROOT))
    for trigger in ("pull_request", "push"):
        assert this_file in infra[True][trigger]["paths"]
    # The materialization rehearsal shares the gate, so select this file's step.
    (step,) = [
        step
        for job in infra["jobs"].values()
        for step in job["steps"]
        if REHEARSAL_GATE in step.get("env", {}) and this_file in step["run"]
    ]
    assert step in infra["jobs"]["ansible"]["steps"]
    assert step["env"] == {REHEARSAL_GATE: "1"}
    assert step["working-directory"] == "${{ github.workspace }}"
    # Lock-pinned pytest plugins; the locked dev group already brings PyYAML
    # 6.0.3, so no --with and no project install.
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
    # The job installs uv before any step that uses it.
    uses = [step.get("uses", "") for step in infra["jobs"]["ansible"]["steps"]]
    assert any(action.startswith("astral-sh/setup-uv@") for action in uses)
    for other in workflows.glob("*.yml"):
        if other.name != "infra-ci.yml":
            assert REHEARSAL_GATE not in other.read_text(), other.name


# Local rehearsal: the role's own enabled tasks run through ansible-core 2.21.2
# against a temporary tree. Only this test rewrites paths, owners and the unit
# listing; the role has no such inputs. Normal passes drop the identity probe
# and host check, which only the dedicated forged-identity pass keeps.

STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
    "ditto-coding-custody@1.service not-found inactive dead ditto-coding-custody@1\n"
)


def _rehearsal_tasks(*, identity: bool = False) -> list[dict]:
    block = copy.deepcopy(_block())
    if not identity:
        assert [block.pop(1)["name"], block.pop(1)["name"]] == [IDENTITY, HOST]
    _task(LISTING, block)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_units }}",
    ]
    _task(REPORT, block)["register"] = "rehearsal_report"

    def rewrite(value):
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, str) and value in OWNERS:
            return "{{ rehearsal_owners['" + value + "'] }}"
        if isinstance(value, str):
            return value.replace("/var/lib/", "{{ rehearsal_root }}/var/lib/")
        return value

    tasks = rewrite(block)
    rendered = json.dumps(tasks)
    assert rendered.count("/var/lib/") == rendered.count(
        "{{ rehearsal_root }}/var/lib/"
    )
    assert "systemctl" not in rendered
    assert not any(f'"{owner}"' in rendered for owner in OWNERS)
    assert _task(PRESET, tasks) == _task(PRESET)
    return tasks


def _play(tasks: list[dict], rehearsal_pass: str) -> dict:
    outcome = "{{ rehearsal_root }}/outcome-{{ rehearsal_pass }}.json"
    return {
        "name": f"Rehearse cleanup ({rehearsal_pass})",
        "hosts": "all",
        "strategy": "free",
        "connection": "local",
        "gather_facts": False,
        "become": False,
        "vars": {
            "ansible_python_interpreter": "{{ ansible_playbook_python }}",
            "rehearsal_pass": rehearsal_pass,
            # Exactly the three documented inputs, as the role defaults provide.
            f"{PREFIX}enabled": True,
            f"{PREFIX}confirmation": "REMOVE NATIVE CODING POSTGRES ENVIRONMENT",
            f"{PREFIX}source_revision": REVISION,
        },
        "tasks": [
            {
                "name": "Rehearse the enabled cleanup tasks",
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
                        "check_mode": False,
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
                        "check_mode": False,
                    }
                ],
            }
        ],
    }


def _run(tmp_path: Path, name: str, hosts: dict, play: dict, *flags) -> None:
    work = tmp_path / name
    work.mkdir()
    # Every rehearsal host is in the real group, so only identity can refuse it.
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": hosts}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "rehearsal.yml").write_text(yaml.safe_dump([play], sort_keys=False))
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
    }
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-f",
            "10",
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


def _outcome(root: Path, rehearsal_pass: str = "first") -> dict:
    return json.loads((root / f"outcome-{rehearsal_pass}.json").read_text())


def _write(path: Path, body: str = "[]") -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o600)
    return path


def _tree(root: Path) -> tuple[Path, Path]:
    custody = _write(root / CUSTODY.lstrip("/"))
    hosted = _write(root / HOSTED.lstrip("/"))
    _write(custody.parent / "custody-key.pem", "key")
    _write(hosted.parent / "evidence-receipt.json", "{}")
    return custody, hosted


def _paths(root: Path, items: list[str]) -> str:
    return json.dumps([f"{root}{item}" for item in items])


def _report(root: Path, verb: str, removed: list[str], absent: list[str]) -> str:
    return (
        f"Native Coding PostgreSQL environment cleanup; source_revision={REVISION}; "
        f"{verb}={_paths(root, removed)}; already_absent={_paths(root, absent)}; "
        "directories_kept=true; services_stopped=false; password_read=false."
    )


def _expected_refusal(task: str) -> str:
    message = _flat(_task(task)["ansible.builtin.assert"]["fail_msg"])
    return message.replace(VALIDATED_REVISION, REVISION).replace(
        f"{{{{ {REVISION_VAR} }}}}", REVISION
    )


def _assert_refused(root: Path, task: str, rehearsal_pass: str = "first") -> None:
    outcome = _outcome(root, rehearsal_pass)
    assert outcome.get("task") == task, (root.name, outcome)
    assert _expected_refusal(task) in [_flat(item) for item in outcome["messages"]], (
        root.name,
        outcome,
    )


def _hosts(roots: dict[str, Path], owners: dict[str, str]) -> dict[str, dict]:
    for root in roots.values():
        root.mkdir(parents=True)
    return {
        name: {
            "rehearsal_root": str(root),
            "rehearsal_units": STOPPED_UNITS,
            "rehearsal_owners": owners,
        }
        for name, root in roots.items()
    }


# The rehearsal needs uvx, network access and coreutils, so root pytest shards
# skip it and run only the structural tests above. The infra-ci Ansible job sets
# the gate; with it set nothing else can skip, so a missing tool fails.
rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)

LIVE_UNITS = {
    "live_custody": "ditto-coding-custody@0.service loaded active running Custody",
    "live_worker": (
        "ditto-coding-hosted-worker.service loaded deactivating stop-sigterm Worker"
    ),
    "activating": "ditto-coding-hosted-worker.service loaded activating start Worker",
    "reloading": "ditto-coding-custody@0.service loaded reloading reload Custody",
    "refreshing": (
        "ditto-coding-custody@2.service loaded refreshing refresh-extensions Custody"
    ),
    "unknown_state": "ditto-coding-hosted-worker.service loaded quiescent idle Worker",
    "unparseable": "● ditto-coding-custody@0.service loaded inactive dead Custody",
}


@rehearsal
def test_rehearsal_removes_only_the_exact_copies_and_refuses_unsafe_state(
    tmp_path,
) -> None:
    # Permission bits drive the partial-removal case; root would bypass them.
    assert os.geteuid() != 0, "run the rehearsal unprivileged"
    owners = dict.fromkeys(OWNERS, pwd.getpwuid(os.getuid()).pw_name)
    names = [
        "absent",
        "present",
        "no_units",
        *LIVE_UNITS,
        "symlink",
        "hardlink",
        "directory",
        "other_owner",
        "linked_parent",
        "linked_home",
        "preset_result",
        "undocumented_input",
        "partial",
    ]
    roots = {name: tmp_path / "hosts" / name for name in names}
    hosts = _hosts(roots, owners)
    hosts["no_units"]["rehearsal_units"] = ""
    for name, line in LIVE_UNITS.items():
        hosts[name]["rehearsal_units"] = STOPPED_UNITS + line + "\n"
    hosts["other_owner"]["rehearsal_owners"] = {
        **owners,
        "ditto-coding-hosted": "rehearsal-other-reader",
    }
    # Inventory values are refused exactly like extra vars.
    hosts["preset_result"][f"{PREFIX}copies"] = {"results": []}
    hosts["undocumented_input"][f"{PREFIX}paths"] = [str(tmp_path / "elsewhere")]

    present = _tree(roots["present"])
    no_units = _tree(roots["no_units"])
    kept = {name: _tree(roots[name]) for name in (*LIVE_UNITS, "other_owner")}
    kept |= {
        name: _tree(roots[name]) for name in ("preset_result", "undocumented_input")
    }
    outside = _write(roots["symlink"] / "outside/unrelated.json", "outside")
    symlink_custody = roots["symlink"] / CUSTODY.lstrip("/")
    symlink_custody.parent.mkdir(mode=0o700, parents=True)
    symlink_custody.symlink_to(outside)
    symlink_hosted = _write(roots["symlink"] / HOSTED.lstrip("/"))
    _, linked = _tree(roots["hardlink"])
    second_link = linked.parent / "evidence-link.json"
    os.link(linked, second_link)
    directory = roots["directory"] / HOSTED.lstrip("/")
    _write(directory / "attempt-receipt.json", "{}")
    parent_custody = _write(roots["linked_parent"] / CUSTODY.lstrip("/"))
    elsewhere = _write(
        roots["linked_parent"] / "elsewhere/postgres-environment.json", "elsewhere"
    )
    hosted_home = roots["linked_parent"] / "var/lib/ditto-coding-hosted"
    hosted_home.mkdir(mode=0o700, parents=True)
    (hosted_home / "private").symlink_to(elsewhere.parent)
    home_hosted = _write(roots["linked_home"] / HOSTED.lstrip("/"))
    real_home = roots["linked_home"] / "real-custody-home"
    home_copy = _write(real_home / "private/postgres-environment.json")
    (roots["linked_home"] / "var/lib/ditto-coding-custody").symlink_to(real_home)
    partial = _tree(roots["partial"])
    # The custody unlink succeeds; the hosted unlink then fails with EACCES.
    partial[1].parent.chmod(0o500)

    try:
        _run(tmp_path, "run", hosts, _play(_rehearsal_tasks(), "first"))
    finally:
        partial[1].parent.chmod(0o700)

    assert _outcome(roots["absent"]) == {
        "report": _report(roots["absent"], "removed", [], COPIES)
    }
    assert not (roots["absent"] / "var").exists()
    for name, pair in (("present", present), ("no_units", no_units)):
        assert _outcome(roots[name]) == {
            "report": _report(roots[name], "removed", COPIES, [])
        }
        for path in pair:
            assert not path.exists() and not path.is_symlink()
            assert path.parent.is_dir() and path.parent.stat().st_mode & 0o777 == 0o700
    assert (present[0].parent / "custody-key.pem").read_text() == "key"
    assert (present[1].parent / "evidence-receipt.json").read_text() == "{}"

    refusals = {
        **dict.fromkeys(LIVE_UNITS, LIVE),
        "symlink": COPY_CHECK,
        "hardlink": COPY_CHECK,
        "directory": COPY_CHECK,
        "other_owner": COPY_CHECK,
        "linked_parent": PARENT_CHECK,
        "linked_home": PARENT_CHECK,
        "preset_result": PRESET,
        "undocumented_input": PRESET,
    }
    for name, task in refusals.items():
        _assert_refused(roots[name], task)
    for pair in kept.values():
        assert all(path.read_text() == "[]" for path in pair)
    assert symlink_custody.is_symlink() and outside.read_text() == "outside"
    assert symlink_hosted.read_text() == "[]"
    assert linked.read_text() == "[]" and second_link.read_text() == "[]"
    assert linked.stat().st_nlink == 2
    assert (directory / "attempt-receipt.json").read_text() == "{}"
    assert parent_custody.read_text() == "[]"
    assert elsewhere.read_text() == "elsewhere"
    assert home_hosted.read_text() == "[]" and home_copy.read_text() == "[]"

    outcome = _outcome(roots["partial"])
    assert outcome["task"] == PARTIAL, outcome
    assert _flat(outcome["messages"][0]) == (
        "Native Coding PostgreSQL environment cleanup failed during removal; "
        f"source_revision={REVISION}; "
        f"removed={_paths(roots['partial'], [CUSTODY])}; "
        f"not_removed={_paths(roots['partial'], [HOSTED])}; "
        "reinspect both paths and reconcile by hand before re-running."
    )
    assert not partial[0].exists() and partial[1].read_text() == "[]"

    # Registered results persist for a whole playbook run, so the idempotent
    # re-run is a separate invocation, exactly as an operator would re-run it.
    _run(
        tmp_path,
        "second",
        {"present": hosts["present"]},
        _play(_rehearsal_tasks(), "second"),
    )
    assert _outcome(roots["present"], "second") == {
        "report": _report(roots["present"], "removed", [], COPIES)
    }


@rehearsal
def test_rehearsal_refuses_extra_vars_that_preset_a_result_or_forge_identity(
    tmp_path,
) -> None:
    owners = dict.fromkeys(OWNERS, pwd.getpwuid(os.getuid()).pw_name)
    roots = {
        name: tmp_path / "hosts" / name for name in ("forged_units", "forged_host")
    }
    hosts = _hosts(roots, owners)
    hosts["forged_units"]["rehearsal_units"] = LIVE_UNITS["live_custody"] + "\n"
    copies = {name: _tree(root) for name, root in roots.items()}

    # The review's exact override, then one complete enough to satisfy the unit
    # allow-list if the preset guard were missing. Both stop at the guard.
    for index, forged in enumerate(
        ({"stdout": ""}, {"stdout": "", "stdout_lines": []})
    ):
        _run(
            tmp_path,
            f"units-{index}",
            {"forged_units": hosts["forged_units"]},
            _play(_rehearsal_tasks(), f"forged-{index}"),
            "-e",
            json.dumps({f"{PREFIX}units": forged}),
        )
        _assert_refused(roots["forged_units"], PRESET, f"forged-{index}")

    # Forged facts that match the dedicated host must not pass the probed check.
    assert socket.gethostname().split(".")[0] != "ditto-coding-hosted-v2"
    forged_facts = {
        "hostname": "ditto-coding-hosted-v2",
        "architecture": "x86_64",
        "distribution": "Debian",
        "distribution_major_version": "13",
    }
    _run(
        tmp_path,
        "identity",
        {"forged_host": hosts["forged_host"]},
        _play(_rehearsal_tasks(identity=True), "first"),
        "-e",
        json.dumps({"ansible_facts": forged_facts}),
    )
    _assert_refused(roots["forged_host"], HOST)
    for pair in copies.values():
        assert all(path.read_text() == "[]" for path in pair)


@rehearsal
def test_rehearsal_check_mode_reports_without_removing(tmp_path) -> None:
    owners = dict.fromkeys(OWNERS, pwd.getpwuid(os.getuid()).pw_name)
    root = tmp_path / "hosts/dry_run"
    hosts = _hosts({"dry_run": root}, owners)
    copies = _tree(root)
    _run(tmp_path, "check", hosts, _play(_rehearsal_tasks(), "first"), "--check")
    assert _outcome(root) == {"report": _report(root, "would_remove", COPIES, [])}
    assert all(path.read_text() == "[]" for path in copies)
