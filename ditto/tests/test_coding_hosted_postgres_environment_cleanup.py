"""Native PostgreSQL environment removal stays default-off, surgical and silent."""

import copy
import json
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment_cleanup"
TASKS = (ROLE / "tasks/main.yml").read_text()
# Parsed tasks without YAML comments, for forbidden-token scans.
PARSED = yaml.safe_dump(yaml.safe_load(TASKS), width=10_000)
MATERIALIZE = yaml.safe_load(
    (
        ROOT / "infra/ansible/roles/coding_hosted_postgres_environment/tasks/main.yml"
    ).read_text()
)[1]["block"]

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

HOST = "Require the exact host, source and removal confirmation"
LISTING = "List live worker and custody units"
LIVE = "Refuse to remove credentials under a live worker or custody instance"
PARENT_STAT = "Inspect the reader directories without following links"
PARENT_CHECK = "Refuse a linked or non-directory parent"
COPY_STAT = "Inspect both exact copies as link metadata only"
COPY_CHECK = (
    "Refuse anything but an absent copy or a regular single-link copy owned by its "
    "reader"
)
UNLINK = "Unlink each exact copy that exists and nothing else"
AFTER_STAT = "Reinspect both exact paths without following links"
AFTER_CHECK = "Require both exact copies to be absent"
REPORT = "Report only the exact paths removed or already absent"


def _block() -> list[dict]:
    return yaml.safe_load(TASKS)[1]["block"]


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    return next(task for task in (tasks or _block()) if task["name"] == name)


def _module(task: dict) -> str:
    (module,) = set(task) - {
        "name",
        "loop",
        "loop_control",
        "register",
        "when",
        "changed_when",
        "check_mode",
    }
    return module


def test_default_off_with_exact_confirmation_source_and_host() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == {
        "coding_hosted_postgres_environment_cleanup_enabled": False,
        "coding_hosted_postgres_environment_cleanup_confirmation": "",
        "coding_hosted_postgres_environment_cleanup_source_revision": "",
    }
    explain, outer = yaml.safe_load(TASKS)
    assert explain["when"] == (
        "not (coding_hosted_postgres_environment_cleanup_enabled | bool)"
    )
    assert "no credential file is removed" in explain["ansible.builtin.debug"]["msg"]
    assert outer["when"] == "coding_hosted_postgres_environment_cleanup_enabled | bool"
    assert "vars" not in outer
    first = _block()[0]
    assert first["name"] == HOST
    that = first["ansible.builtin.assert"]["that"]
    materialize_host = MATERIALIZE[0]["ansible.builtin.assert"]["that"][:5]
    assert that == [
        *materialize_host,
        "coding_hosted_postgres_environment_cleanup_source_revision"
        " is match('^[0-9a-f]{40}$')",
        "coding_hosted_postgres_environment_cleanup_confirmation"
        " == 'REMOVE NATIVE CODING POSTGRES ENVIRONMENT'",
    ]
    assert "ansible_facts['hostname'] == 'ditto-coding-hosted-v2'" in that


def test_live_unit_refusal_reuses_the_materialization_semantics_exactly() -> None:
    names = [task["name"] for task in _block()]
    assert names[1:3] == [LISTING, LIVE]
    listing = _task(LISTING)
    original = _task("List live worker and custody units", MATERIALIZE)
    assert listing["ansible.builtin.command"] == original["ansible.builtin.command"]
    assert (
        "ditto-coding-hosted-worker.service"
        in listing["ansible.builtin.command"]["argv"]
    )
    assert (
        "ditto-coding-custody@*.service" in listing["ansible.builtin.command"]["argv"]
    )
    assert listing["check_mode"] is False and listing["changed_when"] is False
    refuse = _task(LIVE)["ansible.builtin.assert"]["that"]
    materialize_refuse = _task(
        "Refuse to replace credentials under a live worker or custody instance",
        MATERIALIZE,
    )["ansible.builtin.assert"]["that"]
    assert refuse == [
        line.replace(
            "coding_hosted_postgres_environment_units",
            "coding_hosted_postgres_environment_cleanup_units",
        )
        for line in materialize_refuse
    ]
    assert "active|activating|deactivating|reloading" in refuse[0]
    # The listing is the only systemctl use; nothing is stopped, killed or disabled.
    assert PARSED.count("systemctl") == 1
    for forbidden in ("systemd", "service:", "state: stopped", "kill", "is-active"):
        assert forbidden not in PARSED, forbidden


