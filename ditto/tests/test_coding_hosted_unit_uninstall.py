"""Native worker unit and custody template uninstall: default-off and surgical.

Structural tests parse the role and prove that its only removal targets are the
exact unit files coding_hosted_connectivity and coding_hosted_custody_service
install. Module tests drive the pinned-descriptor unlink module directly. The
rehearsal, gated by DITTO_ANSIBLE_REHEARSAL=1, runs the real role (main.yml
verbatim, remove.yml with only paths, owner, identity literals and systemctl
rewritten) through ansible-core 2.21.2 under the repo's ansible.cfg against
temporary trees, and mutates each guard to prove it is load-bearing.
"""

import copy
import functools
import importlib.util
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ANSIBLE = ROOT / "infra/ansible"
ROLE = ANSIBLE / "roles/coding_hosted_unit_uninstall"
MAIN = (ROLE / "tasks/main.yml").read_text()
REMOVE = (ROLE / "tasks/remove.yml").read_text()
MODULE_NAME = "coding_hosted_unit_uninstall_unlink"
MODULE_PATH = ROLE / f"library/{MODULE_NAME}.py"
PLAYBOOK = ANSIBLE / "playbooks/gcp-coding-hosted-unit-uninstall.yml"
FIXTURE = ANSIBLE / "tests/coding-hosted-unit-uninstall.yml"
DOC = ROOT / "infra/docs/coding-hosted-unit-uninstall-v2.md"
REPO_ANSIBLE_CFG = ANSIBLE / "ansible.cfg"

# The install roles and the exact unit files they template.
INSTALLERS = {
    "coding_hosted_connectivity": (
        "/etc/systemd/system/ditto-coding-hosted-worker.service"
    ),
    "coding_hosted_custody_service": (
        "/etc/systemd/system/ditto-coding-custody@.service"
    ),
}
UNIT_DIR = "/etc/systemd/system"
WORKER = INSTALLERS["coding_hosted_connectivity"]
CUSTODY = INSTALLERS["coding_hosted_custody_service"]
UNIT_PATHS = [WORKER, CUSTODY]

PREFIX = "coding_hosted_unit_uninstall_"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
}
GATE_NAME = f"{PREFIX}gate"
CONFIRMATION = "UNINSTALL NATIVE CODING WORKER AND CUSTODY UNITS"
REVISION = "0123456789abcdef0123456789abcdef01234567"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"
REVIEWED_HOST = "ditto-coding-hosted-v2"

# main.yml
PRESET = "Refuse a preset gate, capture or registered result"
GATE_FREEZE = "Freeze the enabled gate once"
DORMANT = "Explain the dormant native unit uninstall"
INCLUDE = "Uninstall the worker unit and custody template behind the enabled gate"
# remove.yml
PRESET_INCLUDE = "Refuse a preset internal name or gate, or a non-boolean enabled flag"
BATCH = "Require the run to target exactly the one dedicated host"
CHECK_MODE = "Refuse check mode for an enabled uninstall"
FREEZE_INPUTS = "Freeze the confirmation and source revision once"
GATE = "Require the exact confirmation and source revision as frozen literals"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the dedicated host"
LISTING = "List live worker and custody units"
LIVE = "Refuse to uninstall unless every listed unit is inactive or failed"
UNLINK = "Remove the two unit files through the symlink-safe module"
RELOAD = "Reload unit definitions without starting or stopping anything"
UNLINK_CHECK = "Require the module to have removed or confirmed absent both unit files"
RELIST = "Re-list live worker and custody units after removal"
LIVE_AFTER = "Refuse if any worker or custody unit went live during removal"
UNIT_FILES = "List unit files systemd still knows for either unit after the reload"
UNIT_FILES_CHECK = "Require systemd to know neither unit file after the reload"
REPORT = "Report only the source revision and which fixed unit paths were removed or already absent"  # noqa: E501

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


def _docs(text: str) -> list[dict]:
    return yaml.safe_load(text)


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    (task,) = [t for t in _walk(tasks or _docs(REMOVE)) if t.get("name") == name]
    return task


def _flat(text: object) -> str:
    return " ".join(str(text).split())


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def _names(tasks: list[dict]) -> set[str]:
    """Every variable a task file creates by register, set_fact or loop_var."""
    names = set()
    for task in _walk(tasks):
        if "register" in task:
            names.add(task["register"])
        names.update(task.get("ansible.builtin.set_fact", {}))
        if "loop_control" in task:
            names.add(task["loop_control"].get("loop_var", "item"))
    return names


@pytest.fixture(autouse=True)
def _umask() -> Iterator[None]:
    # Trees must not be group-writable, which a 0002 login umask would make them.
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


# ─── Structure ────────────────────────────────────────────────────────────────


def test_defaults_are_exactly_the_three_inputs() -> None:
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == INPUTS


def test_main_refuses_presets_then_freezes_the_gate_and_includes() -> None:
    tasks = _docs(MAIN)
    assert [t["name"] for t in tasks] == [PRESET, GATE_FREEZE, DORMANT, INCLUDE]
    (that,) = tasks[0]["ansible.builtin.assert"]["that"]
    assert _flat(that) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PREFIX}', wantlist=True) | sort == ["
        + ", ".join(f"'{n}'" for n in sorted(INPUTS))
        + "]"
    )
    assert tasks[0]["ansible.builtin.assert"]["quiet"] is True
    freeze = tasks[1]
    assert freeze["no_log"] is True
    assert _flat(freeze["ansible.builtin.set_fact"][GATE_NAME]) == (
        f"{{{{ ({PREFIX}enabled | default(false, true)) is sameas true }}}}"
    )
    assert tasks[2]["when"] == f"not ({GATE_NAME} is sameas true)"
    assert tasks[3]["ansible.builtin.include_tasks"] == "remove.yml"
    assert tasks[3]["when"] == f"{GATE_NAME} is sameas true"
    assert "import_tasks" not in MAIN


def test_no_bool_filter_anywhere_in_the_role() -> None:
    for text in (MAIN, REMOVE):
        parsed = yaml.safe_dump(yaml.safe_load(text), width=10_000)
        assert not re.search(r"\|\s*bool\b", parsed)


def test_remove_task_order() -> None:
    assert [t["name"] for t in _docs(REMOVE)] == [
        PRESET_INCLUDE,
        BATCH,
        CHECK_MODE,
        FREEZE_INPUTS,
        GATE,
        IDENTITY,
        HOST,
        LISTING,
        LIVE,
        UNLINK,
        RELOAD,
        UNLINK_CHECK,
        RELIST,
        LIVE_AFTER,
        UNIT_FILES,
        UNIT_FILES_CHECK,
        REPORT,
    ]


def test_in_include_guard_refuses_every_internal_name_and_the_raw_flag() -> None:
    that = _task(PRESET_INCLUDE)["ansible.builtin.assert"]["that"]
    assert that[0] == f"({PREFIX}enabled | default(false, true)) is sameas true"
    allowed = sorted([*INPUTS, GATE_NAME])
    assert _flat(that[1]) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PREFIX}', wantlist=True) | sort == ["
        + ", ".join(f"'{n}'" for n in allowed)
        + "]"
    )
    # Every name the role creates carries the prefix, so both refusals cover it.
    created = _names(_docs(MAIN)) | _names(_docs(REMOVE))
    assert created and all(name.startswith(PREFIX) for name in created), created
    assert _names(_docs(MAIN)) == {GATE_NAME}
    # The rehearsal presets every one of them.
    assert created <= set(INTERNAL_PRESETS)


