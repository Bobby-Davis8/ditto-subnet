"""Hosted attempt config wrapper; synthetic checks never touch a host or unit."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_attempt_config"
TASKS_SOURCE = (ROLE / "tasks/main.yml").read_text()
PLATFORM_CLI = (
    ROOT / "apps/platform/ditto/coding_hosted_attempt_config.py"
).read_text()
PLATFORM = (
    ROOT / "apps/platform/ditto/api_server/coding_hosted_attempt_config.py"
).read_text()
KINDS = [
    "budget_profile",
    "execution_profile",
    "grading_profile",
    "inference_policy",
    "probe_receipt",
]
WORKER = "30000000-0000-4000-8000-000000000003"


def block():
    return yaml.safe_load(TASKS_SOURCE)[1]["block"]


def task(name):
    return next(item for item in block() if item["name"] == name)


def test_role_is_default_off_with_closed_pins_and_no_credential_inputs():
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == {
        "coding_hosted_attempt_config_enabled": False,
        "coding_hosted_attempt_config_confirmation": "",
        "coding_hosted_attempt_config_runtime_revision": "",
        "coding_hosted_attempt_config_evaluation_id": "",
        "coding_hosted_attempt_config_assignment_sha256": "",
        "coding_hosted_attempt_config_evidence_wrapping_key_sha256": "",
        "coding_hosted_attempt_config_inputs": {},
    }
    tasks = yaml.safe_load(TASKS_SOURCE)
    assert tasks[1]["when"] == "coding_hosted_attempt_config_enabled | bool"
    assert tasks[1]["vars"]["coding_hosted_attempt_config_kinds"] == KINDS
    that = task(
        "Require the dedicated host, exact pins and the attempt-bound confirmation"
    )["ansible.builtin.assert"]["that"]
    for expected in (
        "ansible_facts['hostname'] == 'ditto-coding-hosted-v2'",
        "inventory_hostname in groups.get('role_coding_hosted', [])",
        "coding_hosted_attempt_config_inputs.keys() | sort == "
        "coding_hosted_attempt_config_kinds",
    ):
        assert expected in that
    confirmation = next(item for item in that if "CONFIRMATION" in item.upper())
    assert "'MATERIALIZE HOSTED CODING ATTEMPT CONFIG '" in confirmation
    assert "coding_hosted_attempt_config_evaluation_id" in confirmation
    assert "coding_hosted_attempt_config_assignment_sha256" in confirmation
    assert "lookup(" not in TASKS_SOURCE
    assert not re.search(
        r"\b(password|secret|provider_key|hippius|postgres)", TASKS_SOURCE.lower()
    )


def test_transferred_names_match_the_materializer_inbox_scheme():
    transfer = task(
        "Transfer each input under its digest name without replacing existing bytes"
    )["ansible.builtin.copy"]
    assert transfer["force"] is False
    assert transfer["mode"] == "0600" and transfer["owner"] == "ditto-coding-hosted"
    assert transfer["dest"].endswith(
        "/inbox/{{ item.key | replace('_', '-') }}-{{ item.value.sha256 }}.json"
    )
    for kind in KINDS:
        assert f'inbox / f"{kind.replace("_", "-")}-{{' in PLATFORM
    verify = task("Require sealed worker-owned inputs with their reviewed digest")[
        "ansible.builtin.assert"
    ]["that"]
    assert "item.stat.checksum == item.item.value.sha256" in verify
    assert "item.stat.nlink == 1" in verify
    controller = task("Refuse a controller input that is not the reviewed regular file")
    assert (
        "item.stat.checksum == item.item.value.sha256"
        in controller["ansible.builtin.assert"]["that"]
    )


def test_materializer_runs_as_the_worker_with_the_platform_cli_flags():
    argv = task(
        "Materialize the configuration as the worker with the installed interpreter"
    )["ansible.builtin.command"]["argv"]
    assert argv[:9] == [
        "/usr/sbin/runuser",
        "-u",
        "ditto-coding-hosted",
        "--",
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "LANG=C.UTF-8",
        "LC_ALL=C.UTF-8",
    ]
    assert argv[10:14] == ["-B", "-I", "-m", "ditto.coding_hosted_attempt_config"]
    flags = [item for item in argv if item.startswith("--") and item != "--"]
    declared = re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', PLATFORM_CLI)
    assert flags == declared
    assert argv[argv.index("--probe-receipt-sha256") + 1] == (
        "{{ coding_hosted_attempt_config_inputs.probe_receipt.sha256 }}"
    )


def live_pattern():
    assertion = task("Refuse while the worker or any custody instance is live")[
        "ansible.builtin.assert"
    ]["that"][0]
    raw = assertion[
        assertion.index("search('") + len("search('") : assertion.index(
            "', multiline=True)"
        )
    ]
    return re.compile(raw.replace("\\\\", "\\"), re.MULTILINE)


@pytest.mark.parametrize(
    "line",
    [
        "ditto-coding-hosted-worker.service loaded active running Approved",
        f"ditto-coding-custody@{WORKER}.service loaded activating start-pre Native",
        f"ditto-coding-custody@{WORKER}.service loaded deactivating stop-sigterm x",
        "ditto-coding-hosted-worker.service loaded reloading reload Approved",
    ],
)
def test_live_units_are_refused(line):
    units = "ditto-coding-hosted-worker.service not-found inactive dead x\n" + line
    assert live_pattern().search(units + "\n")


def test_stopped_or_failed_units_are_not_live():
    units = (
        "ditto-coding-hosted-worker.service loaded inactive dead Approved\n"
        f"ditto-coding-custody@{WORKER}.service loaded failed failed Native\n"
    )
    assert not live_pattern().search(units)
    assert not live_pattern().search("")
    assert task("List worker and custody units")["ansible.builtin.command"]["argv"][
        -2:
    ] == ["ditto-coding-hosted-worker.service", "ditto-coding-custody@*.service"]


def test_every_check_precedes_writes_and_nothing_is_started():
    names = [item["name"] for item in block()]
    first_write = names.index(
        "Create a missing worker-owned inbox or attempts directory"
    )
    for check in (
        "Require the dedicated host, exact pins and the attempt-bound confirmation",
        "Require each input to be an absolute controller file and SHA-256",
        "Refuse check mode for an enabled materialization",
        "Refuse while the worker or any custody instance is live",
        "Refuse a controller input that is not the reviewed regular file",
        "Require the protected installed interpreter",
        "Refuse unsafe existing inbox or attempts directories without repairing them",
    ):
        assert names.index(check) < first_write
    assert names.index(
        "Require sealed worker-owned inputs with their reviewed digest"
    ) < names.index(
        "Materialize the configuration as the worker with the installed interpreter"
    )
    for forbidden in (
        "state: started",
        "state: restarted",
        "enabled: true",
        "systemd_service",
        "daemon_reload",
        "ansible.builtin.shell",
        "state: absent",
        "force: true",
        "- start",
    ):
        assert forbidden not in TASKS_SOURCE


def test_playbook_fixture_and_ci_never_enable_the_role():
    playbook = yaml.safe_load(
        (
            ROOT / "infra/ansible/playbooks/gcp-coding-hosted-attempt-config.yml"
        ).read_text()
    )[0]
    assert playbook["hosts"] == "role_coding_hosted"
    assert playbook["roles"] == ["coding_hosted_attempt_config"]
    fixture_source = (
        ROOT / "infra/ansible/tests/coding-hosted-attempt-config.yml"
    ).read_text()
    fixture = yaml.safe_load(fixture_source)
    assert [play["hosts"] for play in fixture] == ["localhost"]
    assert "vars" not in fixture[0]
    assert "not coding_hosted_attempt_config_enabled" in fixture_source
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-attempt-config.yml" in workflow
    assert "-i localhost, tests/coding-hosted-attempt-config.yml" in workflow
    assert "coding_hosted_attempt_config_enabled" not in workflow