def test_only_the_two_exact_copies_are_ever_removed() -> None:
    modules = {_module(task) for task in _block()} | {
        _module(task) for task in yaml.safe_load(TASKS)[:1]
    }
    # No file, shell, copy, find or other module that could create, recurse or read.
    assert modules == {
        "ansible.builtin.assert",
        "ansible.builtin.command",
        "ansible.builtin.debug",
        "ansible.builtin.stat",
    }
    commands = [task["name"] for task in _block() if "ansible.builtin.command" in task]
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
    names = [task["name"] for task in _block()]
    assert names == [
        HOST,
        LISTING,
        LIVE,
        PARENT_STAT,
        PARENT_CHECK,
        COPY_STAT,
        COPY_CHECK,
        UNLINK,
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
    assert " ".join(sealed.split()) == (
        "not item.stat.exists or (item.stat.isreg and not item.stat.islnk and "
        "item.stat.nlink == 1 and item.stat.pw_name | default('') == item.item.owner)"
    )
    # Removal is limited to the copies whose lstat result proved them present.
    assert " ".join(_task(UNLINK)["when"].split()) == (
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
        "lookup(",
        "DITTO_CODING_PG_PASSWORD",
        "coding_hosted_postgres_environment_password",
        "gcloud",
        "set -x",
    ):
        assert forbidden not in PARSED, forbidden
    report = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert "item.path" in report and "stat.exists" in report
    assert "services_stopped=false" in report and "password_read=false" in report


def test_playbook_fixture_ci_and_docs_registration() -> None:
    playbooks = ROOT / "infra/ansible/playbooks"
    (play,) = yaml.safe_load(
        (playbooks / "gcp-coding-hosted-postgres-environment-cleanup.yml").read_text()
    )
    assert play["hosts"] == "role_coding_hosted"
    assert play["become"] is True and play["gather_facts"] is True
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
    section = docs.split("## Removal and rotation", 1)[1]
    assert "REMOVE NATIVE CODING POSTGRES ENVIRONMENT" in section
    assert "platform-db-password" in section
    assert "does not rotate" in section


# Local rehearsal: the role's own enabled tasks run through ansible-core 2.21.2
# against a temporary tree. Only this test rewrites paths, owners and the unit
# listing; the role has no such inputs.

STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
)


def _rehearsal_tasks() -> list[dict]:
    block = copy.deepcopy(_block())
    assert block.pop(0)["name"] == HOST
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
    return tasks


