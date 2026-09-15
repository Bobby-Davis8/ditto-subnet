"""Native worker credential materialization is default-off, unforgeable, silent.

The structural tests parse the roles and assert their shape. The rehearsal, gated
by DITTO_ANSIBLE_REHEARSAL=1, runs the enabled tasks through ansible-core 2.21.2
against a temporary tree with obvious stand-in credentials and the real
symlink-safe helpers, and proves the guards refuse every forged, preset,
templated, wrong-state and wrong-metadata input, and that no stand-in value or
any digest of it in any algorithm ever reaches ansible output.
"""

import copy
import grp
import hashlib
import json
import os
import pwd
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_worker_credentials"
CLEANUP_ROLE = ROOT / "infra/ansible/roles/coding_hosted_worker_credentials_cleanup"
MAIN = (ROLE / "tasks/main.yml").read_text()
MATERIALIZE = (ROLE / "tasks/materialize.yml").read_text()
CLEANUP_MAIN = (CLEANUP_ROLE / "tasks/main.yml").read_text()
REMOVE = (CLEANUP_ROLE / "tasks/remove.yml").read_text()
PARSED = yaml.safe_dump(yaml.safe_load(MATERIALIZE), width=10_000)
PLAYBOOK = ROOT / "infra/ansible/playbooks/gcp-coding-hosted-worker-credentials.yml"
CLEANUP_PLAYBOOK = (
    ROOT / "infra/ansible/playbooks/gcp-coding-hosted-worker-credentials-cleanup.yml"
)
MATERIALIZE_HELPER = ROLE / "files/materialize_worker_credentials.py"
REMOVE_HELPER = CLEANUP_ROLE / "files/remove_worker_credentials.py"
IDLE_INCLUDE = ROLE / "tasks/assert_units_idle.yml"
CLEANUP_IDLE_INCLUDE = CLEANUP_ROLE / "tasks/assert_units_idle.yml"

PREFIX = "coding_hosted_worker_credentials_"
CLEANUP_PREFIX = "coding_hosted_worker_credentials_cleanup_"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
}
CLEANUP_INPUTS = {
    f"{CLEANUP_PREFIX}enabled": False,
    f"{CLEANUP_PREFIX}confirmation": "",
    f"{CLEANUP_PREFIX}source_revision": "",
}
CONFIRMATION = "MATERIALIZE NATIVE CODING WORKER CREDENTIALS"
CLEANUP_CONFIRMATION = "REMOVE NATIVE CODING WORKER CREDENTIALS"
REVISION = "0123456789abcdef0123456789abcdef01234567"
REHEARSAL_GATE = "DITTO_ANSIBLE_REHEARSAL"
OWNER = "ditto-coding-hosted"

# main.yml
GATE_FREEZE = "Freeze the enabled gate once"
DORMANT = "Explain dormant native worker credential materialization"
INCLUDE = "Materialize the worker-owned credential files behind the enabled gate"
# materialize.yml
PRESET = "Refuse preset registered results, captures and undocumented role inputs"
ENV_VAR = "Refuse a credential supplied as an Ansible variable"
CHECK_MODE = "Refuse check mode for an enabled materialization"
FREEZE_GATE = "Freeze the confirmation and source revision once"
GATE = "Require the exact confirmation and source revision as frozen literals"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the dedicated host"
CURATOR = "Refuse the offline curator secret key in the controller environment"
FREEZE_SECRETS = "Freeze the controller-environment credentials once"
CREDS = "Require each controller credential by name without printing its value"
DISTINCT = "Require eight distinct credential values without printing them"
RENDER = "Render the three credential documents once without printing them"
ACCOUNTS = "Inspect host accounts once"
ACCOUNT_CHECK = "Require the distinct worker and custodian identities"
LISTING = "List live worker and custody units"
LIVE = "Refuse to replace credentials unless every listed unit is inactive or failed"
WORKER_PROCS = "Require no process is running as the worker UID"
WORKER_PROCS_CHECK = "Refuse if any process runs as the worker UID"
DIRECTORIES = "Inspect the worker home and private directory without following links"
DIR_CHECK = "Require an existing owner-only private directory below a real worker home"
WRITE = "Write and verify the three files with the symlink-safe helper"
WRITE_CHECK = "Require the helper to have written and verified all three files"
RELIST = "Re-list live worker and custody units after writing"
LIVE_AFTER = "Refuse if any worker or custody unit went live during materialization"
REPORT = "Report only that the files exist and the revision that wrote them"
# assert_units_idle.yml (shared)
IDLE_ASSERT = "Require every listed worker or custody unit to be inactive or failed"

NO_LOG_TASKS = (FREEZE_GATE, CURATOR, FREEZE_SECRETS, CREDS, DISTINCT, RENDER)

ENV_NAMES = [
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY",
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY",
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY",
    "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY",
    "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY",
    "DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY",
    "DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY",
    "DITTO_CODING_WORKER_PROVIDER_KEY",
]


def _docs(text: str) -> list[dict]:
    return yaml.safe_load(text)


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict]) -> dict:
    (task,) = [t for t in _walk(tasks) if t.get("name") == name]
    return task


def _flat(text: object) -> str:
    return " ".join(str(text).split())


# ─── Structure ────────────────────────────────────────────────────────────────


def test_defaults_are_exactly_the_three_inputs() -> None:
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == INPUTS
    assert (
        yaml.safe_load((CLEANUP_ROLE / "defaults/main.yml").read_text())
        == CLEANUP_INPUTS
    )


