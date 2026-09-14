"""Native PostgreSQL environment materialization stays default-off and silent."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment"
TASKS = (ROLE / "tasks/main.yml").read_text()


def _task(name: str) -> dict:
    block = yaml.safe_load(TASKS)[1]["block"]
    return next(task for task in block if task["name"] == name)


def test_default_off_and_password_only_from_the_environment() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == {
        "coding_hosted_postgres_environment_enabled": False,
        "coding_hosted_postgres_environment_confirmation": "",
        "coding_hosted_postgres_environment_source_revision": "",
        "coding_hosted_postgres_environment_host": "",
        "coding_hosted_postgres_environment_password": (
            "{{ lookup('env', 'DITTO_CODING_PG_PASSWORD') | default('', true) }}"
        ),
    }
    assert "MATERIALIZE NATIVE CODING POSTGRES ENVIRONMENT" in TASKS
    assert "ansible_facts['hostname'] == 'ditto-coding-hosted-v2'" in TASKS


def test_credentials_are_never_logged_read_back_or_diffed() -> None:
    write = _task(
        "Write each reader's own PostgreSQL environment copy without printing it"
    )
    assert write["no_log"] is True and write["diff"] is False
    assert (
        _task("Require one bounded single-line password from the protected boundary")[
            "no_log"
        ]
        is True
    )
    for forbidden in (
        "slurp",
        "fetch",
        "get_checksum: true",
        "set -x",
        "gcloud secrets",
    ):
        assert forbidden not in TASKS
    report = _task("Report only that the copies exist")["ansible.builtin.debug"]["msg"]
    assert "password" not in report.lower()
    for name, task in (
        (task["name"], task) for task in yaml.safe_load(TASKS)[1]["block"]
    ):
        if "coding_hosted_postgres_environment_password" in yaml.safe_dump(task):
            assert task.get("no_log") is True, name


def test_each_reader_gets_its_own_owner_only_copy_for_the_admitted_principal() -> None:
    write = _task(
        "Write each reader's own PostgreSQL environment copy without printing it"
    )
    assert write["ansible.builtin.copy"]["mode"] == "0600"
    assert write["loop"] == [
        {
            "path": "/var/lib/ditto-coding-custody/private/postgres-environment.json",
            "owner": "ditto-coding-custody",
        },
        {
            "path": "/var/lib/ditto-coding-hosted/private/postgres-environment.json",
            "owner": "ditto-coding-hosted",
        },
    ]
    entries = write["vars"]["coding_hosted_postgres_environment_entries"]
    assert "POSTGRES_USER=ditto" in entries
    assert "POSTGRES_DB=ditto_platform_prod" in entries
    directories = _task("Create owner-only credential directories for each reader")
    assert directories["ansible.builtin.file"]["mode"] == "0700"
    assert {item["owner"] for item in directories["loop"]} == {
        "ditto-coding-custody",
        "ditto-coding-hosted",
    }
    refuse = _task(
        "Refuse to replace credentials under a live worker or custody instance"
    )
    assert "active|activating|deactivating|reloading" in yaml.safe_dump(refuse)
    listing = _task("List live worker and custody units")["ansible.builtin.command"]
    assert "ditto-coding-hosted-worker.service" in listing["argv"]
    assert "ditto-coding-custody@*.service" in listing["argv"]


def test_playbook_group_connection_and_ci_registration() -> None:
    (play,) = yaml.safe_load(
        (
            ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment.yml"
        ).read_text()
    )
    assert play["hosts"] == "role_coding_hosted"
    assert play["roles"] == ["coding_hosted_postgres_environment"]
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