def _play(hosts: str, tasks: list[dict], rehearsal_pass: str) -> dict:
    outcome = "{{ rehearsal_root }}/outcome-{{ rehearsal_pass }}.json"
    return {
        "name": f"Rehearse cleanup ({rehearsal_pass})",
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


def _run(tmp_path: Path, name: str, hosts: dict, plays: list[dict], *flags) -> None:
    work = tmp_path / name
    work.mkdir()
    (work / "inventory.yml").write_text(yaml.safe_dump({"all": {"hosts": hosts}}))
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
        timeout=600,
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


def _report(root: Path, verb: str, removed: list[str], absent: list[str]) -> str:
    def paths(items: list[str]) -> str:
        return json.dumps([f"{root}{item}" for item in items])

    return (
        f"Native Coding PostgreSQL environment cleanup; {verb}={paths(removed)}; "
        f"already_absent={paths(absent)}; directories_kept=true; "
        "services_stopped=false; password_read=false."
    )


rehearsal = pytest.mark.skipif(
    shutil.which("uvx") is None
    or not Path("/usr/bin/unlink").is_file()
    or not Path("/usr/bin/printf").is_file(),
    reason="local rehearsal needs uvx and coreutils unlink/printf",
)


@rehearsal
def test_rehearsal_removes_only_the_exact_copies_and_refuses_unsafe_state(
    tmp_path,
) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    owners = dict.fromkeys(OWNERS, user)
    roots = {
        name: tmp_path / "hosts" / name
        for name in (
            "absent",
            "present",
            "live_custody",
            "live_worker",
            "symlink",
            "hardlink",
            "directory",
            "other_owner",
            "linked_parent",
        )
    }
    for root in roots.values():
        root.mkdir(parents=True)
    hosts = {
        name: {
            "rehearsal_root": str(root),
            "rehearsal_units": STOPPED_UNITS,
            "rehearsal_owners": owners,
        }
        for name, root in roots.items()
    }
    hosts["live_custody"]["rehearsal_units"] = (
        "ditto-coding-custody@0.service loaded active running Custody\n"
    )
    hosts["live_worker"]["rehearsal_units"] = (
        "ditto-coding-hosted-worker.service loaded deactivating stop-sigterm Worker\n"
    )
    hosts["other_owner"]["rehearsal_owners"] = {
        **owners,
        "ditto-coding-hosted": "rehearsal-other-reader",
    }

    present = _tree(roots["present"])
    live = [_tree(roots["live_custody"]), _tree(roots["live_worker"])]
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
    other_owner = _tree(roots["other_owner"])
    parent_custody = _write(roots["linked_parent"] / CUSTODY.lstrip("/"))
    elsewhere = _write(
        roots["linked_parent"] / "elsewhere/postgres-environment.json", "elsewhere"
    )
    hosted_home = roots["linked_parent"] / "var/lib/ditto-coding-hosted"
    hosted_home.mkdir(mode=0o700, parents=True)
    (hosted_home / "private").symlink_to(elsewhere.parent)

    tasks = _rehearsal_tasks()
    _run(
        tmp_path,
        "run",
        hosts,
        [_play("all", tasks, "first"), _play("present", tasks, "second")],
    )

    assert _outcome(roots["absent"]) == {
        "report": _report(roots["absent"], "removed", [], COPIES)
    }
    assert not (roots["absent"] / "var").exists()

    assert _outcome(roots["present"]) == {
        "report": _report(roots["present"], "removed", COPIES, [])
    }
    assert _outcome(roots["present"], "second") == {
        "report": _report(roots["present"], "removed", [], COPIES)
    }
    for path in present:
        assert not path.exists() and not path.is_symlink()
        assert path.parent.is_dir() and path.parent.stat().st_mode & 0o777 == 0o700
    assert (present[0].parent / "custody-key.pem").read_text() == "key"
    assert (present[1].parent / "evidence-receipt.json").read_text() == "{}"

    refusals = {
        "live_custody": LIVE,
        "live_worker": LIVE,
        "symlink": COPY_CHECK,
        "hardlink": COPY_CHECK,
        "directory": COPY_CHECK,
        "other_owner": COPY_CHECK,
        "linked_parent": PARENT_CHECK,
    }
    for name, task in refusals.items():
        outcome = _outcome(roots[name])
        assert outcome.get("task") == task, (name, outcome)
        fail_msg = " ".join(
            str(_task(task)["ansible.builtin.assert"]["fail_msg"]).split()
        )
        assert fail_msg in outcome["messages"], (name, outcome)

    for pair in live:
        assert all(path.read_text() == "[]" for path in pair)
    assert symlink_custody.is_symlink() and outside.read_text() == "outside"
    assert symlink_hosted.read_text() == "[]"
    assert linked.read_text() == "[]" and second_link.read_text() == "[]"
    assert linked.stat().st_nlink == 2
    assert (directory / "attempt-receipt.json").read_text() == "{}"
    assert all(path.read_text() == "[]" for path in other_owner)
    assert parent_custody.read_text() == "[]"
    assert elsewhere.read_text() == "elsewhere"


@rehearsal
def test_rehearsal_check_mode_reports_without_removing(tmp_path) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    root = tmp_path / "hosts/dry_run"
    root.mkdir(parents=True)
    copies = _tree(root)
    hosts = {
        "dry_run": {
            "rehearsal_root": str(root),
            "rehearsal_units": STOPPED_UNITS,
            "rehearsal_owners": dict.fromkeys(OWNERS, user),
        }
    }
    _run(
        tmp_path,
        "check",
        hosts,
        [_play("all", _rehearsal_tasks(), "first")],
        "--check",
    )
    assert _outcome(root) == {"report": _report(root, "would_remove", COPIES, [])}
    assert all(path.read_text() == "[]" for path in copies)