def test_main_freezes_the_gate_then_includes_behind_it() -> None:
    tasks = _docs(MAIN)
    assert [t["name"] for t in tasks] == [GATE_FREEZE, DORMANT, INCLUDE]
    freeze = _task(GATE_FREEZE, tasks)
    assert freeze["no_log"] is True
    assert _flat(freeze["ansible.builtin.set_fact"][f"{PREFIX}gate"]) == (
        f"{{{{ ({PREFIX}enabled | default(false, true)) | bool }}}}"
    )
    assert _task(DORMANT, tasks)["when"] == f"not {PREFIX}gate"
    include = _task(INCLUDE, tasks)
    assert include["ansible.builtin.include_tasks"] == "materialize.yml"
    assert include["when"] == f"{PREFIX}gate"
    assert "ansible.builtin.copy" not in MAIN and "import_tasks" not in MAIN
    # Cleanup mirrors the gate freeze.
    ctasks = _docs(CLEANUP_MAIN)
    assert [t["name"] for t in ctasks] == [
        GATE_FREEZE,
        "Explain dormant native worker credential removal",
        "Remove the worker-owned credential files behind the enabled gate",
    ]
    assert ctasks[-1]["ansible.builtin.include_tasks"] == "remove.yml"
    assert ctasks[-1]["when"] == f"{CLEANUP_PREFIX}gate"
    assert ctasks[0]["no_log"] is True


def test_materialize_task_order() -> None:
    assert [t["name"] for t in _docs(MATERIALIZE)] == [
        PRESET,
        ENV_VAR,
        CHECK_MODE,
        FREEZE_GATE,
        GATE,
        IDENTITY,
        HOST,
        CURATOR,
        FREEZE_SECRETS,
        CREDS,
        DISTINCT,
        RENDER,
        ACCOUNTS,
        ACCOUNT_CHECK,
        LISTING,
        LIVE,
        WORKER_PROCS,
        WORKER_PROCS_CHECK,
        DIRECTORIES,
        DIR_CHECK,
        WRITE,
        WRITE_CHECK,
        RELIST,
        LIVE_AFTER,
        REPORT,
    ]


def test_preset_guard_is_one_varnames_equality_including_the_gate() -> None:
    tasks = _docs(MATERIALIZE)
    that = _task(PRESET, tasks)["ansible.builtin.assert"]["that"]
    assert len(that) == 1  # no redundant per-name "is not defined" lines
    assert "'^coding_hosted_worker_credentials_(?!cleanup_)'" in _flat(that[0])
    assert _flat(that[0]).endswith(
        "| sort == ['coding_hosted_worker_credentials_confirmation', "
        "'coding_hosted_worker_credentials_enabled', "
        "'coding_hosted_worker_credentials_gate', "
        "'coding_hosted_worker_credentials_source_revision']"
    )
    # An Ansible variable named like a controller env input is refused too.
    env = _task(ENV_VAR, tasks)["ansible.builtin.assert"]["that"]
    assert "(?i)^(DITTO_CODING_WORKER_|DITTO_CODING_HIPPIUS_)" in _flat(env[0])
    # Cleanup's refusal names only its own prefix.
    cthat = _task(
        "Refuse preset registered results and undocumented role inputs", _docs(REMOVE)
    )["ansible.builtin.assert"]["that"]
    assert f"'^{CLEANUP_PREFIX}'" in _flat(cthat[0])
    assert "(?!cleanup_)" not in _flat(cthat[0])


def test_frozen_captures_defeat_lazy_templating() -> None:
    tasks = _docs(MATERIALIZE)
    freeze = _task(FREEZE_GATE, tasks)
    assert freeze["no_log"] is True
    sf = freeze["ansible.builtin.set_fact"]
    assert _flat(sf[f"{PREFIX}gate_confirmation"]) == (
        f"{{{{ {PREFIX}confirmation | default('', true) }}}}"
    )
    assert _flat(sf[f"{PREFIX}gate_source_revision"]) == (
        f"{{{{ {PREFIX}source_revision | default('', true) }}}}"
    )
    that = _task(GATE, tasks)["ansible.builtin.assert"]["that"]
    assert f"{PREFIX}gate_confirmation == '{CONFIRMATION}'" in that
    assert f"{PREFIX}gate_source_revision | length == 40" in that
    assert any("search('[{][{]|[{][%]|[{][#]')" in _flat(line) for line in that)
    # The secret freeze also defaults each lookup so an undefined-class error
    # becomes '' rather than leaking.
    secrets = _task(FREEZE_SECRETS, tasks)["ansible.builtin.set_fact"][
        f"{PREFIX}secrets"
    ]
    assert all("| default('', true)" in v for v in secrets.values())
    assert set(secrets) == set(ENV_NAMES)


def test_every_secret_touching_task_is_no_log_and_nothing_prints_values() -> None:
    tasks = _docs(MATERIALIZE)
    for name in NO_LOG_TASKS:
        assert _task(name, tasks).get("no_log") is True, name
    # The write is a command whose stdin ansible never echoes; the helper never
    # prints a value, so the task stays visible and is not no_log.
    write = _task(WRITE, tasks)
    assert "no_log" not in write
    argv = write["ansible.builtin.command"]["argv"]
    assert argv[0] == "/usr/bin/python3"
    assert argv[1] == "{{ role_path }}/files/materialize_worker_credentials.py"
    assert _flat(write["ansible.builtin.command"]["stdin"]) == (
        f"{{{{ {{'files': {PREFIX}documents}} | to_json }}}}"
    )
    # No module reads the bytes back, diffs, or shells to a secret store.
    for forbidden in (
        "slurp",
        "ansible.builtin.copy",
        "set -x",
        "gcloud secrets",
        "extra_vars",
    ):
        assert forbidden not in MATERIALIZE, forbidden
    report = _task(REPORT, tasks)["ansible.builtin.debug"]["msg"]
    assert "secret" not in report.lower()
    # Every non-no_log assert has a constant fail_msg (no input interpolation),
    # except the helper checks, which interpolate only the helper's own
    # non-secret receipt error field.
    for task in list(_walk(tasks)) + list(_walk(_docs(REMOVE))):
        assertion = task.get("ansible.builtin.assert")
        if assertion and not task.get("no_log"):
            msg = assertion.get("fail_msg", "")
            if "helper" in task["name"]:
                assert "| from_json).error" in _flat(msg)
            else:
                assert "{{" not in msg, task["name"]


