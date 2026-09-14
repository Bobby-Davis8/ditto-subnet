"""Native host prerequisites; synthetic checks never change a host or start a unit."""

import importlib.util
import ipaddress
import json
import re
import socket
import threading
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_prerequisites"
PLACEHOLDER = "{{ coding_hosted_prerequisites_host_address }}"
WORKER = "30000000-0000-4000-8000-000000000003"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HELPER = load("hosted_prerequisites", ROLE / "files/host-prerequisites.py")
PROXY = load("hosted_egress_proxy", ROLE / "files/egress-proxy.py")
HOST = load(
    "hosted_policy_for_prerequisites",
    ROOT / "infra/ansible/roles/coding_hosted/files/host-policy.py",
)
CONNECTIVITY = load(
    "hosted_connectivity_for_prerequisites",
    ROOT
    / "infra/ansible/roles/coding_hosted_connectivity/files/connectivity-policy.py",
)
TASKS_SOURCE = (ROLE / "tasks/main.yml").read_text()
GUARD_SOURCE = (ROLE / "tasks/guard.yml").read_text()
UNIT = (ROLE / "templates/egress-proxy.service.j2").read_text()


def render(address="10.33.0.2"):
    template = (ROLE / "templates/host-prerequisites.json.j2").read_text()
    assert template.count("{{") == template.count(PLACEHOLDER) == 2
    return json.loads(template.replace(PLACEHOLDER, address))


def block():
    return yaml.safe_load(TASKS_SOURCE)[1]["block"]


def task(name):
    return next(item for item in block() if item["name"] == name)


def test_role_is_default_off_with_exact_host_source_and_confirmation():
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == {
        "coding_hosted_prerequisites_enabled": False,
        "coding_hosted_prerequisites_confirmation": "",
        "coding_hosted_prerequisites_source_revision": "",
        "coding_hosted_prerequisites_host_address": "",
    }
    tasks = yaml.safe_load(TASKS_SOURCE)
    assert len(tasks) == 2
    assert tasks[0]["when"] == "not (coding_hosted_prerequisites_enabled | bool)"
    assert tasks[1]["when"] == "coding_hosted_prerequisites_enabled | bool"
    assert block()[0] == {
        "name": "Run the exact host and input guard",
        "ansible.builtin.import_tasks": "guard.yml",
    }
    that = yaml.safe_load(GUARD_SOURCE)[0]["ansible.builtin.assert"]["that"]
    for condition in (
        "inventory_hostname in groups.get('role_coding_hosted', [])",
        "ansible_facts['hostname'] == 'ditto-coding-hosted-v2'",
        "ansible_facts['architecture'] == 'x86_64'",
        "ansible_facts['distribution'] == 'Debian'",
        "ansible_facts['distribution_major_version'] == '13'",
        "coding_hosted_prerequisites_confirmation == "
        "'CONVERGE NATIVE CODING HOST PREREQUISITES'",
        "coding_hosted_prerequisites_source_revision | length == 40",
        "coding_hosted_prerequisites_source_revision is match('^[0-9a-f]{40}$')",
        "coding_hosted_prerequisites_host_address == "
        "(ansible_facts['default_ipv4'] | default({})).get('address')",
    ):
        assert condition in that
    assert task("Refuse check mode for an enabled convergence")[
        "ansible.builtin.assert"
    ]["that"] == ["not ansible_check_mode"]


def test_playbook_fixture_and_ci_never_enable_the_role():
    playbook = yaml.safe_load(
        (
            ROOT / "infra/ansible/playbooks/gcp-coding-hosted-prerequisites.yml"
        ).read_text()
    )[0]
    assert playbook["hosts"] == "role_coding_hosted"
    assert playbook["roles"] == ["coding_hosted_prerequisites"]
    fixture = yaml.safe_load(
        (ROOT / "infra/ansible/tests/coding-hosted-prerequisites.yml").read_text()
    )
    assert all(play["hosts"] == "localhost" for play in fixture)
    assert fixture[0]["roles"] == ["coding_hosted_prerequisites"]
    assert "coding_hosted_prerequisites_enabled is false" in yaml.safe_dump(fixture)
    negative = (
        ROOT / "infra/ansible/tests/coding-hosted-prerequisites-negative.yml"
    ).read_text()

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    # No fixture play, task or set_fact can turn the role on.
    assert "coding_hosted_prerequisites_enabled" not in set(
        keys([fixture, yaml.safe_load(negative)])
    )
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-prerequisites.yml" in workflow
    assert "-i localhost, tests/coding-hosted-prerequisites.yml" in workflow
    assert "coding_hosted_prerequisites_enabled" not in workflow


