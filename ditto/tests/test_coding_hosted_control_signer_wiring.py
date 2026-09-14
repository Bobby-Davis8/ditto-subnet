"""Default-off host wiring for the hosted-v2 Platform control signer.

Platform converge may only stat-verify a seed placed by a separate protected
ceremony; validators register trust in one public address. Real Ansible
rendering and the stat guard's negative cases run in
infra/ansible/tests/coding-hosted-control-signer.yml (Infrastructure CI).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from ditto.validator import coding_hosted_control

ROOT = Path(__file__).parents[2]
ANSIBLE = ROOT / "infra/ansible"
PLATFORM_ROLE = ANSIBLE / "roles/platform_app"
VALIDATOR_ROLE = ANSIBLE / "roles/validator_stack"
SEED_DIRECTORY = "/etc/ditto-platform/coding-hosted-signer"
SEED_FILE = f"{SEED_DIRECTORY}/seed"
ANSIBLE_HOTKEY_PATTERN = "^5[1-9A-HJ-NP-Za-km-z]{47}$"
SEED_MARKERS = (
    "coding-hosted-signer",
    "coding_hosted_signer_seed",
    "DITTO_CODING_HOSTED_SIGNER_SEED_FILE",
)
# Modules that can read, create, move, hash or transport file contents.
CONTENT_MODULES = {
    "archive",
    "assemble",
    "blockinfile",
    "command",
    "copy",
    "fetch",
    "file",
    "get_url",
    "lineinfile",
    "raw",
    "replace",
    "script",
    "shell",
    "slurp",
    "synchronize",
    "template",
    "unarchive",
    "uri",
}
TASK_KEYWORDS = {
    "args",
    "become",
    "become_user",
    "block",
    "always",
    "changed_when",
    "environment",
    "failed_when",
    "loop",
    "loop_control",
    "name",
    "no_log",
    "notify",
    "register",
    "rescue",
    "vars",
    "when",
    "tags",
    "delegate_to",
    "run_once",
    "until",
    "retries",
    "delay",
    "ignore_errors",
    "check_mode",
    "listen",
}


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _tasks(items: Any) -> Iterator[dict[str, Any]]:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        for section in ("block", "rescue", "always", "tasks", "pre_tasks"):
            yield from _tasks(item.get(section))
        yield from _tasks(item.get("post_tasks"))
        yield from _tasks(item.get("handlers"))


def _module(task: dict[str, Any]) -> str:
    names = [key for key in task if key not in TASK_KEYWORDS]
    assert len(names) == 1, task
    return names[0].rsplit(".", 1)[-1]


def _converge_task_files() -> list[Path]:
    """Every task/handler/playbook YAML a real converge can execute."""
    files = sorted((ANSIBLE / "playbooks").glob("*.yml"))
    for role in sorted((ANSIBLE / "roles").iterdir()):
        for section in ("tasks", "handlers"):
            files.extend(sorted((role / section).glob("*.yml")))
    return files


def _text(task: dict[str, Any]) -> str:
    return yaml.safe_dump(task, sort_keys=True)


def test_platform_signer_wiring_ships_off_with_one_fixed_path() -> None:
    defaults = _load(PLATFORM_ROLE / "defaults/main.yml")
    template = (PLATFORM_ROLE / "templates/platform.env.j2").read_text()

    assert defaults["platform_coding_hosted_control_enabled"] is False
    assert defaults["platform_coding_hosted_signer_hotkey"] == ""
    assert defaults["platform_coding_hosted_signer_seed_directory"] == SEED_DIRECTORY
    assert defaults["platform_coding_hosted_signer_seed_file"] == SEED_FILE
    assert defaults["platform_coding_hosted_signer_seed_ancestors"] == [
        "/etc/ditto-platform",
        "/etc",
        "/",
    ]

    block = template[template.index("# --- Hosted-v2 native control signer") :]
    block = block[: block.index("{% endif %}") + len("{% endif %}")]
    enabled, disabled = block.split("{% else %}")
    assert "{% if platform_coding_hosted_control_enabled | bool %}" in enabled
    assert [
        line for line in enabled.splitlines() if "=" in line and "#" not in line
    ] == [
        "DITTO_CODING_HOSTED_CONTROL_ENABLED=true",
        f"DITTO_CODING_HOSTED_SIGNER_SEED_FILE={SEED_FILE}",
        "DITTO_CODING_HOSTED_SIGNER_HOTKEY="
        "{{ platform_coding_hosted_signer_hotkey | quote }}",
    ]
    assert [line for line in disabled.splitlines() if "=" in line] == [
        "DITTO_CODING_HOSTED_CONTROL_ENABLED=false"
    ]
    # The rendered path is a literal, never an overridable variable, and no
    # secret material or seed variable feeds the block.
    assert "platform_coding_hosted_signer_seed" not in template
    assert "platform_secrets" not in block
    assert template.count("DITTO_CODING_HOSTED_") == 4


def test_no_converge_task_reads_creates_copies_or_hashes_the_seed() -> None:
    seen_stats = 0
    for path in _converge_task_files():
        for task in _tasks(_load(path)):
            if "hosts" in task or {"block", "rescue", "always"} & task.keys():
                continue
            text = _text(task)
            if not any(marker in text for marker in SEED_MARKERS):
                continue
            module = _module(task)
            assert module not in CONTENT_MODULES, (path, task)
            assert module in {"assert", "stat", "include_tasks", "debug"}, (path, task)
            if module == "stat":
                seen_stats += 1
                arguments = task[next(key for key in task if key.endswith("stat"))]
                assert arguments["follow"] is False, (path, task)
                assert arguments["get_checksum"] is False, (path, task)
                assert arguments["get_mime"] is False, (path, task)
                assert arguments["get_attributes"] is False, (path, task)
                assert "checksum_algorithm" not in arguments, (path, task)
                assert path.parent == PLATFORM_ROLE / "tasks", path
    assert seen_stats == 3

    stat_file = (PLATFORM_ROLE / "tasks/coding_hosted_signer_seed_stat.yml").read_text()
    for forbidden in ("slurp", "copy", "template", "fetch", "get_checksum: true"):
        assert forbidden not in stat_file


def test_disabled_platform_converge_never_reaches_the_seed_path() -> None:
    main = _load(PLATFORM_ROLE / "tasks/main.yml")
    names = [task.get("name", "") for task in main]
    references = [
        task
        for task in main
        if any(
            marker in _text(task)
            for marker in (
                *SEED_MARKERS,
                "coding_hosted_signer",
                "coding_hosted_control",
            )
        )
    ]
    assert references == [
        {
            "name": "Verify the pre-placed hosted-v2 control signer without reading it",
            "ansible.builtin.include_tasks": "coding_hosted_signer.yml",
            "when": "platform_coding_hosted_control_enabled | bool",
        }
    ]
    # Runs straight after preflight: before any package, secret read or render.
    include = names.index(references[0]["name"])
    assert (
        names[include - 1]
        == "Validate env-sourced configuration before rendering anything"
    )
    assert include < names.index("Render .env")

    # The stat guard is reachable only through the profile guard.
    includers = {
        path.name
        for path in _converge_task_files()
        if "coding_hosted_signer_seed_stat.yml" in path.read_text()
    }
    assert includers == {"coding_hosted_signer.yml"}
    signer = _load(PLATFORM_ROLE / "tasks/coding_hosted_signer.yml")
    assert [_module(task) for task in signer] == ["assert", "include_tasks"]
    profile = signer[0]["ansible.builtin.assert"]["that"]
    assert f"platform_coding_hosted_signer_seed_file == '{SEED_FILE}'" in profile
    assert (
        f"platform_coding_hosted_signer_seed_directory == '{SEED_DIRECTORY}'" in profile
    )
    assert (
        f"platform_coding_hosted_signer_hotkey is match('{ANSIBLE_HOTKEY_PATTERN}')"
        in profile
    )
    assert "platform_coding_hosted_signer_hotkey | length == 48" in profile
    assert "platform_owner != 'root'" in profile


def test_seed_guard_mirrors_the_platform_private_file_contract() -> None:
    guard = _load(PLATFORM_ROLE / "tasks/coding_hosted_signer_seed_stat.yml")
    conditions = {
        task["name"]: task["ansible.builtin.assert"]["that"]
        for task in guard
        if "ansible.builtin.assert" in task
    }
    assert conditions["Require safe hosted-v2 control signer seed ancestors"] == [
        "item.stat.exists",
        "item.stat.isdir",
        "not item.stat.islnk",
        "item.stat.pw_name | default('') in ['root', platform_owner]",
        "not item.stat.wgrp",
        "not item.stat.woth",
    ]
    directory = "platform_coding_hosted_signer_directory_stat.stat"
    assert conditions["Require the Platform-owned 0700 seed directory"] == [
        f"{directory}.exists",
        f"{directory}.isdir",
        f"not {directory}.islnk",
        f"{directory}.pw_name | default('') == platform_owner",
        f"{directory}.mode == '0700'",
    ]
    seed = "platform_coding_hosted_signer_seed_stat.stat"
    seed_guard = (
        "Require the pre-placed single-link 0600 32-byte seed "
        "owned by the Platform user"
    )
    assert conditions[seed_guard] == [
        f"{seed}.exists",
        f"{seed}.isreg",
        f"not {seed}.islnk",
        f"{seed}.pw_name | default('') == platform_owner",
        f"{seed}.mode == '0600'",
        f"{seed}.nlink == 1",
        f"{seed}.size == 32",
    ]


def test_platform_owner_is_the_pm2_api_user() -> None:
    defaults = _load(PLATFORM_ROLE / "defaults/main.yml")
    all_vars = _load(ANSIBLE / "group_vars/all.yml")
    main = (PLATFORM_ROLE / "tasks/main.yml").read_text()
    deploy = (ROOT / ".github/workflows/platform-deploy.yml").read_text()

    assert defaults["platform_owner"] == "{{ deploy_user | default('deploy') }}"
    assert all_vars["deploy_user"] == "deploy"
    assert "pm2 startup systemd -u {{ platform_owner }}" in main
    assert "sudo -iu deploy bash -lc" in deploy


def test_validator_trust_registration_is_default_off_and_distinct() -> None:
    defaults = _load(VALIDATOR_ROLE / "defaults/main.yml")
    template = (VALIDATOR_ROLE / "templates/validator.env.j2").read_text()
    main = _load(VALIDATOR_ROLE / "tasks/main.yml")
    trust = _load(VALIDATOR_ROLE / "tasks/coding_hosted_trust.yml")

    assert defaults["validator_stack_coding_hosted_control_enabled"] is False
    assert defaults["validator_stack_coding_hosted_platform_hotkey"] == ""
    assert (
        "VALIDATOR_CODING_HOSTED_CONTROL_ENABLED={{ 'true' if "
        "validator_stack_coding_hosted_control_enabled | bool else 'false' }}"
    ) in template
    assert (
        "VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY={{ "
        "validator_stack_coding_hosted_platform_hotkey if "
        "validator_stack_coding_hosted_control_enabled | bool else '' }}"
    ) in template

    assert len(trust) == 1
    assert trust[0]["when"] == "validator_stack_coding_hosted_control_enabled | bool"
    conditions = trust[0]["ansible.builtin.assert"]["that"]
    assert (
        "validator_stack_coding_hosted_platform_hotkey is match("
        f"'{ANSIBLE_HOTKEY_PATTERN}')"
    ) in conditions
    assert "validator_stack_coding_hosted_platform_hotkey | length == 48" in conditions
    assert (
        "validator_stack_coding_hosted_platform_hotkey != validator_stack_hotkey"
        in conditions
    )

    names = [task.get("name", "") for task in main]
    guard = names.index(
        "Validate hosted-v2 Platform control signer trust before mutating the host"
    )
    assert main[guard]["ansible.builtin.import_tasks"] == "coding_hosted_trust.yml"
    assert names[guard - 1] == "Validate production validator inputs"
    assert guard < names.index("Render the production validator environment")


def test_env_keys_and_hotkey_shape_match_the_control_command() -> None:
    template = (VALIDATOR_ROLE / "templates/validator.env.j2").read_text()
    compose = _load(ROOT / "docker-compose.yml")
    environment = compose["services"]["ditto-subnet"]["environment"]
    keys = {
        coding_hosted_control.ENABLED_ENV,
        coding_hosted_control.PLATFORM_HOTKEY_ENV,
    }

    rendered = set(re.findall(r"^(VALIDATOR_CODING_HOSTED_[A-Z_]+)=", template, re.M))
    passed = {key for key in environment if key.startswith("VALIDATOR_CODING_HOSTED_")}
    assert rendered == passed == keys
    synthetic = "5DtDLm5rQHShDqojQpsvcN8tRXHVFaecfDoRet1SU6BFD9Fi"
    assert re.fullmatch(ANSIBLE_HOTKEY_PATTERN.strip("^$"), synthetic)
    assert coding_hosted_control._HOTKEY.fullmatch(synthetic)


def test_compose_passes_hosted_control_trust_through_off_and_empty() -> None:
    compose = _load(ROOT / "docker-compose.yml")
    environment = compose["services"]["ditto-subnet"]["environment"]
    assert environment["VALIDATOR_CODING_HOSTED_CONTROL_ENABLED"] == (
        "${VALIDATOR_CODING_HOSTED_CONTROL_ENABLED:-false}"
    )
    assert environment["VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY"] == (
        "${VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY:-}"
    )
    for name, service in compose["services"].items():
        if name == "ditto-subnet":
            continue
        assert "VALIDATOR_CODING_HOSTED_" not in yaml.safe_dump(service), name


def test_only_the_control_command_reads_validator_trust() -> None:
    readers = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "ditto").rglob("*.py")
        if "VALIDATOR_CODING_HOSTED_" in path.read_text()
        and "tests" not in path.relative_to(ROOT).parts
    }
    assert readers == {"ditto/validator/coding_hosted_control.py"}


def test_inventory_and_workflows_never_enable_the_signer_or_trust() -> None:
    paths = [
        *sorted(ANSIBLE.rglob("*.yml")),
        *sorted((ROOT / ".github/workflows").glob("*.yml")),
    ]
    for path in paths:
        if path.is_relative_to(ANSIBLE / "tests") or path.is_relative_to(
            ANSIBLE / "roles"
        ):
            continue
        text = path.read_text()
        for flag in (
            "platform_coding_hosted_control_enabled",
            "platform_coding_hosted_signer_hotkey",
            "validator_stack_coding_hosted_control_enabled",
            "validator_stack_coding_hosted_platform_hotkey",
        ):
            assert flag not in text, path


def test_infra_ci_runs_the_rendering_and_stat_guard_fixture() -> None:
    workflow = _load(ROOT / ".github/workflows/infra-ci.yml")
    steps = workflow["jobs"]["ansible"]["steps"]
    commands = [step.get("run", "") for step in steps]
    fixture = [
        command for command in commands if "coding-hosted-control-signer.yml" in command
    ]
    assert fixture == [
        "uvx --from ansible-core==2.21.2 ansible-playbook "
        "-i localhost, tests/coding-hosted-control-signer.yml"
    ]
    play = _load(ANSIBLE / "tests/coding-hosted-control-signer.yml")[0]
    assert play["hosts"] == "localhost"
    assert play["connection"] == "local"
    assert play["become"] is False
    assert "roles" not in play
    fixture_text = "\n".join(
        path.read_text()
        for path in sorted((ANSIBLE / "tests").glob("coding-hosted-control-*.yml"))
    )
    assert "become: true" not in fixture_text


def test_ceremony_doc_keeps_key_custody_out_of_automation() -> None:
    doc = (ROOT / "infra/docs/coding-hosted-control-signer-v2.md").read_text()
    for required in (
        SEED_FILE,
        "protected ceremony",
        "curator",
        "hosted_control_configured",
        "Rotation",
        "Revocation",
    ):
        assert required in doc, required
    platform_doc = (
        ROOT / "apps/platform/docs/coding-hosted-control-startup-v2.md"
    ).read_text()
    assert "infra/docs/coding-hosted-control-signer-v2.md" in platform_doc