def test_write_and_verify_use_the_symlink_safe_helper() -> None:
    tasks = _docs(MATERIALIZE)
    argv = _task(WRITE, tasks)["ansible.builtin.command"]["argv"]
    assert "--private-dir" in argv and "/var/lib/ditto-coding-hosted/private" in argv
    assert "--owner" in argv and OWNER in argv
    assert "--source-revision" in argv
    check = _task(WRITE_CHECK, tasks)["ansible.builtin.assert"]["that"]
    assert f"{PREFIX}helper.rc == 0" in check
    assert f"({PREFIX}helper.stdout | from_json).ok" in check
    # The helper opens the path with O_NOFOLLOW, writes temps then renames all.
    helper = MATERIALIZE_HELPER.read_text()
    assert "O_NOFOLLOW" in helper and "O_DIRECTORY" in helper
    assert "os.rename(" in helper and "src_dir_fd" in helper
    assert "fchown" in helper and "O_EXCL" in helper
    # It never prints a value or a digest.
    assert "print(json.dumps" in helper
    assert ".hexdigest()" not in helper  # digests compared as bytes, never printed


def test_no_service_is_started_and_liveness_uses_one_shared_definition() -> None:
    # Two systemctl listings in materialize: before and after the write.
    assert PARSED.count("systemctl") == 2
    for forbidden in ("systemd:", "service:", "state: stopped", "state: started"):
        assert forbidden not in PARSED, forbidden
    # The idle regex is defined once, in the shared include, and referenced by
    # every liveness check via include_tasks.
    idle = IDLE_INCLUDE.read_text()
    assert idle.count("inactive|failed") == 1
    assert "inactive|failed" not in MATERIALIZE and "inactive|failed" not in REMOVE
    includes = [
        t
        for t in _walk(_docs(MATERIALIZE))
        if t.get("ansible.builtin.include_tasks") == "assert_units_idle.yml"
    ]
    assert [t["name"] for t in includes] == [LIVE, LIVE_AFTER]
    for t in includes:
        assert "idle_units_listing" in t["vars"] and "idle_units_fail_msg" in t["vars"]
    assert IDLE_INCLUDE.read_text() == CLEANUP_IDLE_INCLUDE.read_text()


def test_worker_uid_process_guard_present_in_both_roles() -> None:
    for text in (MATERIALIZE, REMOVE):
        assert "/proc" in text and "-uid" in text
        assert "user manager" in text or "user session" in text


def test_source_revision_is_bound_into_the_helper_and_report() -> None:
    tasks = _docs(MATERIALIZE)
    argv = _task(WRITE, tasks)["ansible.builtin.command"]["argv"]
    i = argv.index("--source-revision")
    assert argv[i + 1] == f"{{{{ {PREFIX}gate_source_revision }}}}"
    report = _task(REPORT, tasks)["ansible.builtin.debug"]["msg"]
    assert f"source_revision={{{{ {PREFIX}gate_source_revision }}}}" in report


def test_playbooks_and_ci_registration() -> None:
    for playbook, role in (
        (PLAYBOOK, "coding_hosted_worker_credentials"),
        (CLEANUP_PLAYBOOK, "coding_hosted_worker_credentials_cleanup"),
    ):
        (play,) = yaml.safe_load(playbook.read_text())
        assert play["hosts"] == "role_coding_hosted"
        assert play["become"] is True and play["gather_facts"] is False
        assert play["roles"] == [role]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-worker-credentials.yml" in workflow
    assert "playbooks/gcp-coding-hosted-worker-credentials-cleanup.yml" in workflow
    assert "tests/coding-hosted-worker-credentials.yml" in workflow
    this_file = str(Path(__file__).relative_to(ROOT))
    infra = yaml.safe_load(workflow)
    for trigger in ("pull_request", "push"):
        assert this_file in infra[True][trigger]["paths"]
        assert "pyproject.toml" in infra[True][trigger]["paths"]
        assert "uv.lock" in infra[True][trigger]["paths"]
    (step,) = [
        s
        for job in infra["jobs"].values()
        for s in job["steps"]
        if REHEARSAL_GATE in s.get("env", {}) and this_file in s["run"]
    ]
    assert step in infra["jobs"]["ansible"]["steps"]
    # The loader-compatibility test runs for a role-only change.
    platform = (ROOT / ".github/workflows/platform-ci.yml").read_text()
    assert "coding_hosted_worker_credentials" in platform


def test_docs_describe_every_guard() -> None:
    docs = (ROOT / "infra/docs/coding-hosted-worker-credentials-v2.md").read_text()
    for phrase in (
        "`coding_hosted_worker_credentials_*`",
        CONFIRMATION,
        CLEANUP_CONFIRMATION,
        "DITTO_CODING_WORKER_",
        "curator secret key",
        "include_tasks",
        "--start-at-task",
        "finalization",
        "Removal is not revocation",
        "`refreshing`",
        "An empty listing",
        f"`{REHEARSAL_GATE}=1`",
        "dedicated image reader",
        "capped OpenRouter",
        "O_NOFOLLOW",
        "worker UID",
        "ANSIBLE_CONFIG",
    ):
        assert phrase in docs, phrase


# ─── Rehearsal ──────────────────────────────────────────────────────────────