def test_rendered_record_passes_helper_and_fits_connectivity_candidate_tcp():
    record = render()
    assert record == {
        "schema": "dittobench-coding-hosted-host-prerequisites-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "router_listen": "10.33.0.2:18080",
        "egress_network": "ditto-coding-restricted",
        "egress_proxy": "http://10.33.0.2:18090",
        # Every language runtime and the Rust driver require exactly 10001.
        "candidate_uid": 10001,
        "candidate_gid": 10001,
    }
    assert HELPER.settings(record) == (("10.33.0.2", 18080), ("10.33.0.2", 18090))
    endpoints = [
        {"address": "10.33.0.2", "port": 18080},
        {"address": "10.33.0.2", "port": 18090},
    ]
    for rollout in (False, True):
        assert CONNECTIVITY.pairs(endpoints, candidate=True, rollout=rollout) == [
            ("10.33.0.2", 18080),
            ("10.33.0.2", 18090),
        ]


def test_every_guard_address_is_a_valid_record_and_nothing_else_in_the_subnet():
    that = yaml.safe_load(GUARD_SOURCE)[0]["ansible.builtin.assert"]["that"]
    guard = next(
        condition[len("coding_hosted_prerequisites_host_address is match('") : -2]
        for condition in that
        if condition.startswith("coding_hosted_prerequisites_host_address is match(")
    )
    accepted = [
        str(address)
        for address in ipaddress.IPv4Network("10.33.0.0/24")
        if re.match(guard, str(address))
    ]
    assert accepted == [f"10.33.0.{last}" for last in range(2, 254)]
    for address in accepted:
        HELPER.settings(render(address))


@pytest.mark.parametrize(
    "field,value",
    [
        ("router_listen", "0.0.0.0:18080"),
        ("router_listen", "127.0.0.1:18080"),
        ("router_listen", "8.8.8.8:18080"),
        ("router_listen", "169.254.169.254:18080"),
        ("router_listen", "100.64.0.1:18080"),
        ("router_listen", "10.33.0.2:80"),
        ("router_listen", "10.33.0.2:018080"),
        ("router_listen", "10.33.0.2:99999"),
        ("router_listen", "10.33.0.02:18080"),
        ("router_listen", "[fd00::1]:18080"),
        ("router_listen", "ditto-coding-hosted-v2:18080"),
        ("router_listen", "10.33.0.2:18090"),
        ("egress_proxy", "http://secret@10.33.0.2:18090"),
        ("egress_proxy", "https://10.33.0.2:18090"),
        ("egress_proxy", "http://10.33.0.2:18090/"),
        ("egress_proxy", "http://10.33.0.2:18090?x=1"),
        ("egress_proxy", "http://10.33.0.2"),
        ("egress_proxy", "http://10.33.0.3:18090"),
        ("egress_proxy", "http://8.8.8.8:18090"),
        ("egress_proxy", "10.33.0.2:18090"),
        ("egress_network", ""),
        ("egress_network", "-restricted"),
        ("egress_network", "Restricted"),
        ("egress_network", "a" * 129),
        ("egress_network", "ditto-job-0011223344556677"),
        ("egress_network", "restricted\n"),
        ("candidate_uid", 0),
        ("candidate_gid", 0),
        ("candidate_uid", 65537),
        ("candidate_uid", True),
        ("candidate_gid", "10001"),
        ("shadow_only", False),
        ("weight_eligible", True),
        ("schema", "dittobench-coding-hosted-runtime-v2"),
    ],
)
def test_record_rejects_what_the_runtime_or_connectivity_would_refuse(field, value):
    record = render()
    record[field] = value
    with pytest.raises(ValueError):
        HELPER.settings(record)


def test_record_is_a_closed_object():
    for mutate in (
        lambda record: record.update(docker_socket="/run/docker.sock"),
        lambda record: record.pop("egress_network"),
    ):
        record = render()
        mutate(record)
        with pytest.raises(ValueError):
            HELPER.settings(record)


def host_fixture(monkeypatch, *, port_range=(32768, 60999)):
    calls = []
    subordinate = {
        "/etc/subuid": f"{HOST.USER}:100000:65536\n",
        "/etc/subgid": f"{HOST.USER}:200000:65536\n",
    }
    monkeypatch.setattr(HELPER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        HELPER,
        "host_policy",
        lambda: {"identity": lambda: calls.append("identity"), "mapping": HOST.mapping},
    )
    monkeypatch.setattr(HELPER.Path, "read_text", lambda path: subordinate[str(path)])
    monkeypatch.setattr(HELPER, "ephemeral_range", lambda: port_range)
    monkeypatch.setattr(
        HELPER, "bindable", lambda address, port: calls.append((address, port))
    )
    return calls