def test_targeting_is_the_whole_play_host_set_not_an_overridable_var() -> None:
    assert _task(BATCH)["ansible.builtin.assert"]["that"] == [
        f"ansible_play_batch == ['{REVIEWED_HOST}']",
        f"ansible_play_hosts_all == ['{REVIEWED_HOST}']",
    ]
    for text in (MAIN, REMOVE):
        assert "inventory_hostname" not in yaml.safe_dump(yaml.safe_load(text))
        assert "groups" not in yaml.safe_dump(yaml.safe_load(text))
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["gather_facts"] is False and play["become"] is True
    assert play["roles"] == ["coding_hosted_unit_uninstall"]
    assert set(play) == {"name", "hosts", "become", "gather_facts", "roles"}


def test_check_mode_is_refused_before_any_host_probe() -> None:
    names = [t["name"] for t in _docs(REMOVE)]
    assert _task(CHECK_MODE)["ansible.builtin.assert"]["that"] == [
        "not ansible_check_mode"
    ]
    assert names.index(CHECK_MODE) < names.index(IDENTITY)


def test_inputs_are_frozen_once_under_no_log_and_checked_as_literals() -> None:
    freeze = _task(FREEZE_INPUTS)
    assert freeze["no_log"] is True
    facts = freeze["ansible.builtin.set_fact"]
    assert _flat(facts[f"{PREFIX}gate_confirmation"]) == (
        f"{{{{ {PREFIX}confirmation | default('', true) }}}}"
    )
    assert _flat(facts[f"{PREFIX}gate_source_revision"]) == (
        f"{{{{ {PREFIX}source_revision | default('', true) }}}}"
    )
    that = _task(GATE)["ansible.builtin.assert"]["that"]
    assert f"{PREFIX}gate_confirmation == '{CONFIRMATION}'" in that
    assert f"{PREFIX}gate_source_revision is match('^[0-9a-f]{{40}}$')" in that
    # No task after the freeze reads the raw inputs again.
    after = _docs(REMOVE)[[t["name"] for t in _docs(REMOVE)].index(GATE) :]
    for task in after:
        dumped = json.dumps(task)
        for raw in ("confirmation", "source_revision"):
            assert f"{PREFIX}{raw}" not in dumped, task["name"]


def test_identity_comes_from_a_registered_probe() -> None:
    probe = _task(IDENTITY)
    assert probe["register"] == f"{PREFIX}identity"
    assert "ansible.builtin.setup" in probe
    assert _task(HOST)["ansible.builtin.assert"]["that"] == [
        f"{PREFIX}identity.ansible_facts.ansible_hostname == '{REVIEWED_HOST}'",
        f"{PREFIX}identity.ansible_facts.ansible_architecture == 'x86_64'",
        f"{PREFIX}identity.ansible_facts.ansible_distribution == 'Debian'",
        f"{PREFIX}identity.ansible_facts.ansible_distribution_major_version == '13'",
    ]
    assert "ansible_facts[" not in REMOVE


def test_live_units_are_refused_before_and_rechecked_after_removal() -> None:
    for listing, check in ((LISTING, LIVE), (RELIST, LIVE_AFTER)):
        task = _task(listing)
        assert task["ansible.builtin.command"]["argv"] == LISTING_ARGV
        assert task["check_mode"] is False and task["changed_when"] is False
        (that,) = _task(check)["ansible.builtin.assert"]["that"]
        assert _flat(that) == _flat(
            f"{task['register']}.stdout_lines | reject('match', '{UNIT_PATTERN}') "
            "| list | length == 0"
        )
    names = [t["name"] for t in _docs(REMOVE)]
    assert names.index(LIVE) < names.index(UNLINK) < names.index(RELIST)
    files = _task(UNIT_FILES)
    assert files["ansible.builtin.command"]["argv"] == [
        "/usr/bin/systemctl",
        "list-unit-files",
        "--no-legend",
        "--plain",
        "--full",
        *[Path(p).name for p in UNIT_PATHS],
    ]
    assert _task(UNIT_FILES_CHECK)["ansible.builtin.assert"]["that"] == [
        f"{files['register']}.stdout | trim | length == 0"
    ]


def test_role_stops_and_starts_nothing_and_touches_only_unit_files() -> None:
    parsed = yaml.safe_dump(_docs(REMOVE), width=10_000) + yaml.safe_dump(
        _docs(MAIN), width=10_000
    )
    systemctl = [
        task["ansible.builtin.command"]["argv"][1]
        for task in _walk(_docs(REMOVE))
        if "ansible.builtin.command" in task
    ]
    assert systemctl == ["list-units", "daemon-reload", "list-units", "list-unit-files"]
    modules = {
        key
        for task in _walk(_docs(REMOVE))
        for key in task
        if "." in key or key == MODULE_NAME
    }
    assert modules == {
        "ansible.builtin.assert",
        "ansible.builtin.set_fact",
        "ansible.builtin.setup",
        "ansible.builtin.command",
        "ansible.builtin.debug",
        MODULE_NAME,
    }
    # Every operative string outside the constant messages and the report.
    values = [
        value
        for task in _walk(_docs(REMOVE))
        for key, body in task.items()
        if key != "name"
        for value in _strings(
            {k: v for k, v in body.items() if k not in ("fail_msg", "msg")}
            if isinstance(body, dict)
            else body
        )
    ]
    for forbidden in (
        "/var/lib",
        "/etc/ditto",
        "/opt/",
        "/usr/local",
        "/run/",
        "private",
        "postgres",
        "credential",
        "getent",
        "docker",
        "slurp",
        "shell",
        ".service.d",
    ):
        assert not any(forbidden in value.lower() for value in values), forbidden
    assert "unit_dir: /etc/systemd/system" in parsed
    unlink = _task(UNLINK)
    assert unlink[MODULE_NAME] == {"unit_dir": UNIT_DIR, "owner": "root"}
    assert unlink["failed_when"] is False


def test_constant_fail_messages_except_the_module_receipt() -> None:
    for task in _walk(_docs(MAIN) + _docs(REMOVE)):
        assertion = task.get("ansible.builtin.assert")
        if not assertion:
            continue
        message = assertion["fail_msg"]
        if task["name"] == UNLINK_CHECK:
            # Only the module's own fixed-path lists, each defaulted.
            refs = re.findall(r"\{\{(.*?)\}\}", message)
            assert refs and all(
                re.fullmatch(
                    rf" {PREFIX}removal\.\w+ \| default\(\[\]\) \| to_json ", ref
                )
                for ref in refs
            ), refs
        else:
            assert "{{" not in message, task["name"]
        assert assertion["quiet"] is True


def test_report_interpolates_only_the_frozen_revision_and_module_lists() -> None:
    message = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert re.findall(r"\{\{(.*?)\}\}", message) == [
        f" {PREFIX}gate_source_revision ",
        f" {PREFIX}removal.removed | to_json ",
        f" {PREFIX}removal.already_absent | to_json ",
    ]


def test_reload_follows_removal_even_after_a_partial_refusal() -> None:
    names = [t["name"] for t in _docs(REMOVE)]
    assert names.index(UNLINK) + 1 == names.index(RELOAD)
    assert names.index(RELOAD) + 1 == names.index(UNLINK_CHECK)
    reload = _task(RELOAD)
    assert reload["ansible.builtin.command"]["argv"] == [
        "/usr/bin/systemctl",
        "daemon-reload",
    ]
    assert "when" not in reload