STANDINS = {
    ENV_NAMES[0]: 'hip_reader"acc\\ess1',
    ENV_NAMES[1]: 'reader"sec\\ret2',
    ENV_NAMES[2]: 'hip_curator"acc\\ess3',
    ENV_NAMES[3]: 'hip_evidence"acc\\ess4',
    ENV_NAMES[4]: 'evidence"sec\\ret5',
    ENV_NAMES[5]: 'GOOG"image\\access6',
    ENV_NAMES[6]: 'image"sec\\ret7',
    ENV_NAMES[7]: 'sk-or"prov\\ider8',
}
LOOKUP_STANDIN = "REHEARSAL_LOOKUP_STANDIN"
LOOKUP_VALUE = 'lookup"lea\\k9'
STOPPED_UNITS = (
    "ditto-coding-hosted-worker.service loaded failed failed Worker\n"
    "ditto-coding-custody@0.service loaded inactive dead Custody\n"
)
LIVE_UNITS = {
    "active": "ditto-coding-custody@0.service loaded active running Custody",
    "activating": "ditto-coding-hosted-worker.service loaded activating start Worker",
    "reloading": "ditto-coding-custody@0.service loaded reloading reload Custody",
    "refreshing": "ditto-coding-custody@1.service loaded refreshing refresh-extensions C",  # noqa: E501
    "maintenance": "ditto-coding-custody@2.service loaded maintenance cleaning Custody",
    "unknown_state": "ditto-coding-hosted-worker.service loaded quiescent idle Worker",
    "unparseable": "● ditto-coding-custody@0.service loaded inactive dead Custody",
}
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
PROBED_IDENTITY = {
    "hostname": "ditto-coding-hosted-v2",
    "architecture": "x86_64",
    "distribution": "Debian",
    "distribution_major_version": "13",
}

rehearsal = pytest.mark.skipif(
    os.environ.get(REHEARSAL_GATE) != "1",
    reason=f"set {REHEARSAL_GATE}=1 to run the ansible-core rehearsal",
)


def _account_uid() -> str:
    # The rehearsal accounts must name the current user's real UID so the find
    # (rewritten to printf) and the helper's fchown/getpwnam agree.
    return str(os.getuid())


def _rewrite_owner_and_paths(value):
    if isinstance(value, dict):
        return {
            k: v if k == "ansible.builtin.assert" else _rewrite_owner_and_paths(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_owner_and_paths(v) for v in value]
    if isinstance(value, str) and value == OWNER:
        return "{{ rehearsal_owner }}"
    if isinstance(value, str) and value.startswith("{{ role_path }}/files/"):
        helper = value.split("/files/", 1)[1]
        base = str(ROLE if "materialize" in helper else CLEANUP_ROLE)
        return f"{base}/files/{helper}"
    if isinstance(value, str):
        return value.replace("/var/lib/", "{{ rehearsal_root }}/var/lib/")
    return value


def _rehearse_common(
    tasks: list[dict], *, production_identity: bool, host_task: str
) -> None:
    if not production_identity:
        host = _task(host_task, tasks)["ansible.builtin.assert"]
        probed_prefix = host["that"][1].split(".ansible_facts.")[0]
        rewritten = []
        for line in host["that"]:
            m = re.fullmatch(
                rf"{re.escape(probed_prefix)}\.ansible_facts\.(ansible_\w+) == '[^']+'",
                line,
            )
            rewritten.append(
                f"{probed_prefix}.ansible_facts.{m[1]} == rehearsal_local_identity.ansible_facts.{m[1]}"  # noqa: E501
                if m
                else line
            )
        assert sum(a != b for a, b in zip(host["that"], rewritten, strict=True)) == 4
        host["that"] = rewritten
    accounts = _task(ACCOUNTS, tasks)
    assert accounts.pop("ansible.builtin.getent") == {"database": "passwd"}
    accounts["ansible.builtin.set_fact"] = {"getent_passwd": "{{ rehearsal_accounts }}"}
    for name, var in (
        (LISTING, "rehearsal_units"),
        (RELIST, "rehearsal_units_after | default(rehearsal_units)"),
    ):
        for t in tasks:
            if t.get("name") == name:
                t["ansible.builtin.command"]["argv"] = [
                    "/usr/bin/printf",
                    "%s",
                    "{{ " + var + " }}",
                ]
    _task(WORKER_PROCS, tasks)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_worker_procs | default('') }}",
    ]


def _rehearsal_materialize(*, production_identity: bool = False) -> list[dict]:
    tasks = copy.deepcopy(_docs(MATERIALIZE))
    _rehearse_common(tasks, production_identity=production_identity, host_task=HOST)
    _task(REPORT, tasks)["register"] = "rehearsal_report"
    tasks = _rewrite_owner_and_paths(tasks)
    unrooted = [
        t["name"]
        for t in _walk(tasks)
        if "block" not in t and re.search(r"(?<!\}\})/var/lib/", json.dumps(t))
    ]
    assert unrooted == [ACCOUNT_CHECK], unrooted
    return tasks


def _play(
    tasks: list[dict], rp: str, hosts: str = "all", report_var: str = "rehearsal_report"
) -> dict:
    outcome = "{{ rehearsal_root }}/outcome-{{ rehearsal_pass }}.json"
    return {
        "name": f"Rehearse ({rp})",
        "hosts": hosts,
        "strategy": "free",
        "connection": "local",
        "gather_facts": False,
        "become": False,
        "vars": {
            "ansible_python_interpreter": "{{ ansible_playbook_python }}",
            "rehearsal_pass": rp,
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
                "name": "Rehearse",
                "block": [
                    *tasks,
                    {
                        "name": "Record completion",
                        "ansible.builtin.copy": {
                            "dest": outcome,
                            "content": "{{ {'report': "
                            + report_var
                            + ".msg} | to_json }}",
                        },
                    },
                ],
                "rescue": [
                    {
                        "name": "Record refusal",
                        "ansible.builtin.copy": {
                            "dest": outcome,
                            "content": (
                                "{{ {'task': ansible_failed_task.name, "
                                "'messages': [ansible_failed_result.msg | default('')] + "  # noqa: E501
                                "(ansible_failed_result.results | default([]) "
                                "| selectattr('failed', 'defined') | selectattr('failed') "  # noqa: E501
                                "| map(attribute='msg') | list)} | to_json }}"
                            ),
                        },
                    }
                ],
            },
        ],
    }