def test_check_maps_candidate_identity_and_probes_both_exact_listeners(monkeypatch):
    record = render()
    calls = host_fixture(monkeypatch)
    receipt = HELPER.check(record)
    assert calls == ["identity", ("10.33.0.2", 18080), ("10.33.0.2", 18090)]
    assert receipt == {
        "schema": "dittobench-coding-hosted-host-prerequisites-check-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "router_listen": "10.33.0.2:18080",
        "egress_proxy": "http://10.33.0.2:18090",
        "egress_network": "ditto-coding-restricted",
        "candidate_host_uid": 110000,
        "candidate_host_gid": 210000,
        "services_started": False,
        "private_execution_ready": False,
    }


def test_check_requires_root_before_reading_host_state(monkeypatch):
    def forbidden():
        raise AssertionError("host state read before root check")

    monkeypatch.setattr(HELPER.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(HELPER, "host_policy", forbidden)
    with pytest.raises(ValueError):
        HELPER.check(render())


def test_check_refuses_ephemeral_overlap_busy_or_foreign_listener(monkeypatch):
    record = render()
    host_fixture(monkeypatch, port_range=(10000, 20000))
    with pytest.raises(ValueError):
        HELPER.check(record)

    host_fixture(monkeypatch)

    def busy(_address, _port):
        raise OSError("synthetic address in use")

    monkeypatch.setattr(HELPER, "bindable", busy)
    with pytest.raises(OSError):
        HELPER.check(record)


def test_check_refuses_a_range_that_cannot_map_the_candidate(monkeypatch):
    record = render()
    host_fixture(monkeypatch)
    monkeypatch.setattr(
        HELPER.Path, "read_text", lambda _path: f"{HOST.USER}:100000:1\n"
    )
    with pytest.raises(ValueError):
        HELPER.check(record)


def test_bind_probe_detects_busy_and_nonlocal_addresses():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        with pytest.raises(OSError):
            HELPER.bindable("127.0.0.1", held.getsockname()[1])
    with pytest.raises(OSError):
        HELPER.bindable("192.0.2.1", 18080)  # TEST-NET-1 is never assigned locally.


class Stream:
    def __init__(self, body):
        self.body = body

    def read(self, size):
        return self.body[:size]


@pytest.mark.parametrize(
    "argv,body",
    [
        (["check", "extra"], b"{}"),
        (["verify"], b"{}"),
        (["check"], b""),
        (["check"], b" " * 4097),
        (["check"], b'{"schema":"a","schema":"b"}'),
    ],
)
def test_main_accepts_only_one_bounded_unique_record(argv, body):
    with pytest.raises(ValueError):
        HELPER.main(argv, Stream(body))


@pytest.mark.parametrize(
    "argv",
    [
        ["127.0.0.1", "18090"],
        ["0.0.0.0", "18090"],
        ["8.8.8.8", "18090"],
        ["10.33.0.2", "80"],
        ["10.33.0.2", "018090"],
        ["10.33.0.2", "70000"],
        ["10.33.0.2"],
        ["10.33.0.2", "18090", "upstream.example"],
    ],
)
def test_proxy_listens_only_on_one_private_unprivileged_address(argv):
    with pytest.raises(ValueError):
        PROXY.listener(argv)


def test_proxy_accepts_the_record_listener():
    assert PROXY.listener(["10.33.0.2", "18090"]) == ("10.33.0.2", 18090)


def exchange(port, payload):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(payload)
        chunks = []
        while chunk := client.recv(4096):
            chunks.append(chunk)
        return b"".join(chunks)


@pytest.fixture
def refusing_server(monkeypatch):
    monkeypatch.setattr(PROXY, "DEADLINE_SECONDS", 0.5)
    server = PROXY.Server(("127.0.0.1", 0), PROXY.Refusal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_proxy_refuses_connect_and_every_other_method(refusing_server):
    connect = exchange(
        refusing_server, b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert connect.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    plain = exchange(
        refusing_server, b"GET http://169.254.169.254/ HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    assert plain.startswith(b"HTTP/1.1 405 Method Not Allowed\r\n")
    # A slow client is closed at the deadline without a tunnel.
    assert exchange(refusing_server, b"CONNECT slow") == b""


def test_proxy_has_no_upstream_connection_path():
    source = (ROLE / "files/egress-proxy.py").read_text()
    for forbidden in (
        "create_connection",
        ".connect(",
        "urllib",
        "http.client",
        "getaddrinfo",
    ):
        assert forbidden not in source


def live_pattern():
    assertion = task(
        "Refuse to converge while a worker, custody instance or egress proxy is live"
    )["ansible.builtin.assert"]["that"][0]
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
        "ditto-coding-hosted-egress-proxy.service loaded deactivating stop-sigterm x",
        "ditto-coding-hosted-egress-proxy.service loaded reloading reload Native",
        "ditto-coding-hosted-worker.service loaded maintenance cleaning Approved",
    ],
)
def test_live_units_are_refused(line):
    units = (
        "ditto-coding-hosted-worker.service not-found inactive dead x\n" + line + "\n"
    )
    assert live_pattern().search(units)


def test_stopped_or_failed_units_are_not_live():
    units = (
        "ditto-coding-hosted-worker.service loaded inactive dead Approved\n"
        f"ditto-coding-custody@{WORKER}.service loaded failed failed Native\n"
        "ditto-coding-hosted-egress-proxy.service not-found inactive dead x\n"
    )
    assert not live_pattern().search(units)
    assert not live_pattern().search("")
    command = task("List worker, custody and egress proxy units")[
        "ansible.builtin.command"
    ]
    assert command["argv"][-3:] == [
        "ditto-coding-hosted-worker.service",
        "ditto-coding-custody@*.service",
        "ditto-coding-hosted-egress-proxy.service",
    ]


def test_all_checks_precede_writes_and_nothing_is_started_enabled_or_created():
    names = [item["name"] for item in block()]
    first_write = names.index(
        "Install fixed prerequisite files without replacing existing bytes"
    )
    for check in (
        "Refuse check mode for an enabled convergence",
        "Require the existing daemon-identity deny guard",
        "Refuse to converge while a worker, custody instance or egress proxy is live",
        "Require the policy directory and host policy from daemon provisioning",
        "Refuse to rewrite unexpected existing prerequisite state",
        "Refuse overrides that could weaken the proxy sandbox",
        "Check the record and unit before installing anything",
        "Require the exact redacted check receipt",
    ):
        assert names.index(check) < first_write
    install = task("Install fixed prerequisite files without replacing existing bytes")
    assert install["ansible.builtin.copy"]["force"] is False
    assert install["loop"] == ["helper", "proxy", "record", "unit"]
    assert task("Reload unit definitions without enabling or starting the proxy") == {
        "name": "Reload unit definitions without enabling or starting the proxy",
        "ansible.builtin.systemd_service": {"daemon_reload": True},
    }
    state = task("Require the exact static, inactive and unmodified proxy unit")
    expected = state["ansible.builtin.assert"]["that"][0]
    for value in (
        "ActiveState=inactive",
        "DropInPaths=",
        "UnitFileState=static",
        "NeedDaemonReload=no",
    ):
        assert f"'{value}'" in expected
    source = TASKS_SOURCE + GUARD_SOURCE
    for forbidden in (
        "state: started",
        "state: restarted",
        "enabled: true",
        "masked:",
        "enable-linger",
        "docker network",
        "ansible.builtin.user",
        "ansible.builtin.group",
        "runuser",
        "nft",
        "ansible.builtin.shell",
        "no_log",
    ):
        assert forbidden not in source
    assert "systemctl, start" not in source and "- start" not in source
    # The only removal is the role's own scratch directory.
    assert source.count("state: absent") == 1
    scratch = 'path: "{{ coding_hosted_prerequisites_scratch.path }}"'
    assert f"{scratch}\n            state: absent" in source


def test_proxy_unit_is_manual_refusing_and_ip_confined():
    assert "[Install]" not in UNIT
    for line in (
        "Type=exec",
        "DynamicUser=yes",
        "ExecStart=/usr/bin/python3 -I -B "
        f"/usr/local/lib/ditto-coding-hosted/egress-proxy.py {PLACEHOLDER} 18090",
        "Restart=no",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "PrivateDevices=yes",
        "ProtectSystem=strict",
        "RestrictAddressFamilies=AF_INET",
        "SocketBindDeny=any",
        "SocketBindAllow=ipv4:tcp:18090",
        "IPAddressDeny=any",
        f"IPAddressAllow={PLACEHOLDER}/32",
        "StandardOutput=null",
        "StandardError=null",
    ):
        assert line in UNIT.splitlines()
    for forbidden in (
        "User=ditto-coding-hosted",
        "WantedBy",
        "Restart=always",
        "AF_UNIX",
        "AF_INET6",
    ):
        assert forbidden not in UNIT


def test_doc_states_the_boundaries():
    doc = " ".join(
        (ROOT / "infra/docs/coding-hosted-prerequisites-v2.md").read_text().split()
    )
    for boundary in (
        "CONVERGE NATIVE CODING HOST PREREQUISITES",
        "does not create a Docker network",
        "ditto-job-",
        "empty allowlist",
        "never enables or starts",
        "10001",
        "candidate_tcp",
        "Rollback",
        "weight_eligible=false",
    ):
        assert boundary in doc
