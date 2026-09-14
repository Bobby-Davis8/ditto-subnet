"""The Ansible role renders exactly what the native runtime parser accepts."""

import json
import os
from pathlib import Path

import pytest
import yaml

from ditto.api_server.coding_hosted_runtime_config import postgres_config
from ditto.api_server.coding_hosted_runtime_io import read_json

ROOT = Path(__file__).resolve().parents[5]
TASKS = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment/tasks/main.yml"


def _entries(host: str, password: str) -> list[str]:
    block = yaml.safe_load(TASKS.read_text())[1]["block"]
    write = next(task for task in block if "vars" in task)
    rendered = []
    for entry in write["vars"]["coding_hosted_postgres_environment_entries"]:
        rendered.append(
            entry.replace(
                "{{ coding_hosted_postgres_environment_host }}", host
            ).replace("{{ coding_hosted_postgres_environment_password }}", password)
        )
    assert not any("{{" in entry for entry in rendered)
    return rendered


def test_rendered_entries_parse_with_the_bounded_admitted_principal() -> None:
    config, entries = postgres_config(
        _entries("10.30.0.5", 's3cr3t-with=equals&json"quote')
    )
    assert (config.host, config.port, config.user, config.database) == (
        "10.30.0.5",
        5432,
        "ditto",
        "ditto_platform_prod",
    )
    assert config.password == 's3cr3t-with=equals&json"quote'
    assert (config.pool_min_size, config.pool_max_size, config.command_timeout) == (
        1,
        4,
        30.0,
    )
    assert len(entries) == 8


def test_parser_rejects_an_empty_password_the_role_also_refuses() -> None:
    with pytest.raises(ValueError):
        postgres_config(_entries("10.30.0.5", ""))


def test_ansible_to_json_bytes_load_through_the_private_reader(tmp_path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    path = directory / "postgres-environment.json"
    # Ansible's to_json is json.dumps with default separators and ASCII escapes.
    body = json.dumps(_entries("10.30.0.5", "p\u00e4ss/\\word")) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(body)
    config, _ = postgres_config(read_json(path, 128 << 10))
    assert config.password == "p\u00e4ss/\\word"