def _digests(value: str) -> list[str]:
    raw = value.encode()
    out = []
    for algo in ("md5", "sha1", "sha256", "sha512"):
        out.append(hashlib.new(algo, raw).hexdigest())
    return out


def _leak_free(output: str) -> None:
    secrets = [*STANDINS.values(), LOOKUP_VALUE]
    documents = list(_expected_documents().values())
    for value in secrets:
        for form in (value, json.dumps(value)[1:-1], yaml.safe_dump(value).strip()):
            assert form not in output, form
        for digest in _digests(value):
            assert digest not in output, digest
    for document in documents:
        for digest in _digests(document):
            assert digest not in output, digest


def _expected_documents() -> dict[str, str]:
    hippius = {
        "DITTO_CODING_HIPPIUS_ENDPOINT_URL": "https://s3.hippius.com",
        "DITTO_CODING_HIPPIUS_REGION": "decentralized",
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET": "ditto-subnet-coding-private-input",  # noqa: E501
        "DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET": "ditto-subnet-coding-sealed-evidence",  # noqa: E501
        "DITTO_CODING_HIPPIUS_TIMEOUT_SECONDS": "20",
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": STANDINS[ENV_NAMES[0]],
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY": STANDINS[ENV_NAMES[1]],
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY": STANDINS[ENV_NAMES[2]],
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": STANDINS[ENV_NAMES[3]],
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": STANDINS[ENV_NAMES[4]],
    }
    image = {
        "endpoint_url": "https://storage.googleapis.com",
        "bucket": "ditto-platform-agents-prod",
        "region": "auto",
        "access_key": STANDINS[ENV_NAMES[5]],
        "secret_key": STANDINS[ENV_NAMES[6]],
    }
    return {
        "hippius-environment.json": json.dumps(hippius, sort_keys=True),
        "image-storage.json": json.dumps(image, sort_keys=True),
        "provider-key": STANDINS[ENV_NAMES[7]],
    }


def _make_home(root: Path) -> Path:
    home = root / "var/lib/ditto-coding-hosted"
    (home / "private").mkdir(parents=True)
    home.chmod(0o755)
    (home / "private").chmod(0o700)
    return home / "private"


