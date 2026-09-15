"""Native worker credential materialization is default-off, unforgeable, silent.

The structural tests parse the role and assert its shape. The rehearsal, gated by
DITTO_ANSIBLE_REHEARSAL=1, runs the enabled tasks through ansible-core 2.21.2
against a temporary tree with obvious stand-in credentials and proves the guards
refuse every forged, preset, templated, wrong-state and wrong-metadata input, and
that no stand-in value or its digest ever reaches ansible output.
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
MAIN = (ROLE / "tasks/main.yml").read_text()
MATERIALIZE = (ROLE / "tasks/materialize.yml").read_text()
PARSED = yaml.safe_dump(yaml.safe_load(MATERIALIZE), width=10_000)
PLAYBOOK = ROOT / "infra/ansible/playbooks/gcp-coding-hosted-worker-credentials.yml"
CLEANUP_ROLE = ROOT / "infra/ansible/roles/coding_hosted_worker_credentials_cleanup"
CLEANUP_PLAYBOOK = (
    ROOT / "infra/ansible/playbooks/gcp-coding-hosted-worker-credentials-cleanup.yml"
)

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

PRIVATE = "/var/lib/ditto-coding-hosted/private"
HIPPIUS = f"{PRIVATE}/hippius-environment.json"
IMAGE = f"{PRIVATE}/image-storage.json"
PROVIDER = f"{PRIVATE}/provider-key"
FILES = [HIPPIUS, IMAGE, PROVIDER]
OWNER = "ditto-coding-hosted"

DORMANT = "Explain dormant native worker credential materialization"
INCLUDE = "Materialize the worker-owned credential files behind the enabled gate"
PRESET = "Refuse preset registered results, captures and undocumented role inputs"
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
DIRECTORIES = "Inspect the worker home and private directory without following links"
DIR_CHECK = "Require an existing owner-only private directory below a real worker home"
DESTINATIONS = "Inspect the three exact destinations as link metadata only"
DEST_CHECK = (
    "Refuse anything but an absent destination or a regular single-link owner-only file"
)
WRITE = "Write each worker credential file atomically without printing it"
REINSPECT = "Reinspect each file as ownership, mode and digest metadata only"
SEALED = "Require owner-only regular single-link files"
DIGEST = "Require each file to hold exactly its rendered document"
RELIST = "Re-list live worker and custody units after writing"
LIVE_AFTER = "Refuse if any worker or custody unit went live during materialization"
REPORT = "Report only that the files exist"

NO_LOG_TASKS = (
    FREEZE_GATE,
    CURATOR,
    FREEZE_SECRETS,
    CREDS,
    DISTINCT,
    RENDER,
    WRITE,
    REINSPECT,
    SEALED,
    DIGEST,
)

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


def _documents(text: str) -> list[dict]:
    return yaml.safe_load(text)


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict]) -> dict:
    (task,) = [task for task in _walk(tasks) if task.get("name") == name]
    return task


def _flat(text: str) -> str:
    return " ".join(str(text).split())


# ─── Structure ────────────────────────────────────────────────────────────────


def test_defaults_are_exactly_the_three_inputs() -> None:
    assert yaml.safe_load((ROLE / "defaults/main.yml").read_text()) == INPUTS
    assert (
        yaml.safe_load((CLEANUP_ROLE / "defaults/main.yml").read_text())
        == CLEANUP_INPUTS
    )


def test_main_only_reports_dormant_or_includes_behind_the_gate() -> None:
    tasks = _documents(MAIN)
    assert [task["name"] for task in tasks] == [DORMANT, INCLUDE]
    gate = f"({PREFIX}enabled | default(false, true)) | bool"
    assert _flat(_task(DORMANT, tasks)["when"]) == f"not ({gate})"
    include = _task(INCLUDE, tasks)
    # The enabled branch is a dynamic include, so --start-at-task cannot jump
    # past the guards into it and a per-loop re-template cannot flip the gate.
    assert include["ansible.builtin.include_tasks"] == "materialize.yml"
    assert _flat(include["when"]) == gate
    # main.yml never writes; the write lives only in the included file.
    assert "ansible.builtin.copy" not in MAIN
    assert "import_tasks" not in MAIN  # a static import would defeat --start-at-task
    cleanup = _documents((CLEANUP_ROLE / "tasks/main.yml").read_text())
    assert [task["name"] for task in cleanup] == [
        "Explain dormant native worker credential removal",
        "Remove the worker-owned credential files behind the enabled gate",
    ]
    assert cleanup[1]["ansible.builtin.include_tasks"] == "remove.yml"
    cleanup_gate = f"({CLEANUP_PREFIX}enabled | default(false, true)) | bool"
    assert _flat(cleanup[1]["when"]) == cleanup_gate


def test_materialize_task_order() -> None:
    assert [task["name"] for task in _documents(MATERIALIZE)] == [
        PRESET,
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
        DIRECTORIES,
        DIR_CHECK,
        DESTINATIONS,
        DEST_CHECK,
        WRITE,
        REINSPECT,
        SEALED,
        DIGEST,
        RELIST,
        LIVE_AFTER,
        REPORT,
    ]


def test_preset_guard_lists_every_capture_and_registered_result() -> None:
    tasks = _documents(MATERIALIZE)
    that = _task(PRESET, tasks)["ansible.builtin.assert"]["that"]
    reserved = [
        line.split(" is not defined")[0] for line in that if "is not defined" in line
    ]
    assert reserved == [
        f"{PREFIX}gate_confirmation",
        f"{PREFIX}gate_source_revision",
        f"{PREFIX}secrets",
        f"{PREFIX}documents",
        f"{PREFIX}identity",
        f"{PREFIX}accounts",
        f"{PREFIX}units",
        f"{PREFIX}units_after",
        f"{PREFIX}directories",
        f"{PREFIX}destinations",
        f"{PREFIX}files",
    ]
    # Every role-prefixed name the tasks use is an input, a capture or a
    # registered result: the reserved list plus the inputs is total.
    registered = [task["register"] for task in _walk(tasks) if "register" in task]
    scoped = [
        name
        for task in _walk(tasks)
        for name in task.get("ansible.builtin.set_fact", {})
    ]
    used = set(re.findall(rf"\b{PREFIX}\w+", PARSED))
    assert used <= {*INPUTS, *reserved} and set(registered + scoped) <= set(reserved)
    # The lookahead keeps this refusal from matching the cleanup prefix.
    varnames = [line for line in that if "varnames" in line][0]
    assert "'^coding_hosted_worker_credentials_(?!cleanup_)'" in _flat(varnames)
    assert _flat(varnames).endswith(
        "| sort == ['coding_hosted_worker_credentials_confirmation', "
        "'coding_hosted_worker_credentials_enabled', "
        "'coding_hosted_worker_credentials_source_revision']"
    )
    # The cleanup refusal names its own prefix and never matches the materialize one.
    cleanup_that = _task(
        "Refuse preset registered results and undocumented role inputs",
        _documents((CLEANUP_ROLE / "tasks/remove.yml").read_text()),
    )["ansible.builtin.assert"]["that"]
    assert any(f"'^{CLEANUP_PREFIX}'" in _flat(line) for line in cleanup_that)


def test_frozen_captures_defeat_lazy_templating() -> None:
    tasks = _documents(MATERIALIZE)
    freeze = _task(FREEZE_GATE, tasks)["ansible.builtin.set_fact"]
    assert freeze == {
        f"{PREFIX}gate_confirmation": f"{{{{ {PREFIX}confirmation }}}}",
        f"{PREFIX}gate_source_revision": f"{{{{ {PREFIX}source_revision }}}}",
    }
    assert _task(FREEZE_GATE, tasks)["no_log"] is True
    that = _task(GATE, tasks)["ansible.builtin.assert"]["that"]
    # Validation reads only the frozen captures, never the raw inputs.
    assert f"{PREFIX}gate_confirmation == '{CONFIRMATION}'" in that
    assert f"{PREFIX}gate_source_revision is match('^[0-9a-f]{{40}}$')" in that
    assert f"{PREFIX}gate_source_revision | length == 40" in that
    assert any("search('[{][{]|[{][%]|[{][#]')" in _flat(line) for line in that)
    # A trailing newline satisfies the '$' anchor but fails the exact length.
    assert re.match("^[0-9a-f]{40}$", REVISION + "\n")
    assert len(REVISION + "\n") != 40


def test_every_secret_touching_task_is_no_log() -> None:
    tasks = _documents(MATERIALIZE)
    for name in NO_LOG_TASKS:
        assert _task(name, tasks).get("no_log") is True, name
    write = _task(WRITE, tasks)
    assert write["diff"] is False
    copy_args = write["ansible.builtin.copy"]
    assert copy_args["mode"] == "0600"
    assert copy_args["follow"] is False and copy_args["force"] is True
    assert copy_args["backup"] is False and copy_args["unsafe_writes"] is False
    # Nothing reads the file bytes back or shells out to a secret store.
    for forbidden in ("slurp", "fetch", "set -x", "gcloud secrets", "extra_vars"):
        assert forbidden not in MATERIALIZE
    # The report and every non-no_log fail_msg carry no interpolation.
    report = _task(REPORT, tasks)["ansible.builtin.debug"]["msg"]
    assert "{{" not in report and "secret" not in report.lower()
    for task in _walk(tasks):
        assertion = task.get("ansible.builtin.assert")
        if assertion and not task.get("no_log"):
            assert "{{" not in assertion.get("fail_msg", ""), task["name"]


def test_digest_verification_reads_only_frozen_documents() -> None:
    tasks = _documents(MATERIALIZE)
    stat = _task(REINSPECT, tasks)["ansible.builtin.stat"]
    assert stat["get_checksum"] is True and stat["checksum_algorithm"] == "sha256"
    assert stat["follow"] is False
    digest = _task(DIGEST, tasks)["ansible.builtin.assert"]["that"]
    assert digest == [
        f"item.stat.checksum == {PREFIX}documents[item.item.document] | hash('sha256')"
    ]
    sealed = _task(SEALED, tasks)["ansible.builtin.assert"]["that"]
    assert "not item.stat.islnk" in sealed
    assert "item.stat.nlink == 1" in sealed
    assert "item.stat.mode == '0600'" in sealed


def test_curator_secret_key_is_never_looked_up() -> None:
    render = json.dumps(
        _task(RENDER, _documents(MATERIALIZE))["ansible.builtin.set_fact"]
    )
    assert "CURATOR_ACCESS_KEY" in render and "CURATOR_SECRET" not in render
    curator = _task(CURATOR, _documents(MATERIALIZE))["ansible.builtin.assert"]["that"]
    assert any("CURATOR_SECRET_KEY') == ''" in _flat(line) for line in curator)


def test_no_service_is_started_and_liveness_is_rechecked() -> None:
    tasks = _documents(MATERIALIZE)
    # Exactly two systemctl listings: before the write and after it.
    assert PARSED.count("systemctl") == 2
    for forbidden in ("systemd:", "service:", "state: stopped", "state: started"):
        assert forbidden not in PARSED, forbidden
    names = [task["name"] for task in tasks]
    # The recheck follows the digest verification and precedes the report.
    assert names.index(RELIST) == names.index(DIGEST) + 1
    assert names.index(LIVE_AFTER) == names.index(RELIST) + 1
    assert names.index(REPORT) == names.index(LIVE_AFTER) + 1
    pattern = "(inactive|failed)"
    assert pattern in _flat(_task(LIVE, tasks)["ansible.builtin.assert"]["that"][0])
    assert pattern in _flat(
        _task(LIVE_AFTER, tasks)["ansible.builtin.assert"]["that"][0]
    )


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
    assert "tests/coding-hosted-worker-credentials.yml" in workflow
    assert "playbooks/gcp-coding-hosted-worker-credentials-cleanup.yml" in workflow
    this_file = str(Path(__file__).relative_to(ROOT))
    infra = yaml.safe_load(workflow)
    for trigger in ("pull_request", "push"):
        assert this_file in infra[True][trigger]["paths"]
    (step,) = [
        step
        for job in infra["jobs"].values()
        for step in job["steps"]
        if REHEARSAL_GATE in step.get("env", {}) and this_file in step["run"]
    ]
    assert step in infra["jobs"]["ansible"]["steps"]
    assert step["env"] == {REHEARSAL_GATE: "1"}


def test_docs_describe_every_guard() -> None:
    docs = (ROOT / "infra/docs/coding-hosted-worker-credentials-v2.md").read_text()
    for phrase in (
        "`coding_hosted_worker_credentials_*`",
        "MATERIALIZE NATIVE CODING WORKER CREDENTIALS",
        "REMOVE NATIVE CODING WORKER CREDENTIALS",
        "DITTO_CODING_WORKER_",
        "curator secret key",
        "include_tasks",
        "--start-at-task",
        "finalization error",
        "Removal is not revocation",
        "`refreshing`",
        "An empty listing",
        f"`{REHEARSAL_GATE}=1`",
        "dedicated image reader",
        "capped OpenRouter",
    ):
        assert phrase in docs, phrase


# ─── Rehearsal ──────────────────────────────────────────────────────────────

# Obvious stand-ins carrying " and \ so the leak search covers raw, JSON- and
# YAML-escaped forms. Never a real credential.
STANDINS = {
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY": 'hip_reader"acc\\ess1',  # noqa: E501
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY": 'reader"sec\\ret2',
    "DITTO_CODING_WORKER_HIPPIUS_PRIVATE_INPUT_CURATOR_ACCESS_KEY": 'hip_curator"acc\\ess3',  # noqa: E501
    "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY": 'hip_evidence"acc\\ess4',  # noqa: E501
    "DITTO_CODING_WORKER_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY": 'evidence"sec\\ret5',
    "DITTO_CODING_WORKER_IMAGE_STORAGE_ACCESS_KEY": 'GOOG"image\\access6',
    "DITTO_CODING_WORKER_IMAGE_STORAGE_SECRET_KEY": 'image"sec\\ret7',
    "DITTO_CODING_WORKER_PROVIDER_KEY": 'sk-or"prov\\ider8',
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


def _rehearsal_materialize(*, production_identity: bool = False) -> list[dict]:
    """The role's materialize.yml, rewritten to run against a temporary tree."""
    tasks = copy.deepcopy(_documents(MATERIALIZE))
    if not production_identity:
        host = _task(HOST, tasks)["ansible.builtin.assert"]
        probed = f"{PREFIX}identity.ansible_facts."
        rewritten = []
        for line in host["that"]:
            match = re.fullmatch(rf"{re.escape(probed)}(ansible_\w+) == '[^']+'", line)
            rewritten.append(
                f"{probed}{match[1]} == rehearsal_local_identity.ansible_facts.{match[1]}"  # noqa: E501
                if match
                else line
            )
        assert sum(a != b for a, b in zip(host["that"], rewritten, strict=True)) == 4
        host["that"] = rewritten
    accounts = _task(ACCOUNTS, tasks)
    assert accounts.pop("ansible.builtin.getent") == {"database": "passwd"}
    accounts["ansible.builtin.set_fact"] = {"getent_passwd": "{{ rehearsal_accounts }}"}
    _task(LISTING, tasks)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_units }}",
    ]
    _task(RELIST, tasks)["ansible.builtin.command"]["argv"] = [
        "/usr/bin/printf",
        "%s",
        "{{ rehearsal_units_after | default(rehearsal_units) }}",
    ]
    _task(REPORT, tasks)["register"] = "rehearsal_report"

    def rewrite(value):
        if isinstance(value, dict):
            return {
                key: item if key == "ansible.builtin.assert" else rewrite(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, str) and value == OWNER:
            return "{{ rehearsal_owner }}"
        if isinstance(value, str):
            return value.replace("/var/lib/", "{{ rehearsal_root }}/var/lib/")
        return value

    tasks = rewrite(tasks)
    # Only the account-home literals in the account check keep /var/lib/.
    unrooted = [
        task["name"]
        for task in _walk(tasks)
        if "block" not in task and re.search(r"(?<!\}\})/var/lib/", json.dumps(task))
    ]
    assert unrooted == [ACCOUNT_CHECK]
    rendered = json.dumps(tasks)
    assert "systemctl" not in rendered
    assert f'"{OWNER}"' not in rendered
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
                            "content": "{{ {'report': rehearsal_report.msg} | to_json }}",  # noqa: E501
                        },
                    },
                ],
                "rescue": [
                    {
                        # Records only the failing task's name, never its result:
                        # a no_log task's result is censored, and a non-no_log
                        # assert keeps its static fail_msg here.
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


def _run(tmp_path, name, hosts, plays, *flags) -> str:
    work = tmp_path / name
    work.mkdir()
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": hosts}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "rehearsal.yml").write_text(yaml.safe_dump(plays, sort_keys=False))
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANSIBLE_") and key not in ENV_NAMES
    }
    environment |= {
        "ANSIBLE_HOME": str(work / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(work / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        # The repo callback: yaml-formatted results, which is where an unguarded
        # stat checksum would surface.
        "ANSIBLE_STDOUT_CALLBACK": "default",
        "ANSIBLE_CALLBACK_RESULT_FORMAT": "yaml",
        LOOKUP_STANDIN: LOOKUP_VALUE,
        **STANDINS,
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
            "--diff",
            "-v",
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
    return completed.stdout + completed.stderr


def _leak_free(output: str) -> None:
    haystack = output
    for value in [*STANDINS.values(), LOOKUP_VALUE]:
        for form in (
            value,
            json.dumps(value)[1:-1],
            yaml.safe_dump(value).strip(),
            hashlib.sha256(value.encode()).hexdigest(),
        ):
            assert form not in haystack, form
    # The document digests are also secret-derived and must never surface.
    for document in _expected_documents().values():
        assert hashlib.sha256(document.encode()).hexdigest() not in haystack


def _expected_documents() -> dict[str, str]:
    render = _task(RENDER, _documents(MATERIALIZE))["ansible.builtin.set_fact"][
        f"{PREFIX}documents"
    ]
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
    assert "sort_keys=True" in render["hippius"] and "sort_keys=True" in render["image"]
    return {
        "hippius": json.dumps(hippius, sort_keys=True),
        "image": json.dumps(image, sort_keys=True),
        "provider": STANDINS[ENV_NAMES[7]],
    }


def _hosts(roots: dict[str, Path]) -> dict[str, dict]:
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    for root in roots.values():
        home = root / "var/lib/ditto-coding-hosted"
        (home / "private").mkdir(parents=True)
        home.chmod(0o755)
        (home / "private").chmod(0o700)
    return {
        name: {
            f"{PREFIX}enabled": True,
            f"{PREFIX}confirmation": CONFIRMATION,
            f"{PREFIX}source_revision": REVISION,
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


def _assert_refused(
    root: Path, task: str, rehearsal_pass: str, *, match_msg: bool = True
) -> None:
    outcome = _outcome(root, rehearsal_pass)
    assert outcome.get("task") == task, (root.name, outcome)
    if match_msg:
        expected = _flat(
            _task(task, _documents(MATERIALIZE))["ansible.builtin.assert"]["fail_msg"]
        )
        assert expected in [_flat(item) for item in outcome["messages"]], (
            root.name,
            outcome,
        )
    # No file was written under the private directory before the refusal.
    private = root / "var/lib/ditto-coding-hosted/private"
    assert not any(private.iterdir()), root.name


def _assert_materialized(root: Path, rehearsal_pass: str) -> None:
    assert _outcome(root, rehearsal_pass) == {
        "report": _flat(
            _task(REPORT, _documents(MATERIALIZE))["ansible.builtin.debug"]["msg"]
        )
    }
    documents = _expected_documents()
    private = root / "var/lib/ditto-coding-hosted/private"
    for name, document in (
        ("hippius-environment.json", documents["hippius"]),
        ("image-storage.json", documents["image"]),
        ("provider-key", documents["provider"]),
    ):
        path = private / name
        assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink == 1
        assert path.read_text() == document


@rehearsal
def test_rehearsal_materializes_only_when_every_guard_passes(tmp_path) -> None:
    dest_cases = ("dest_symlink", "dest_hardlink", "dest_wrongmode", "dest_directory")
    names = [
        "materialized",
        "no_units",
        *LIVE_UNITS,
        "confirmation_wrong",
        "revision_newline",
        "lookup_confirmation",
        "preset_units",
        "preset_documents",
        "preset_capture",
        "undocumented_input",
        "credential_as_variable",
        "missing_credential",
        "duplicate_credential",
        "bad_prefix",
        "unit_went_live",
        *dest_cases,
    ]
    roots = {name: tmp_path / "hosts" / name for name in names}
    hosts = _hosts(roots)
    # Pre-place a bad hippius destination for each dest case; the guard must
    # refuse before writing, and never follow or replace it.
    for name in dest_cases:
        private = roots[name] / "var/lib/ditto-coding-hosted/private"
        target = private / "hippius-environment.json"
        if name == "dest_symlink":
            target.symlink_to("/etc/hostname")
        elif name == "dest_hardlink":
            other = private / "other-name"
            other.write_text("x")
            other.chmod(0o600)
            os.link(other, target)
        elif name == "dest_wrongmode":
            target.write_text("x")
            target.chmod(0o644)
        elif name == "dest_directory":
            target.mkdir()
    hosts["no_units"]["rehearsal_units"] = ""
    for name, line in LIVE_UNITS.items():
        hosts[name]["rehearsal_units"] = STOPPED_UNITS + line + "\n"
    hosts["confirmation_wrong"][f"{PREFIX}confirmation"] = (
        "materialize native coding worker credentials"
    )
    hosts["revision_newline"][f"{PREFIX}source_revision"] = REVISION + "\n"
    hosts["lookup_confirmation"][f"{PREFIX}confirmation"] = (
        "{{ lookup('env', '" + LOOKUP_STANDIN + "') }}"
    )
    hosts["preset_units"][f"{PREFIX}units"] = {"stdout": "", "stdout_lines": []}
    hosts["preset_documents"][f"{PREFIX}documents"] = {
        "hippius": "{}",
        "image": "{}",
        "provider": "x",
    }
    hosts["preset_capture"][f"{PREFIX}gate_confirmation"] = CONFIRMATION
    hosts["undocumented_input"][f"{PREFIX}image_bucket"] = "attacker"
    hosts["credential_as_variable"][f"{PREFIX}provider_key"] = "attacker"
    hosts["unit_went_live"]["rehearsal_units_after"] = (
        STOPPED_UNITS + LIVE_UNITS["active"] + "\n"
    )

    # The credential-shaped cases run separately with a mutated environment; keep
    # their fresh roots out of this run.
    env_cases = ("missing_credential", "duplicate_credential", "bad_prefix")
    first_hosts = {name: host for name, host in hosts.items() if name not in env_cases}
    output = _run(
        tmp_path, "run", first_hosts, [_play(_rehearsal_materialize(), "first")]
    )
    _leak_free(output)

    for name in ("materialized", "no_units"):
        _assert_materialized(roots[name], "first")
    for name in LIVE_UNITS:
        _assert_refused(roots[name], LIVE, "first")
    _assert_refused(roots["confirmation_wrong"], GATE, "first")
    _assert_refused(roots["revision_newline"], GATE, "first")
    _assert_refused(roots["lookup_confirmation"], GATE, "first")
    for name in (
        "preset_units",
        "preset_documents",
        "preset_capture",
        "undocumented_input",
        "credential_as_variable",
    ):
        _assert_refused(roots[name], PRESET, "first")
    # Each bad destination is refused before the write; image and provider are
    # never created, and the bad hippius entry is left untouched.
    for name in dest_cases:
        assert _outcome(roots[name], "first")["task"] == DEST_CHECK, name
        private = roots[name] / "var/lib/ditto-coding-hosted/private"
        assert not (private / "image-storage.json").exists(), name
        assert not (private / "provider-key").exists(), name

    # unit_went_live wrote the files then failed the post-write recheck loudly.
    assert _outcome(roots["unit_went_live"], "first")["task"] == LIVE_AFTER
    private = roots["unit_went_live"] / "var/lib/ditto-coding-hosted/private"
    assert len(list(private.iterdir())) == 3

    # Missing, duplicate and wrong-prefix credentials refuse under no_log, so the
    # outcome names the task but the censored result carries no fail_msg.
    missing = {ENV_NAMES[7]: ""}
    _run_env_case(
        tmp_path,
        "missing",
        hosts["missing_credential"],
        roots["missing_credential"],
        CREDS,
        override=missing,
    )
    dup = {ENV_NAMES[1]: STANDINS[ENV_NAMES[0]]}
    _run_env_case(
        tmp_path,
        "dup",
        hosts["duplicate_credential"],
        roots["duplicate_credential"],
        DISTINCT,
        override=dup,
    )
    bad = {ENV_NAMES[0]: "reader-without-prefix"}
    _run_env_case(
        tmp_path, "bad", hosts["bad_prefix"], roots["bad_prefix"], CREDS, override=bad
    )


def _run_env_case(tmp_path, name, host, root, task, *, override) -> None:
    play = _play(_rehearsal_materialize(), name, hosts=name)
    work = tmp_path / name
    work.mkdir()
    inventory = {"all": {"children": {"role_coding_hosted": {"hosts": {name: host}}}}}
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "rehearsal.yml").write_text(yaml.safe_dump([play], sort_keys=False))
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANSIBLE_") and key not in ENV_NAMES
    }
    environment |= {
        "ANSIBLE_HOME": str(work / "h"),
        "ANSIBLE_LOCAL_TEMP": str(work / "t"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "ANSIBLE_CALLBACK_RESULT_FORMAT": "yaml",
        **STANDINS,
        **override,
    }
    completed = subprocess.run(
        [
            "uvx",
            "--from",
            "ansible-core==2.21.2",
            "ansible-playbook",
            "-i",
            "inventory.yml",
            "--diff",
            "-v",
            "rehearsal.yml",
        ],
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = completed.stdout + completed.stderr
    _leak_free(output)
    _assert_refused(root, task, name, match_msg=False)


@rehearsal
def test_rehearsal_refuses_forged_facts_and_the_start_at_task_and_flip_bypasses(
    tmp_path,
) -> None:
    roots = {
        name: tmp_path / "hosts" / name for name in ("forged_host", "forged_accounts")
    }
    hosts = _hosts(roots)
    forged_facts = {
        **{f"ansible_{key}": value for key, value in PROBED_IDENTITY.items()},
        "getent_passwd": REHEARSAL_ACCOUNTS,
    }
    hosts["forged_accounts"]["rehearsal_accounts"] = {
        "ditto-coding-hosted": REHEARSAL_ACCOUNTS["ditto-coding-hosted"]
    }
    output = _run(
        tmp_path,
        "forged",
        {name: hosts[name] for name in ("forged_host", "forged_accounts")},
        [
            _play(
                _rehearsal_materialize(production_identity=True), "facts", "forged_host"
            ),
            _play(_rehearsal_materialize(), "facts", "forged_accounts"),
        ],
        "-e",
        json.dumps({"ansible_facts": forged_facts}),
    )
    _leak_free(output)
    _assert_refused(roots["forged_host"], HOST, "facts")
    _assert_refused(roots["forged_accounts"], ACCOUNT_CHECK, "facts")

    # The gate cases run the real main.yml -> materialize.yml include, so the
    # include gate and --start-at-task behaviour match the shipped role.
    gate_root = tmp_path / "hosts" / "gate"
    (gate_root / "var/lib/ditto-coding-hosted/private").mkdir(parents=True)
    (gate_root / "var/lib/ditto-coding-hosted").chmod(0o755)
    (gate_root / "var/lib/ditto-coding-hosted/private").chmod(0o700)
    materialize_file = tmp_path / "gate" / "materialize.yml"
    (tmp_path / "gate").mkdir()
    materialize_file.write_text(
        yaml.safe_dump(_rehearsal_materialize(), sort_keys=False)
    )
    base = {
        "rehearsal_root": str(gate_root),
        "rehearsal_units": STOPPED_UNITS,
        "rehearsal_owner": pwd.getpwuid(os.getuid()).pw_name,
        "rehearsal_group": grp.getgrgid(os.getgid()).gr_name,
        "rehearsal_accounts": REHEARSAL_ACCOUNTS,
        f"{PREFIX}confirmation": CONFIRMATION,
        f"{PREFIX}source_revision": REVISION,
    }
    main_play = {
        "name": "Rehearse the gate",
        "hosts": "all",
        "connection": "local",
        "gather_facts": False,
        "become": False,
        "vars": {"ansible_python_interpreter": "{{ ansible_playbook_python }}"},
        "tasks": [
            {
                "name": DORMANT,
                "ansible.builtin.debug": {"msg": "dormant"},
                "when": f"not (({PREFIX}enabled | default(false, true)) | bool)",
            },
            {
                "name": INCLUDE,
                "ansible.builtin.include_tasks": "materialize.yml",
                "when": f"({PREFIX}enabled | default(false, true)) | bool",
            },
        ],
    }
    # Flip: a per-loop re-template evaluates false at the top-level gate.
    flip = {**base, f"{PREFIX}enabled": "{{ item is defined }}"}
    output = _run_gate(tmp_path, "flip", flip, main_play, materialize_file)
    _leak_free(output)
    assert not any((gate_root / "var/lib/ditto-coding-hosted/private").iterdir())

    # --start-at-task on the write cannot reach a dynamically included task.
    enabled = {**base, f"{PREFIX}enabled": True}
    output = _run_gate(
        tmp_path,
        "startat",
        enabled,
        main_play,
        materialize_file,
        "--start-at-task",
        WRITE,
    )
    _leak_free(output)
    assert not any((gate_root / "var/lib/ditto-coding-hosted/private").iterdir())


def _run_gate(tmp_path, name, host_vars, play, materialize_file, *flags) -> str:
    work = tmp_path / f"gate-{name}"
    work.mkdir()
    (work / "materialize.yml").write_text(materialize_file.read_text())
    inventory = {
        "all": {"children": {"role_coding_hosted": {"hosts": {"gate": host_vars}}}}
    }
    (work / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (work / "play.yml").write_text(yaml.safe_dump([play], sort_keys=False))
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANSIBLE_") and key not in ENV_NAMES
    }
    environment |= {
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
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return completed.stdout + completed.stderr


@rehearsal
def test_rehearsal_removing_no_log_from_verification_would_leak(tmp_path) -> None:
    """A meta-check: stripping no_log from the write/verify tasks leaks a digest,
    so the no_log lines are load-bearing and CI catches their removal."""
    tasks = _rehearsal_materialize()
    for name in (WRITE, REINSPECT, SEALED, DIGEST):
        _task(name, tasks).pop("no_log", None)
    root = tmp_path / "hosts" / "mutated"
    hosts = _hosts({"mutated": root})
    output = _run(tmp_path, "mutated", hosts, [_play(tasks, "mut")])
    _assert_materialized(root, "mut")
    # With no_log removed, at least one document digest now appears in output.
    digests = [
        hashlib.sha256(document.encode()).hexdigest()
        for document in _expected_documents().values()
    ]
    assert any(digest in output for digest in digests)