# ─── Parity with the install roles ────────────────────────────────────────────


def _role_texts(role: Path) -> Iterator[tuple[Path, str]]:
    for path in sorted(role.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path, path.read_text(errors="replace")


def test_removal_targets_equal_the_install_role_unit_destinations() -> None:
    module = _unlink_module()
    assert [f"{UNIT_DIR}/{name}" for name in module.UNIT_NAMES] == UNIT_PATHS
    assert tuple(UNIT_DIR.strip("/").split("/")) == module.UNIT_DIR_SUFFIX
    for role, path in INSTALLERS.items():
        tasks = _docs((ANSIBLE / f"roles/{role}/tasks/main.yml").read_text())
        systemd = [
            task
            for task in _walk(tasks)
            if "block" not in task
            and any("/etc/systemd" in value for value in _strings(task))
        ]
        (install,) = systemd
        template = install["ansible.builtin.template"]
        assert template["dest"] == path
        assert template["owner"] == "root" and template["mode"] == "0644"
        # The uninstall owner is the install owner.
        assert _task(UNLINK)[MODULE_NAME]["owner"] == template["owner"]
        body = (ANSIBLE / f"roles/{role}/templates/{template['src']}").read_text()
        # No [Install] section: the install role never creates an enablement link.
        assert not re.search(r"^\s*\[Install\]", body, re.M), role
        # The install role never enables, drops in or links either unit.
        dumped = json.dumps(tasks)
        assert '"enabled"' not in dumped and "systemctl enable" not in dumped
        assert ".service.d" not in dumped and "state: link" not in dumped


def test_no_other_role_installs_a_path_for_either_unit() -> None:
    pattern = re.compile(
        r"/etc/systemd/[^\s\"'`]*(?:ditto-coding-hosted-worker|ditto-coding-custody@)"
        r"[^\s\"'`]*"
    )
    found: dict[str, set[str]] = {}
    for role in sorted((ANSIBLE / "roles").iterdir()):
        if role.name == ROLE.name:
            continue
        for _, text in _role_texts(role):
            for match in pattern.findall(text):
                found.setdefault(role.name, set()).add(match)
    assert found == {role: {path} for role, path in INSTALLERS.items()}


# ─── Module ──────────────────────────────────────────────────────────────────


def _unlink_module() -> Any:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _unit_tree(root: Path, *, worker: bool = True, custody: bool = True) -> Path:
    unit_dir = root / "etc/systemd/system"
    unit_dir.mkdir(parents=True)
    for directory in (root / "etc", root / "etc/systemd", unit_dir):
        directory.chmod(0o755)
    for present, path in ((worker, WORKER), (custody, CUSTODY)):
        if present:
            target = unit_dir / Path(path).name
            target.write_text("[Unit]\n")
            target.chmod(0o644)
    (unit_dir / "unrelated.service").write_text("[Unit]\n")
    return unit_dir


def _uid() -> int:
    return os.getuid()


def test_module_removes_only_the_two_unit_files(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    result = module.remove_units(str(unit_dir), _uid())
    assert result["removed"] == [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]
    assert result["already_absent"] == [] and result["refused"] == []
    assert result["changed"] is True
    assert sorted(p.name for p in unit_dir.iterdir()) == ["unrelated.service"]
    again = module.remove_units(str(unit_dir), _uid())
    assert again["removed"] == [] and again["changed"] is False
    assert again["already_absent"] == [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]


def test_module_reports_a_missing_unit_dir_as_already_absent(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = tmp_path.resolve() / "etc/systemd/system"
    result = module.remove_units(str(unit_dir), _uid())
    assert result["unit_dir_present"] is False
    assert result["already_absent"] == [
        f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS
    ]


def test_module_check_mode_removes_nothing(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    result = module.remove_units(str(unit_dir), _uid(), check_mode=True)
    assert result["would_remove"] == [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]
    assert result["removed"] == []
    assert (unit_dir / Path(WORKER).name).exists()


@pytest.mark.parametrize("swap", ["symlink", "directory", "hardlink", "fifo"])
def test_module_refuses_a_swapped_worker_unit_and_attempts_nothing_after(
    tmp_path, swap
) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    unit_dir = _unit_tree(root, worker=False)
    worker = unit_dir / Path(WORKER).name
    outside = root / "outside.service"
    outside.write_text("keep")
    if swap == "symlink":
        worker.symlink_to(outside)
    elif swap == "directory":
        worker.mkdir()
    elif swap == "hardlink":
        os.link(outside, worker)
    else:
        os.mkfifo(worker)
    result = module.remove_units(str(unit_dir), _uid())
    assert result["removed"] == []
    assert [r.split(": ")[0] for r in result["refused"]] == [str(worker)]
    assert result["not_attempted"] == [f"{unit_dir}/{Path(CUSTODY).name}"]
    assert outside.read_text() == "keep"
    assert (unit_dir / Path(CUSTODY).name).exists()
    assert os.path.lexists(worker)


def test_module_reports_a_partial_removal(tmp_path) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    unit_dir = _unit_tree(root, custody=False)
    other = root / "other"
    other.write_text("x")
    os.link(other, unit_dir / Path(CUSTODY).name)
    result = module.remove_units(str(unit_dir), _uid())
    assert result["removed"] == [f"{unit_dir}/{Path(WORKER).name}"]
    assert result["refused"] == [
        f"{unit_dir}/{Path(CUSTODY).name}: has more than one hard link"
    ]
    assert result["not_attempted"] == []
    assert result["changed"] is True


def test_module_refuses_a_tree_not_owned_by_the_unit_owner(tmp_path) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    # Without root the files cannot be chowned; an owner the tree does not have
    # is refused at the first foreign directory, before any file.
    with pytest.raises(module.Unsafe, match="owned by"):
        module.remove_units(str(unit_dir), _uid() + 1)
    assert (unit_dir / Path(WORKER).name).exists()


@pytest.mark.parametrize(
    "entry",
    [
        "ditto-coding-hosted-worker.service.d/override.conf",
        "ditto-coding-custody@.service.d/override.conf",
        "ditto-coding-custody@7c9e6679-7425-40de-944b-e07fc1f90ae7.service",
        "multi-user.target.wants/ditto-coding-hosted-worker.service",
        "default.target.requires/ditto-coding-custody@x.service",
    ],
)
def test_module_refuses_drop_ins_instances_and_links_before_removing(
    tmp_path, entry
) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    path = unit_dir / entry
    path.parent.mkdir(exist_ok=True)
    if ".wants" in entry or ".requires" in entry:
        path.symlink_to(unit_dir / Path(WORKER).name)
    else:
        path.write_text("[Service]\n")
    result = module.remove_units(str(unit_dir), _uid())
    assert result["foreign"] == [
        entry.split("/")[0] if ".service.d" in entry else entry
    ]
    assert result["removed"] == []
    assert result["not_attempted"] == [f"{unit_dir}/{Path(p).name}" for p in UNIT_PATHS]
    assert all((unit_dir / Path(p).name).exists() for p in UNIT_PATHS)


def test_module_refuses_a_symlinked_dependency_directory(tmp_path) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    unit_dir = _unit_tree(root)
    hidden = root / "hidden"
    hidden.mkdir()
    (hidden / "ditto-coding-hosted-worker.service").symlink_to(
        unit_dir / Path(WORKER).name
    )
    (unit_dir / "multi-user.target.wants").symlink_to(hidden)
    with pytest.raises(module.Unsafe, match="is a symlink"):
        module.remove_units(str(unit_dir), _uid())
    assert all((unit_dir / Path(p).name).exists() for p in UNIT_PATHS)


def test_module_refuses_a_symlinked_or_writable_unit_directory(tmp_path) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    real = _unit_tree(root / "real")
    linked = root / "linked/etc/systemd"
    linked.mkdir(parents=True)
    (linked / "system").symlink_to(real)
    with pytest.raises(module.Unsafe, match="symlink"):
        module.remove_units(str(linked / "system"), _uid())
    assert (real / Path(WORKER).name).exists()

    writable = _unit_tree(root / "writable")
    writable.chmod(0o775)
    with pytest.raises(module.Unsafe, match="writable by group or others"):
        module.remove_units(str(writable), _uid())
    writable.chmod(0o755)
    writable.parent.chmod(0o777)
    with pytest.raises(module.Unsafe, match="writable by group or others"):
        module.remove_units(str(writable), _uid())
    writable.parent.chmod(0o755)
    assert (writable / Path(WORKER).name).exists()


@pytest.mark.parametrize(
    "path",
    [
        "etc/systemd/system",
        "/etc/systemd/system/",
        "/etc/systemd//system",
        "/etc/systemd/../systemd/system",
        "/etc/systemd/user",
        "/etc/systemd/system.control",
        "/run/systemd/transient",
    ],
)
def test_module_refuses_any_unit_dir_but_an_etc_systemd_system(path) -> None:
    module = _unlink_module()
    with pytest.raises(module.Unsafe):
        module.split_unit_dir(path)


def test_module_pins_directories_and_never_follows_links() -> None:
    src = MODULE_PATH.read_text()
    assert "O_NOFOLLOW" in src and "O_DIRECTORY" in src
    assert 'os.open("/"' in src
    assert "os.unlink(name, dir_fd=dir_fd)" in src
    for forbidden in ("os.remove(", "shutil", "rmdir", "os.path.realpath", "open(path"):
        assert forbidden not in src, forbidden


def test_module_mutation_without_nofollow_follows_a_symlinked_unit_dir(
    tmp_path,
) -> None:
    # Mutation check: the O_NOFOLLOW walk is what refuses a swapped parent.
    module = _unlink_module()
    root = tmp_path.resolve()
    real = _unit_tree(root / "real")
    linked = root / "linked/etc/systemd"
    linked.mkdir(parents=True)
    (linked / "system").symlink_to(real)
    module._DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    result = module.remove_units(str(linked / "system"), _uid())
    assert result["removed"]
    assert not (real / Path(WORKER).name).exists()


def test_module_mutation_without_foreign_scan_leaves_a_drop_in_orphaned(
    tmp_path,
) -> None:
    module = _unlink_module()
    unit_dir = _unit_tree(tmp_path.resolve())
    (unit_dir / "ditto-coding-hosted-worker.service.d").mkdir()
    module.foreign_entries = lambda _fd: []
    result = module.remove_units(str(unit_dir), _uid())
    assert (
        result["removed"]
        and (unit_dir / "ditto-coding-hosted-worker.service.d").exists()
    )


def test_module_mutation_without_type_checks_unlinks_a_symlink(tmp_path) -> None:
    module = _unlink_module()
    root = tmp_path.resolve()
    unit_dir = _unit_tree(root, worker=False)
    (unit_dir / Path(WORKER).name).symlink_to(root / "elsewhere")
    fake = {k: getattr(stat, k) for k in dir(stat) if not k.startswith("__")}
    module.stat = SimpleNamespace(**{**fake, "S_ISREG": lambda _mode: True})
    result = module.remove_units(str(unit_dir), _uid())
    assert result["removed"][0].endswith(Path(WORKER).name)


# ─── Playbook, CI and docs ───────────────────────────────────────────────────


def test_fixture_ci_and_docs_registration() -> None:
    (fixture,) = yaml.safe_load(FIXTURE.read_text())
    assert fixture["hosts"] == "localhost" and fixture["become"] is False
    assert fixture["roles"] == ["coding_hosted_unit_uninstall"]
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra-ci.yml").read_text())
    this_file = str(Path(__file__).relative_to(ROOT))
    for trigger in ("pull_request", "push"):
        paths = workflow[True][trigger]["paths"]
        assert "infra/**" in paths
        assert this_file in paths and "pyproject.toml" in paths and "uv.lock" in paths
    steps = workflow["jobs"]["ansible"]["steps"]
    runs = "\n".join(step.get("run", "") for step in steps)
    assert "playbooks/gcp-coding-hosted-unit-uninstall.yml" in runs
    assert "tests/coding-hosted-unit-uninstall.yml" in runs
    (step,) = [
        step
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if REHEARSAL_GATE in step.get("env", {}) and this_file in step.get("run", "")
    ]
    assert step in steps
    assert step["env"] == {REHEARSAL_GATE: "1"}
    assert step["working-directory"] == "${{ github.workspace }}"
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
    uses = [s.get("uses", "") for s in steps]
    assert any(action.startswith("astral-sh/setup-uv@") for action in uses)


def test_docs_describe_every_guard_and_residual() -> None:
    doc = DOC.read_text()
    for phrase in (
        CONFIRMATION,
        *UNIT_PATHS,
        "coding_hosted_connectivity",
        "coding_hosted_custody_service",
        "--limit ditto-coding-hosted-v2",
        "ansible_play_batch == ['ditto-coding-hosted-v2']",
        "ansible_play_hosts_all == ['ditto-coding-hosted-v2']",
        "is sameas true",
        "include_tasks",
        "--start-at-task",
        "O_NOFOLLOW",
        "daemon-reload",
        "inactive or failed",
        "not_attempted",
        "already_absent",
        "check mode",
        f"`{REHEARSAL_GATE}=1`",
        "#1897",
        "#1925",
        "custody-run.py",
        "connectivity-policy.py",
        "Residual",
    ):
        assert phrase in doc, phrase


# ─── Rehearsal ────────────────────────────────────────────────────────────────

rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)

# A distinctive value no escaping changes, used as a wrong input; it must never
# appear in ansible output.
CANARY = "Kq7vUninstallCanaryZ3w9"
STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
    "ditto-coding-custody@1.service not-found inactive dead ditto-coding-custody@1\n"
)
LIVE_UNITS = {
    "live_custody": "ditto-coding-custody@0.service loaded active running Custody",
    "live_worker": "ditto-coding-hosted-worker.service loaded deactivating stop-sigterm W",  # noqa: E501
    "activating": "ditto-coding-hosted-worker.service loaded activating start Worker",
    "reloading": "ditto-coding-custody@0.service loaded reloading reload Custody",
    "refreshing": "ditto-coding-custody@2.service loaded refreshing refresh-extensions C",  # noqa: E501
    "maintenance": "ditto-coding-custody@3.service loaded maintenance cleaning Custody",
    "unknown_state": "ditto-coding-hosted-worker.service loaded quiescent idle Worker",
    "unparseable": "● ditto-coding-custody@0.service loaded inactive dead Custody",
}
INTERNAL_PRESETS = {
    GATE_NAME: True,
    f"{PREFIX}gate_confirmation": CONFIRMATION,
    f"{PREFIX}gate_source_revision": REVISION,
    f"{PREFIX}identity": {"ansible_facts": {"ansible_hostname": REVIEWED_HOST}},
    f"{PREFIX}units": {"stdout_lines": []},
    f"{PREFIX}removal": {"refused": [], "removed": [], "already_absent": []},
    f"{PREFIX}reload": {"rc": 0},
    f"{PREFIX}units_after": {"stdout_lines": []},
    f"{PREFIX}unit_files_after": {"stdout": ""},
    f"{PREFIX}undocumented": "x",
}


def _rewrite(node: Any, *, local_identity: bool) -> Any:
    """Point remove.yml at a temporary tree: unit dir, owner, systemctl, identity."""
    if isinstance(node, dict):
        out = {
            key: value
            if key == "ansible.builtin.assert"
            else _rewrite(value, local_identity=local_identity)
            for key, value in node.items()
        }
        command = out.get("ansible.builtin.command")
        if command and command["argv"][0] == "/usr/bin/systemctl":
            register = out.get("register")
            if register == f"{PREFIX}reload":
                argv = ["/usr/bin/touch", "{{ rehearsal_root }}/daemon-reloaded"]
            elif register == f"{PREFIX}units_after":
                argv = [
                    "/usr/bin/printf",
                    "%s",
                    "{{ rehearsal_units_after | default(rehearsal_units) }}",
                ]
            elif register == f"{PREFIX}unit_files_after":
                argv = [
                    "/usr/bin/printf",
                    "%s",
                    "{{ rehearsal_unit_files | default('') }}",
                ]
            else:
                argv = ["/usr/bin/printf", "%s", "{{ rehearsal_units }}"]
            out["ansible.builtin.command"] = {"argv": argv}
        if MODULE_NAME in out:
            out[MODULE_NAME] = {
                "unit_dir": "{{ rehearsal_root }}" + out[MODULE_NAME]["unit_dir"],
                "owner": "{{ rehearsal_owner }}",
            }
        if out.get("name") == REPORT:
            out["register"] = "rehearsal_report"
        if "ansible.builtin.assert" in out and local_identity:
            identity = _local_identity()
            that = [
                re.sub(
                    rf"^({PREFIX}identity\.ansible_facts\.(ansible_\w+)) == '[^']+'$",
                    lambda m: f"{m[1]} == {json.dumps(identity[m[2]])}",
                    line,
                )
                for line in out["ansible.builtin.assert"]["that"]
            ]
            out["ansible.builtin.assert"] = dict(
                out["ansible.builtin.assert"], that=that
            )
        return out
    if isinstance(node, list):
        return [_rewrite(item, local_identity=local_identity) for item in node]
    return node


@functools.cache
def _local_identity() -> dict[str, str]:
    """This machine's probed identity, as literals: --start-at-task skips any
    in-play probe, so the rewritten host check compares against constants."""
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible",
            "-i",
            "localhost,",
            "-c",
            "local",
            "-m",
            "ansible.builtin.setup",
            "-a",
            "gather_subset=!all,!min,platform,distribution",
            "localhost",
        ],
        env={k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
        | {"ANSIBLE_NOCOLOR": "1", "ANSIBLE_PYTHON_INTERPRETER": sys.executable},
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    facts = json.loads(completed.stdout.split("=> ", 1)[1])["ansible_facts"]
    return {
        key: facts[key]
        for key in (
            "ansible_hostname",
            "ansible_architecture",
            "ansible_distribution",
            "ansible_distribution_major_version",
        )
    }


def _build_role(
    dst: Path,
    *,
    local_identity: bool,
    real_batch: bool,
    mutate_main=None,
    mutate_remove=None,
) -> None:
    role = dst / "roles/coding_hosted_unit_uninstall"
    (role / "tasks").mkdir(parents=True)
    shutil.copytree(ROLE / "defaults", role / "defaults")
    shutil.copytree(
        ROLE / "library", role / "library", ignore=shutil.ignore_patterns("__pycache__")
    )
    tasks = _rewrite(copy.deepcopy(_docs(REMOVE)), local_identity=local_identity)
    if not real_batch:
        # Multi-host bulk runs cannot be the single dedicated host; the batch
        # guard is rehearsed with its real literal in the targeting test.
        _task(BATCH, tasks)["ansible.builtin.assert"]["that"] = [
            "ansible_play_batch == ansible_play_batch"
        ]
    rendered = json.dumps(tasks)
    assert "systemctl" not in rendered
    assert '"/etc/systemd' not in rendered
    assert _task(PRESET_INCLUDE, tasks) == _task(PRESET_INCLUDE)
    assert _task(GATE, tasks) == _task(GATE)
    assert _task(LIVE, tasks) == _task(LIVE)
    main = _docs(MAIN)
    if mutate_main is not None:
        mutate_main(main)
    if mutate_remove is not None:
        mutate_remove(tasks)
    (role / "tasks/main.yml").write_text(
        MAIN if mutate_main is None else yaml.safe_dump(main, sort_keys=False)
    )
    (role / "tasks/remove.yml").write_text(yaml.safe_dump(tasks, sort_keys=False))


def _record(outcome: str, content: str) -> dict:
    return {
        "ansible.builtin.copy": {"dest": outcome, "content": content},
        "check_mode": False,
        "no_log": True,
        "diff": False,
    }


def _play(rehearsal_pass: str, serial: int | None = None) -> dict:
    outcome = "{{ rehearsal_root }}/outcome-" + rehearsal_pass + ".json"
    play: dict[str, Any] = {
        "name": f"Rehearse unit uninstall ({rehearsal_pass})",
        "hosts": "all",
        "strategy": "free" if serial is None else "linear",
        "gather_facts": False,
        "become": False,
        "vars": {"ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "tasks": [
            {
                "name": "Rehearse the role",
                "block": [
                    # A static import, like the playbook's roles: list, so
                    # --start-at-task sees exactly the production task list.
                    {
                        "name": "Import the role",
                        "ansible.builtin.import_role": {
                            "name": "coding_hosted_unit_uninstall"
                        },
                    },
                    {
                        "name": "Record completion",
                        **_record(
                            outcome,
                            "{{ {'report': rehearsal_report.msg | default(none)}"
                            " | to_json }}",
                        ),
                    },
                ],
                "rescue": [
                    {
                        "name": "Record refusal",
                        **_record(
                            outcome,
                            "{{ {'task': ansible_failed_task.name, 'messages': "
                            "[ansible_failed_result.msg | default('')]} | to_json }}",
                        ),
                    }
                ],
            },
        ],
    }
    if serial is not None:
        play["serial"] = serial
    return play


def _run(
    tmp_path: Path,
    name: str,
    hosts: dict[str, dict],
    *flags: str,
    rehearsal_pass: str = "first",
    local_identity: bool = True,
    real_batch: bool = False,
    serial: int | None = None,
    mutate_main=None,
    mutate_remove=None,
    allow_deprecation: bool = False,
) -> str:
    work = tmp_path / "runs" / name
    work.mkdir(parents=True)
    _build_role(
        work,
        local_identity=local_identity,
        real_batch=real_batch,
        mutate_main=mutate_main,
        mutate_remove=mutate_remove,
    )
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": hosts}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "play.yml").write_text(
        yaml.safe_dump([_play(rehearsal_pass, serial)], sort_keys=False)
    )
    shutil.copy(REPO_ANSIBLE_CFG, work / "ansible.cfg")
    config = REPO_ANSIBLE_CFG.read_text()
    assert re.search(r"^roles_path\s*=\s*roles$", config, re.M)
    environment = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    environment |= {
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_HOME": str(work / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(work / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
    }
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-f",
            "8",
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
    assert CANARY not in output, name
    if not allow_deprecation:
        assert "DEPRECATION" not in output, (name, output[-3000:])
    return output


def _host(root: Path, **overrides: Any) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    return {
        "ansible_connection": "local",
        f"{PREFIX}enabled": True,
        f"{PREFIX}confirmation": CONFIRMATION,
        f"{PREFIX}source_revision": REVISION,
        "rehearsal_root": str(root),
        "rehearsal_units": STOPPED_UNITS,
        "rehearsal_owner": pwd.getpwuid(os.getuid()).pw_name,
        **overrides,
    }


def _outcome(root: Path, rehearsal_pass: str = "first") -> dict | None:
    path = root / f"outcome-{rehearsal_pass}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _units(root: Path) -> list[Path]:
    return [root / p.lstrip("/") for p in UNIT_PATHS]


def _kept(root: Path) -> bool:
    return all(path.read_text() == "[Unit]\n" for path in _units(root))


def _gone(root: Path) -> bool:
    return not any(os.path.lexists(path) for path in _units(root))


def _rooted(root: Path, paths: list[str]) -> str:
    return json.dumps([f"{root}{p}" for p in paths])


def _report(root: Path, removed: list[str], absent: list[str]) -> str:
    return (
        f"Native Coding worker and custody unit uninstall; source_revision={REVISION}; "
        f"removed={_rooted(root, removed)}; already_absent={_rooted(root, absent)}; "
        "daemon_reloaded=true; services_stopped=false; services_started=false; "
        "credentials_touched=false; data_dirs_touched=false."
    )


def _message(task: str) -> str:
    source = _docs(MAIN) if task in {t["name"] for t in _docs(MAIN)} else _docs(REMOVE)
    return _flat(_task(task, source)["ansible.builtin.assert"]["fail_msg"])


def _assert_refused(root: Path, task: str, rehearsal_pass: str = "first") -> None:
    outcome = _outcome(root, rehearsal_pass)
    assert outcome is not None and outcome.get("task") == task, (root.name, outcome)
    if task != UNLINK_CHECK:
        assert _message(task) in [_flat(m) for m in outcome["messages"]], outcome


def _assert_refused_untouched(root: Path, task: str) -> None:
    _assert_refused(root, task)
    assert _kept(root), root.name
    assert not (root / "daemon-reloaded").exists(), root.name


@rehearsal
def test_rehearsal_uninstalls_only_when_every_guard_passes(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    roots: dict[str, Path] = {}
    hosts: dict[str, dict] = {}

    def add(name: str, *, tree: bool = True, **overrides: Any) -> Path:
        root = tmp_path / "hosts" / name
        hosts[name] = _host(root, **overrides)
        if tree:
            _unit_tree(root)
        roots[name] = root
        return root

    add("removed")
    add("disabled", **{f"{PREFIX}enabled": False})
    add("enabled_missing", **{f"{PREFIX}enabled": None})
    add("enabled_string", **{f"{PREFIX}enabled": CANARY})
    add("enabled_true_string", **{f"{PREFIX}enabled": "true"})
    add("enabled_flip", **{f"{PREFIX}enabled": "{{ item is defined }}"})
    _unit_tree(add("already_absent", tree=False), worker=False, custody=False)
    add("no_unit_dir", tree=False)
    _unit_tree(add("worker_only", tree=False), custody=False)
    add("confirmation_wrong", **{f"{PREFIX}confirmation": CANARY})
    add("confirmation_lower", **{f"{PREFIX}confirmation": CONFIRMATION.lower()})
    add("revision_upper", **{f"{PREFIX}source_revision": REVISION.upper()})
    add("revision_short", **{f"{PREFIX}source_revision": REVISION[:39]})
    add("revision_newline", **{f"{PREFIX}source_revision": REVISION + "\n"})
    add("revision_missing", **{f"{PREFIX}source_revision": None})
    for state, line in LIVE_UNITS.items():
        add(f"live_{state}", rehearsal_units=STOPPED_UNITS + line + "\n")
    for preset, value in INTERNAL_PRESETS.items():
        add(f"preset_{preset.removeprefix(PREFIX)}", **{preset: value})
    add("went_live_after", rehearsal_units_after=LIVE_UNITS["live_custody"] + "\n")
    add(
        "shadow_unit_file",
        rehearsal_unit_files="ditto-coding-custody@.service static -\n",
    )
    for swap in ("symlink", "hardlink", "directory"):
        root = add(f"swap_{swap}", tree=False)
        unit_dir = _unit_tree(root, worker=False)
        worker = unit_dir / Path(WORKER).name
        (root / "outside").write_text("keep")
        if swap == "symlink":
            worker.symlink_to(root / "outside")
        elif swap == "hardlink":
            os.link(root / "outside", worker)
        else:
            worker.mkdir()
    partial = add("partial", tree=False)
    unit_dir = _unit_tree(partial, custody=False)
    (unit_dir / Path(CUSTODY).name).symlink_to(partial / "outside")
    (partial / "outside").write_text("keep")
    parent_link = add("parent_symlink", tree=False)
    real = _unit_tree(parent_link / "real")
    (parent_link / "etc/systemd").mkdir(parents=True)
    (parent_link / "etc/systemd/system").symlink_to(real)
    add("dir_writable", tree=False)
    _unit_tree(roots["dir_writable"]).chmod(0o775)
    add("drop_in", tree=False)
    (_unit_tree(roots["drop_in"]) / "ditto-coding-hosted-worker.service.d").mkdir()
    add("wants_link", tree=False)
    wants = _unit_tree(roots["wants_link"]) / "multi-user.target.wants"
    wants.mkdir()
    (wants / "ditto-coding-hosted-worker.service").symlink_to(
        roots["wants_link"] / "etc/systemd/system/ditto-coding-hosted-worker.service"
    )

    output = _run(tmp_path, "bulk", hosts)
    assert "PLAY RECAP" in output

    # A full removal reloads and reports both paths; the unrelated unit stays.
    assert _outcome(roots["removed"]) == {
        "report": _report(roots["removed"], UNIT_PATHS, [])
    }
    assert _gone(roots["removed"]) and (roots["removed"] / "daemon-reloaded").exists()
    assert (roots["removed"] / "etc/systemd/system/unrelated.service").exists()
    assert _outcome(roots["already_absent"]) == {
        "report": _report(roots["already_absent"], [], UNIT_PATHS)
    }
    assert _outcome(roots["no_unit_dir"]) == {
        "report": _report(roots["no_unit_dir"], [], UNIT_PATHS)
    }
    assert _outcome(roots["worker_only"]) == {
        "report": _report(roots["worker_only"], [WORKER], [CUSTODY])
    }
    # Disabled, missing, non-boolean and lazily templated flags are a no-op.
    for name in (
        "disabled",
        "enabled_missing",
        "enabled_string",
        "enabled_true_string",
        "enabled_flip",
    ):
        assert _outcome(roots[name]) == {"report": None}, name
        assert _kept(roots[name]) and not (roots[name] / "daemon-reloaded").exists(), (
            name
        )
    for name in (
        "confirmation_wrong",
        "confirmation_lower",
        "revision_upper",
        "revision_short",
        "revision_newline",
        "revision_missing",
    ):
        _assert_refused_untouched(roots[name], GATE)
    for state in LIVE_UNITS:
        _assert_refused_untouched(roots[f"live_{state}"], LIVE)
    for preset in INTERNAL_PRESETS:
        _assert_refused_untouched(
            roots[f"preset_{preset.removeprefix(PREFIX)}"], PRESET
        )
    # Removed, then refused on the post-removal checks.
    _assert_refused(roots["went_live_after"], LIVE_AFTER)
    assert _gone(roots["went_live_after"])
    _assert_refused(roots["shadow_unit_file"], UNIT_FILES_CHECK)
    assert _gone(roots["shadow_unit_file"])
    # Swapped entries: nothing after the refusal is attempted, the link target
    # survives, systemd is still reloaded and the receipt names every state.
    for swap in ("symlink", "hardlink", "directory"):
        root = roots[f"swap_{swap}"]
        _assert_refused(root, UNLINK_CHECK)
        (message,) = _outcome(root)["messages"]
        worker = f"{root}{WORKER}"
        assert f'refused=["{worker}: ' in _flat(message), message
        assert (
            f"removed=[]; already_absent=[]; not_attempted={_rooted(root, [CUSTODY])}"
            in _flat(message)
        )
        assert (root / "outside").read_text() == "keep"
        assert (root / CUSTODY.lstrip("/")).exists()
        assert (root / "daemon-reloaded").exists()
    root = roots["partial"]
    _assert_refused(root, UNLINK_CHECK)
    (message,) = _outcome(root)["messages"]
    assert f"removed={_rooted(root, [WORKER])}" in _flat(message)
    assert (
        f'refused=["{root}{CUSTODY}: is a symlink, directory or special file"]'
        in _flat(message)
    )
    assert not os.path.lexists(root / WORKER.lstrip("/"))
    assert (root / "outside").read_text() == "keep"
    assert (root / "daemon-reloaded").exists()
    # Directory-level refusals remove nothing.
    for name in ("parent_symlink", "dir_writable", "drop_in", "wants_link"):
        _assert_refused(roots[name], UNLINK_CHECK)
        (message,) = _outcome(roots[name])["messages"]
        assert "removed=[]" in _flat(message), (name, message)
    assert all(
        (roots["parent_symlink"] / "real" / p.lstrip("/")).exists() for p in UNIT_PATHS
    )
    assert (
        _kept(roots["dir_writable"])
        and _kept(roots["drop_in"])
        and _kept(roots["wants_link"])
    )
    assert 'foreign=["ditto-coding-hosted-worker.service.d"]' in _flat(
        _outcome(roots["drop_in"])["messages"][0]
    )
    assert (
        'foreign=["multi-user.target.wants/ditto-coding-hosted-worker.service"]'
        in _flat(_outcome(roots["wants_link"])["messages"][0])
    )

    # Idempotent second run: what the first run removed is now already absent.
    second = {n: hosts[n] for n in ("removed", "worker_only")}
    _run(tmp_path, "second", second, rehearsal_pass="second")
    assert _outcome(roots["removed"], "second") == {
        "report": _report(roots["removed"], [], UNIT_PATHS)
    }
    assert _outcome(roots["worker_only"], "second") == {
        "report": _report(roots["worker_only"], [], UNIT_PATHS)
    }


@rehearsal
def test_rehearsal_forged_identity_and_facts_are_refused(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    root = tmp_path / "hosts/forged"
    hosts = {"forged": _host(root)}
    _unit_tree(root)
    forged = {
        "ansible_facts": {
            "hostname": REVIEWED_HOST,
            "architecture": "x86_64",
            "distribution": "Debian",
            "distribution_major_version": "13",
        },
        "ansible_hostname": REVIEWED_HOST,
    }
    # Production identity literals: this machine is not the dedicated host, and
    # extra-vars facts cannot stand in for the registered probe.
    _run(tmp_path, "forged", hosts, "-e", json.dumps(forged), local_identity=False)
    _assert_refused_untouched(root, HOST)


@rehearsal
def test_rehearsal_targets_exactly_the_one_dedicated_host(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    ok = tmp_path / "hosts/ok"
    _unit_tree(ok)
    _run(tmp_path, "single", {REVIEWED_HOST: _host(ok)}, real_batch=True)
    assert _outcome(ok) == {"report": _report(ok, UNIT_PATHS, [])}

    for name, serial in (("extra", None), ("serial", 1)):
        roots = {
            h: tmp_path / "hosts" / f"{name}_{h}" for h in (REVIEWED_HOST, "rogue-vm")
        }
        for root in roots.values():
            _unit_tree(root)
        _run(
            tmp_path,
            name,
            {h: _host(r) for h, r in roots.items()},
            real_batch=True,
            serial=serial,
        )
        for root in roots.values():
            _assert_refused_untouched(root, BATCH)

    # -e inventory_hostname cannot forge the batch.
    forged = tmp_path / "hosts/forged_name"
    _unit_tree(forged)
    _run(
        tmp_path,
        "forged_name",
        {"wrong-host": _host(forged)},
        "-e",
        f"inventory_hostname={REVIEWED_HOST}",
        real_batch=True,
    )
    _assert_refused_untouched(forged, BATCH)


@rehearsal
def test_rehearsal_check_mode_is_refused_when_enabled_and_dormant_when_not(
    tmp_path,
) -> None:
    tmp_path = tmp_path.resolve()
    roots = {n: tmp_path / "hosts" / n for n in ("enabled", "disabled")}
    for root in roots.values():
        _unit_tree(root)
    hosts = {
        "enabled": _host(roots["enabled"]),
        "disabled": _host(roots["disabled"], **{f"{PREFIX}enabled": False}),
    }
    _run(tmp_path, "check", hosts, "--check")
    _assert_refused_untouched(roots["enabled"], CHECK_MODE)
    assert _outcome(roots["disabled"]) == {"report": None}
    assert _kept(roots["disabled"])


def _start_hosts(tmp_path: Path, label: str) -> dict[str, tuple[dict, Path]]:
    out = {}
    for name, enabled in (("on", True), ("off", False)):
        root = tmp_path / "hosts" / f"{label}_{name}"
        _unit_tree(root)
        out[name] = (
            _host(
                root,
                **{f"{PREFIX}enabled": enabled},
                rehearsal_units=STOPPED_UNITS + LIVE_UNITS["live_custody"] + "\n",
            ),
            root,
        )
    return out


@rehearsal
def test_rehearsal_start_at_every_task_cannot_skip_the_guards(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    # Presets that would bypass the live-unit guard and open the gate if any
    # start point could skip the in-include refusal. Both hosts list a live unit.
    presets = json.dumps({GATE_NAME: True, f"{PREFIX}units": {"stdout_lines": []}})
    main_names = [t["name"] for t in _docs(MAIN)]
    remove_names = [t["name"] for t in _docs(REMOVE)]
    for index, task in enumerate(main_names + remove_names):
        label = f"start{index:02d}"
        hosts = _start_hosts(tmp_path, label)
        _run(
            tmp_path,
            label,
            {n: h for n, (h, _) in hosts.items()},
            "--start-at-task",
            task,
            "-e",
            presets,
        )
        for name, (_, root) in hosts.items():
            assert _kept(root), (task, name)
            assert not (root / "daemon-reloaded").exists(), (task, name)
            outcome = _outcome(root)
            if task == PRESET:
                _assert_refused(root, PRESET)
            elif task in main_names:
                # Starting past main.yml's refusal reaches the include, whose
                # first task refuses the preset gate and registered result.
                assert outcome is not None and outcome["task"] == PRESET_INCLUDE, (
                    task,
                    outcome,
                )
            else:
                # Tasks of the dynamically included file are invisible to
                # --start-at-task, so nothing in the play ran at all.
                assert outcome is None, (task, outcome)


# ─── Mutations: each guard is load-bearing ────────────────────────────────────


def _drop(name: str):
    def mutate(tasks: list[dict]) -> None:
        tasks[:] = [t for t in tasks if t.get("name") != name]

    return mutate


def _drop_that(name: str, index: int):
    def mutate(tasks: list[dict]) -> None:
        del _task(name, tasks)["ansible.builtin.assert"]["that"][index]

    return mutate


def _mutant(tmp_path: Path, label: str, host: dict, *flags: str, **kwargs: Any) -> Path:
    root = Path(host["rehearsal_root"])
    _run(tmp_path, f"mutant_{label}", {label: host}, *flags, **kwargs)
    return root


def _tree_host(tmp_path: Path, label: str, **overrides: Any) -> dict:
    root = tmp_path / "hosts" / f"mutant_{label}"
    _unit_tree(root)
    return _host(root, **overrides)


@rehearsal
def test_rehearsal_mutations_prove_every_guard_is_load_bearing(tmp_path) -> None:
    tmp_path = tmp_path.resolve()
    live = STOPPED_UNITS + LIVE_UNITS["live_custody"] + "\n"

    # main.yml preset refusal and in-include refusal both removed: a -e preset
    # registered listing hides a live unit and the units are removed.
    both = _mutant(
        tmp_path,
        "presets",
        _tree_host(tmp_path, "presets", rehearsal_units=live),
        "-e",
        json.dumps({f"{PREFIX}units": {"stdout_lines": []}}),
        mutate_main=_drop(PRESET),
        mutate_remove=_drop(PRESET_INCLUDE),
    )
    assert _gone(both)

    # In-include refusal removed: --start-at-task past main.yml's refusal with
    # the same preset removes the units.
    start = _mutant(
        tmp_path,
        "in_include",
        _tree_host(tmp_path, "in_include", rehearsal_units=live),
        "--start-at-task",
        GATE_FREEZE,
        "-e",
        json.dumps({f"{PREFIX}units": {"stdout_lines": []}}),
        mutate_remove=_drop(PRESET_INCLUDE),
    )
    assert _gone(start)

    # Raw enabled re-assert removed: --start-at-task past main.yml's refusal
    # with -e gate=true opens the removal even though enabled is false.
    raw = _mutant(
        tmp_path,
        "raw_enabled",
        _tree_host(tmp_path, "raw_enabled", **{f"{PREFIX}enabled": False}),
        "--start-at-task",
        GATE_FREEZE,
        "-e",
        json.dumps({GATE_NAME: True}),
        mutate_remove=_drop_that(PRESET_INCLUDE, 0),
    )
    assert _gone(raw)

    # The gate filter replaced by bool: a non-boolean string opens the removal.
    def bool_gate(main: list[dict]) -> None:
        _task(GATE_FREEZE, main)["ansible.builtin.set_fact"][GATE_NAME] = (
            f"{{{{ {PREFIX}enabled | bool }}}}"
        )

    def bool_raw(tasks: list[dict]) -> None:
        _task(PRESET_INCLUDE, tasks)["ansible.builtin.assert"]["that"][0] = (
            f"{PREFIX}enabled | bool"
        )

    root = tmp_path / "hosts/mutant_bool"
    _unit_tree(root)
    _run(
        tmp_path,
        "mutant_bool",
        {"bool": _host(root, **{f"{PREFIX}enabled": "yes"})},
        mutate_main=bool_gate,
        mutate_remove=bool_raw,
        allow_deprecation=True,
    )
    assert _gone(root)

    # Batch guard removed: an extra host in the play is uninstalled too.
    roots = {
        h: tmp_path / "hosts" / f"mutant_batch_{h}" for h in (REVIEWED_HOST, "rogue-vm")
    }
    for r in roots.values():
        _unit_tree(r)
    _run(
        tmp_path,
        "mutant_batch",
        {h: _host(r) for h, r in roots.items()},
        real_batch=True,
        mutate_remove=_drop(BATCH),
    )
    assert all(_gone(r) for r in roots.values())

    # Check-mode guard removed: an enabled --check run probes the host and
    # drives the module in check mode instead of refusing up front; it then
    # fails only on the receipt because nothing was removed.
    check = _mutant(
        tmp_path,
        "check",
        _tree_host(tmp_path, "check"),
        "--check",
        mutate_remove=_drop(CHECK_MODE),
    )
    outcome = _outcome(check)
    assert outcome is not None and outcome["task"] == UNLINK_CHECK, outcome
    assert _kept(check) and not (check / "daemon-reloaded").exists()

    # Confirmation/revision guard removed: a wrong confirmation uninstalls.
    gate = _mutant(
        tmp_path,
        "gate",
        _tree_host(tmp_path, "gate", **{f"{PREFIX}confirmation": "no"}),
        mutate_remove=_drop(GATE),
    )
    assert _gone(gate)

    # Host guard removed: a forged-fact run on the wrong machine uninstalls.
    host = _mutant(
        tmp_path,
        "host",
        _tree_host(tmp_path, "host"),
        "-e",
        json.dumps({"ansible_hostname": REVIEWED_HOST}),
        local_identity=False,
        mutate_remove=_drop(HOST),
    )
    assert _gone(host)

    # Live-unit guard removed: a live custody instance loses its template.
    live_root = _mutant(
        tmp_path,
        "live",
        _tree_host(tmp_path, "live", rehearsal_units=live),
        mutate_remove=_drop(LIVE),
    )
    assert _gone(live_root)

    # Module receipt check removed: a refused symlink swap reports success.
    swap = tmp_path / "hosts/mutant_receipt"
    unit_dir = _unit_tree(swap, worker=False)
    (unit_dir / Path(WORKER).name).symlink_to(swap / "outside")
    _run(
        tmp_path,
        "mutant_receipt",
        {"receipt": _host(swap)},
        mutate_remove=_drop(UNLINK_CHECK),
    )
    outcome = _outcome(swap)
    assert outcome is not None and outcome.get("report", "").startswith(
        "Native Coding worker and custody unit uninstall"
    )

    # Reload removed: removal completes with no daemon-reload.
    reload = _mutant(
        tmp_path, "reload", _tree_host(tmp_path, "reload"), mutate_remove=_drop(RELOAD)
    )
    assert _gone(reload) and not (reload / "daemon-reloaded").exists()
    assert _outcome(reload)["report"].startswith("Native Coding worker and custody")

    # Post-removal live check removed: a unit that went live is reported clean.
    after = _mutant(
        tmp_path,
        "live_after",
        _tree_host(tmp_path, "live_after", rehearsal_units_after=live),
        mutate_remove=_drop(LIVE_AFTER),
    )
    assert "report" in _outcome(after)

    # Post-reload unit-file check removed: a shadow definition is reported clean.
    shadow = _mutant(
        tmp_path,
        "shadow",
        _tree_host(
            tmp_path,
            "shadow",
            rehearsal_unit_files="ditto-coding-custody@.service static -\n",
        ),
        mutate_remove=_drop(UNIT_FILES_CHECK),
    )
    assert "report" in _outcome(shadow)