def _base_vars(root: Path) -> dict:
    return {
        "rehearsal_root": str(root),
        "rehearsal_units": STOPPED_UNITS,
        "rehearsal_owner": pwd.getpwuid(os.getuid()).pw_name,
        "rehearsal_group": grp.getgrgid(os.getgid()).gr_name,
        "rehearsal_accounts": {
            "ditto-coding-hosted": [
                "x",
                _account_uid(),
                _account_uid(),
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
        },
    }


def _mat_host(root: Path) -> dict:
    _make_home(root)
    return {
        f"{PREFIX}enabled": True,
        # main.yml freezes this gate before the include; the rehearsal inlines
        # materialize.yml, so it supplies the frozen gate the preset guard expects.
        f"{PREFIX}gate": True,
        f"{PREFIX}confirmation": CONFIRMATION,
        f"{PREFIX}source_revision": REVISION,
        **_base_vars(root),
    }


def _run(
    tmp_path, name, hosts, plays, *flags, extra_env=None, idle_from=IDLE_INCLUDE
) -> str:
    work = tmp_path / name
    work.mkdir()
    (work / "assert_units_idle.yml").write_text(idle_from.read_text())
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": hosts}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "rehearsal.yml").write_text(yaml.safe_dump(plays, sort_keys=False))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ANSIBLE_") and k not in ENV_NAMES
    }
    env |= {
        "ANSIBLE_HOME": str(work / "h"),
        "ANSIBLE_LOCAL_TEMP": str(work / "t"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "ANSIBLE_CALLBACK_RESULT_FORMAT": "yaml",
        LOOKUP_STANDIN: LOOKUP_VALUE,
        **(extra_env if extra_env is not None else STANDINS),
    }
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-f",
            "8",
            "-i",
            "inventory.yml",
            "--diff",
            "-v",
            *flags,
            "rehearsal.yml",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return completed.stdout + completed.stderr


def _outcome(root: Path, rp: str) -> dict:
    return json.loads((root / f"outcome-{rp}.json").read_text())


def _private(root: Path) -> Path:
    return root / "var/lib/ditto-coding-hosted/private"


def _assert_refused(root, task, rp, *, match_msg=True, files_before=0) -> None:
    outcome = _outcome(root, rp)
    assert outcome.get("task") == task, (root.name, outcome)
    if match_msg:
        expected = _flat(
            _task(task, _docs(MATERIALIZE))["ansible.builtin.assert"]["fail_msg"]
        )
        assert expected in [_flat(m) for m in outcome["messages"]], (root.name, outcome)
    assert len(list(_private(root).iterdir())) == files_before, root.name


def _assert_materialized(root, rp) -> None:
    assert _outcome(root, rp)["report"].startswith(
        "Native Coding worker credentials materialized"
    )
    documents = _expected_documents()
    for name, document in documents.items():
        path = _private(root) / name
        assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink == 1
        assert path.read_text() == document


@rehearsal
def test_rehearsal_materializes_only_when_every_guard_passes(tmp_path) -> None:
    dest_cases = ("dest_symlink", "dest_hardlink", "dest_wrongmode")
    # These run in their own env-mutated subprocess, so keep them out of the
    # first multi-host run where the environment carries valid stand-ins.
    env_cases = (
        "missing_credential",
        "duplicate_credential",
        "bad_prefix",
        "curator_secret",
    )
    names = [
        "materialized",
        "no_units",
        *LIVE_UNITS,
        "confirmation_wrong",
        "revision_newline",
        "lookup_confirmation",
        "erroring_confirmation",
        "preset_units",
        "preset_documents",
        "preset_gate_capture",
        "undocumented_input",
        "credential_as_variable",
        "worker_uid_busy",
        "unit_went_live",
        *dest_cases,
        *env_cases,
    ]
    roots = {n: tmp_path / "hosts" / n for n in names}
    hosts = {n: _mat_host(r) for n, r in roots.items()}
    hosts["no_units"]["rehearsal_units"] = ""
    for n, line in LIVE_UNITS.items():
        hosts[n]["rehearsal_units"] = STOPPED_UNITS + line + "\n"
    hosts["confirmation_wrong"][f"{PREFIX}confirmation"] = CONFIRMATION.lower()
    hosts["revision_newline"][f"{PREFIX}source_revision"] = REVISION + "\n"
    hosts["lookup_confirmation"][f"{PREFIX}confirmation"] = (
        "{{ lookup('env', '" + LOOKUP_STANDIN + "') }}"
    )
    # An undefined-class error template must freeze to '' and refuse, not leak.
    hosts["erroring_confirmation"][f"{PREFIX}confirmation"] = (
        "{{ {}['" + LOOKUP_STANDIN + "'] }}"
    )
    hosts["preset_units"][f"{PREFIX}units"] = {"stdout": "", "stdout_lines": []}
    hosts["preset_documents"][f"{PREFIX}documents"] = {
        "hippius-environment.json": "{}",
        "image-storage.json": "{}",
        "provider-key": "x",
    }
    hosts["preset_gate_capture"][f"{PREFIX}gate_confirmation"] = CONFIRMATION
    hosts["undocumented_input"][f"{PREFIX}image_bucket"] = "attacker"
    hosts["credential_as_variable"][ENV_NAMES[7]] = "attacker"
    hosts["worker_uid_busy"]["rehearsal_worker_procs"] = "x"
    hosts["unit_went_live"]["rehearsal_units_after"] = (
        STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
    )
    for n in dest_cases:
        target = _private(roots[n]) / "hippius-environment.json"
        if n == "dest_symlink":
            target.symlink_to("/etc/hostname")
        elif n == "dest_hardlink":
            other = _private(roots[n]) / "other"
            other.write_text("x")
            os.link(other, target)
        elif n == "dest_wrongmode":
            target.write_text("x")
            target.chmod(0o644)

    first = {n: h for n, h in hosts.items() if n not in env_cases}
    output = _run(tmp_path, "run", first, [_play(_rehearsal_materialize(), "first")])
    _leak_free(output)

    for n in ("materialized", "no_units"):
        _assert_materialized(roots[n], "first")
    for n in LIVE_UNITS:
        _assert_refused(roots[n], IDLE_ASSERT, "first", match_msg=False)
    _assert_refused(roots["confirmation_wrong"], GATE, "first")
    _assert_refused(roots["revision_newline"], GATE, "first")
    _assert_refused(roots["lookup_confirmation"], GATE, "first")
    _assert_refused(roots["erroring_confirmation"], GATE, "first")
    for n in (
        "preset_units",
        "preset_documents",
        "preset_gate_capture",
        "undocumented_input",
    ):
        _assert_refused(roots[n], PRESET, "first")
    _assert_refused(roots["credential_as_variable"], ENV_VAR, "first")
    _assert_refused(roots["worker_uid_busy"], WORKER_PROCS_CHECK, "first")
    # Destinations: the helper refuses; nothing new written, bad file remains.
    for n in dest_cases:
        assert _outcome(roots[n], "first")["task"] == WRITE_CHECK, n
        assert not (_private(roots[n]) / "image-storage.json").exists(), n
    # unit_went_live wrote then failed the post-write recheck.
    assert _outcome(roots["unit_went_live"], "first")["task"] == IDLE_ASSERT
    assert len(list(_private(roots["unit_went_live"]).iterdir())) == 3

    # Env-mutated cases run in their own subprocess with the offending export.
    _run_env_case(
        tmp_path,
        "curator",
        hosts["curator_secret"],
        roots["curator_secret"],
        CURATOR,
        extra={"DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_SECRET_KEY": "leak"},
        match_msg=False,
    )
    _run_env_case(
        tmp_path,
        "missing",
        hosts["missing_credential"],
        roots["missing_credential"],
        CREDS,
        extra={ENV_NAMES[7]: ""},
        match_msg=False,
    )
    _run_env_case(
        tmp_path,
        "dup",
        hosts["duplicate_credential"],
        roots["duplicate_credential"],
        DISTINCT,
        extra={ENV_NAMES[1]: STANDINS[ENV_NAMES[0]]},
        match_msg=False,
    )
    _run_env_case(
        tmp_path,
        "bad",
        hosts["bad_prefix"],
        roots["bad_prefix"],
        CREDS,
        extra={ENV_NAMES[0]: "reader-without-prefix"},
        match_msg=False,
    )


def _run_env_case(tmp_path, name, host, root, task, *, extra, match_msg) -> None:
    output = _run(
        tmp_path,
        name,
        {name: {**host, "rehearsal_root": str(root)}},
        [_play(_rehearsal_materialize(), name, hosts=name)],
        extra_env={**STANDINS, **extra},
    )
    _leak_free(output)
    _assert_refused(root, task, name, match_msg=match_msg)


@rehearsal
def test_rehearsal_refuses_forged_facts_and_start_at_task_and_flip(tmp_path) -> None:
    roots = {n: tmp_path / "hosts" / n for n in ("forged_host", "forged_accounts")}
    hosts = {n: _mat_host(r) for n, r in roots.items()}
    hosts["forged_accounts"]["rehearsal_accounts"] = {
        "ditto-coding-hosted": _base_vars(roots["forged_accounts"])[
            "rehearsal_accounts"
        ]["ditto-coding-hosted"]
    }
    forged = {
        **{f"ansible_{k}": v for k, v in PROBED_IDENTITY.items()},
        "getent_passwd": REHEARSAL_ACCOUNTS,
    }
    output = _run(
        tmp_path,
        "forged",
        hosts,
        [
            _play(
                _rehearsal_materialize(production_identity=True), "facts", "forged_host"
            ),
            _play(_rehearsal_materialize(), "facts", "forged_accounts"),
        ],
        "-e",
        json.dumps({"ansible_facts": forged}),
    )
    _leak_free(output)
    _assert_refused(roots["forged_host"], HOST, "facts")
    _assert_refused(roots["forged_accounts"], ACCOUNT_CHECK, "facts")

    # Gate cases run the real main.yml -> materialize.yml include structure.
    gate_root = tmp_path / "hosts" / "gate"
    _make_home(gate_root)
    materialize_file = tmp_path / "materialize.yml"
    materialize_file.write_text(
        yaml.safe_dump(_rehearsal_materialize(), sort_keys=False)
    )
    base = {
        **_base_vars(gate_root),
        f"{PREFIX}confirmation": CONFIRMATION,
        f"{PREFIX}source_revision": REVISION,
    }
    main_play = {
        "name": "Gate",
        "hosts": "all",
        "connection": "local",
        "gather_facts": False,
        "become": False,
        "vars": {"ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "tasks": [
            {
                "name": GATE_FREEZE,
                "ansible.builtin.set_fact": {
                    f"{PREFIX}gate": f"{{{{ ({PREFIX}enabled | default(false, true)) | bool }}}}"  # noqa: E501
                },
                "no_log": True,
            },
            {
                "name": DORMANT,
                "ansible.builtin.debug": {"msg": "dormant"},
                "when": f"not {PREFIX}gate",
            },
            {
                "name": INCLUDE,
                "ansible.builtin.include_tasks": "materialize.yml",
                "when": f"{PREFIX}gate",
            },
        ],
    }
    flip = {**base, f"{PREFIX}enabled": "{{ item is defined }}"}
    out = _run_gate(tmp_path, "flip", flip, main_play, materialize_file)
    _leak_free(out)
    assert not any(_private(gate_root).iterdir())
    enabled = {**base, f"{PREFIX}enabled": True}
    out = _run_gate(
        tmp_path,
        "startat",
        enabled,
        main_play,
        materialize_file,
        "--start-at-task",
        WRITE,
    )
    _leak_free(out)
    assert not any(_private(gate_root).iterdir())


def _run_gate(tmp_path, name, host_vars, play, materialize_file, *flags) -> str:
    work = tmp_path / f"gate-{name}"
    work.mkdir()
    (work / "materialize.yml").write_text(materialize_file.read_text())
    (work / "assert_units_idle.yml").write_text(IDLE_INCLUDE.read_text())
    (work / "inventory.yml").write_text(
        yaml.safe_dump(
            {
                "all": {
                    "children": {"role_coding_hosted": {"hosts": {"gate": host_vars}}}
                }
            }
        )
    )
    (work / "play.yml").write_text(yaml.safe_dump([play], sort_keys=False))
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("ANSIBLE_") and k not in ENV_NAMES
    }
    env |= {
        "ANSIBLE_HOME": str(work / "h"),
        "ANSIBLE_LOCAL_TEMP": str(work / "t"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "ANSIBLE_CALLBACK_RESULT_FORMAT": "yaml",
        **STANDINS,
    }
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-i",
            "inventory.yml",
            "-v",
            *flags,
            "play.yml",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return completed.stdout + completed.stderr


@rehearsal
def test_rehearsal_removing_no_log_from_render_would_leak(tmp_path) -> None:
    """Stripping no_log from the document render leaks a raw stand-in, so the
    no_log lines are load-bearing and CI catches their removal."""
    tasks = _rehearsal_materialize()
    _task(RENDER, tasks).pop("no_log", None)
    root = tmp_path / "hosts" / "mutated"
    output = _run(
        tmp_path, "mutated", {"mutated": _mat_host(root)}, [_play(tasks, "mut")]
    )
    _assert_materialized(root, "mut")
    assert any(v in output for v in STANDINS.values())


# ─── Cleanup rehearsal ────────────────────────────────────────────────────────

C_PRESET = "Refuse preset registered results and undocumented role inputs"
C_GATE = "Require the exact confirmation and source revision as frozen literals"
C_HOST = "Require the dedicated host"
C_ACCOUNTS = "Inspect host accounts once"
C_LISTING = "List live worker and custody units"
C_RELIST = "Re-list live worker and custody units after removing"
C_WORKER_PROCS = "Require no process is running as the worker UID"
C_REMOVE_CHECK = (
    "Require the helper to have removed or confirmed absent all three files"
)
C_REPORT = "Report only the source revision and which fixed files were removed or already absent"  # noqa: E501


def _rehearsal_remove() -> list[dict]:
    tasks = copy.deepcopy(_docs(REMOVE))
    _rehearse_common_cleanup(tasks)
    _task(C_REPORT, tasks)["register"] = "rehearsal_report"
    tasks = _rewrite_owner_and_paths(tasks)
    return tasks


def _rehearse_common_cleanup(tasks: list[dict]) -> None:
    host = _task(C_HOST, tasks)["ansible.builtin.assert"]
    probed_prefix = host["that"][1].split(".ansible_facts.")[0]
    rewritten = []
    for line in host["that"]:
        m = re.fullmatch(
            rf"{re.escape(probed_prefix)}\.ansible_facts\.(ansible_\w+) == '[^']+'",
            line,
        )
        rewritten.append(
            f"{probed_prefix}.ansible_facts.{m[1]} == rehearsal_local_identity.ansible_facts.{m[1]}"  # noqa: E501
            if m
            else line
        )
    host["that"] = rewritten
    accounts = _task(C_ACCOUNTS, tasks)
    accounts.pop("ansible.builtin.getent")
    accounts["ansible.builtin.set_fact"] = {"getent_passwd": "{{ rehearsal_accounts }}"}
    for name, var in (
        (C_LISTING, "rehearsal_units"),
        (C_RELIST, "rehearsal_units_after | default(rehearsal_units)"),
    ):
        _task(name, tasks)["ansible.builtin.command"]["argv"] = [
            "/usr/bin/printf",
            "%s",
            "{{ " + var + " }}",
        ]
    _task(C_WORKER_PROCS, tasks)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_worker_procs | default('') }}",
    ]


def _cleanup_host(
    root: Path,
    *,
    populate=("hippius-environment.json", "image-storage.json", "provider-key"),
) -> dict:
    private = _make_home(root)
    for name in populate:
        (private / name).write_text("stale")
        (private / name).chmod(0o600)
    return {
        f"{CLEANUP_PREFIX}enabled": True,
        f"{CLEANUP_PREFIX}gate": True,
        f"{CLEANUP_PREFIX}confirmation": CLEANUP_CONFIRMATION,
        f"{CLEANUP_PREFIX}source_revision": REVISION,
        "rehearsal_root": str(root),
        "rehearsal_units": STOPPED_UNITS,
        "rehearsal_owner": pwd.getpwuid(os.getuid()).pw_name,
        "rehearsal_group": grp.getgrgid(os.getgid()).gr_name,
        "rehearsal_accounts": _base_vars(root)["rehearsal_accounts"],
    }


@rehearsal
def test_rehearsal_cleanup_removes_only_when_every_guard_passes(tmp_path) -> None:
    names = [
        "removed",
        "already_absent",
        "revision_newline",
        "preset_units",
        "cleanup_symlink",
        "cleanup_directory",
        "cleanup_hardlink",
        "cleanup_partial",
        "unit_live",
        "unit_went_live_after",
    ]
    roots = {n: tmp_path / "hosts" / n for n in names}
    hosts = {}
    hosts["removed"] = _cleanup_host(roots["removed"])
    hosts["already_absent"] = _cleanup_host(roots["already_absent"], populate=())
    hosts["revision_newline"] = _cleanup_host(roots["revision_newline"])
    hosts["revision_newline"][f"{CLEANUP_PREFIX}source_revision"] = REVISION + "\n"
    hosts["preset_units"] = _cleanup_host(roots["preset_units"])
    hosts["preset_units"][f"{CLEANUP_PREFIX}units"] = {"stdout": "", "stdout_lines": []}
    hosts["cleanup_symlink"] = _cleanup_host(
        roots["cleanup_symlink"], populate=("image-storage.json", "provider-key")
    )
    (_private(roots["cleanup_symlink"]) / "hippius-environment.json").symlink_to(
        "/etc/hostname"
    )
    hosts["cleanup_directory"] = _cleanup_host(
        roots["cleanup_directory"], populate=("image-storage.json", "provider-key")
    )
    (_private(roots["cleanup_directory"]) / "hippius-environment.json").mkdir()
    hosts["cleanup_hardlink"] = _cleanup_host(
        roots["cleanup_hardlink"], populate=("hippius-environment.json", "provider-key")
    )
    other = _private(roots["cleanup_hardlink"]) / "other"
    other.write_text("x")
    os.link(other, _private(roots["cleanup_hardlink"]) / "image-storage.json")
    # Partial: provider-key is a symlink, so the first two unlink then it refuses.
    hosts["cleanup_partial"] = _cleanup_host(
        roots["cleanup_partial"],
        populate=("hippius-environment.json", "image-storage.json"),
    )
    (_private(roots["cleanup_partial"]) / "provider-key").symlink_to("/etc/hostname")
    hosts["unit_live"] = _cleanup_host(roots["unit_live"])
    hosts["unit_live"]["rehearsal_units"] = STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
    hosts["unit_went_live_after"] = _cleanup_host(roots["unit_went_live_after"])
    hosts["unit_went_live_after"]["rehearsal_units_after"] = (
        STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
    )

    output = _run(
        tmp_path,
        "cleanup",
        hosts,
        [_play(_rehearsal_remove(), "c")],
        idle_from=CLEANUP_IDLE_INCLUDE,
    )
    _leak_free(output)

    def outcome(n):
        return _outcome(roots[n], "c")

    assert outcome("removed")["report"].startswith(
        "Native Coding worker credential cleanup"
    )
    assert not any(_private(roots["removed"]).iterdir())
    assert outcome("already_absent")["report"].startswith(
        "Native Coding worker credential cleanup"
    )
    assert outcome("revision_newline")["task"] == C_GATE
    assert outcome("preset_units")["task"] == C_PRESET
    for n in (
        "cleanup_symlink",
        "cleanup_directory",
        "cleanup_hardlink",
        "cleanup_partial",
    ):
        assert outcome(n)["task"] == C_REMOVE_CHECK, n
    assert outcome("unit_live")["task"] == IDLE_ASSERT
    # Files untouched when the live guard refuses before the helper runs.
    assert (_private(roots["unit_live"]) / "provider-key").exists()
    assert outcome("unit_went_live_after")["task"] == IDLE_ASSERT
