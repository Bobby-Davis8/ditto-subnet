"""Native PostgreSQL environment materialization is default-off, unforgeable, silent."""

import re
from collections.abc import Iterator
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment"
TASKS = (ROLE / "tasks/main.yml").read_text()
# Parsed tasks without YAML comments, for token scans.
PARSED = yaml.safe_dump(yaml.safe_load(TASKS), width=10_000)
PLAYBOOK = ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment.yml"

PASSWORD_LOOKUP = "lookup('env', 'DITTO_CODING_PG_PASSWORD')"
PREFIX = "coding_hosted_postgres_environment_"
INPUTS = {
    f"{PREFIX}enabled": False,
    f"{PREFIX}confirmation": "",
    f"{PREFIX}source_revision": "",
    f"{PREFIX}host": "",
}
CONFIRMATION = "MATERIALIZE NATIVE CODING POSTGRES ENVIRONMENT"
REVISION = "0123456789abcdef0123456789abcdef01234567"
DATABASE_HOST = "10.30.0.5"

CUSTODY = "/var/lib/ditto-coding-custody/private/postgres-environment.json"
HOSTED = "/var/lib/ditto-coding-hosted/private/postgres-environment.json"
COPIES = [
    {"path": CUSTODY, "owner": "ditto-coding-custody"},
    {"path": HOSTED, "owner": "ditto-coding-hosted"},
]
OWNERS = ("ditto-coding-custody", "ditto-coding-hosted")

EXPLAIN = "Explain dormant native PostgreSQL environment materialization"
PASSWORD_VARIABLE = "Refuse a password supplied as an Ansible variable"
PRESET = "Refuse preset registered results and undocumented role inputs"
IDENTITY = "Probe this machine's identity into a result extra vars cannot preset"
HOST = "Require the exact host, source, database address and confirmation"
PASSWORD = "Require one bounded single-line password from the controller environment"
ACCOUNTS = "Inspect host accounts once"
ACCOUNT_CHECK = "Require the distinct worker and custodian identities"
LISTING = "List live worker and custody units"
LIVE = "Refuse to replace credentials unless every listed unit is inactive or failed"
DIRECTORIES = "Create owner-only credential directories for each reader"
WRITE = "Write each reader's own PostgreSQL environment copy without printing it"
REINSPECT = "Reinspect both copies as ownership, mode and digest metadata only"
SEALED = "Require owner-only regular single-link copies"
DIGEST = "Require both copies to hold exactly the rendered document"
REPORT = "Report only that the copies exist"


def _documents(text: str = TASKS) -> list[dict]:
    return yaml.safe_load(text)


def _guards() -> list[dict]:
    return _documents()[0]["block"]


def _block() -> list[dict]:
    return _documents()[1]["block"]


def _walk(tasks: list[dict]) -> Iterator[dict]:
    for task in tasks:
        yield task
        for section in ("block", "rescue", "always"):
            yield from _walk(task.get(section, []))


def _task(name: str, tasks: list[dict] | None = None) -> dict:
    (task,) = [task for task in _walk(tasks or _documents()) if task["name"] == name]
    return task


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _registered(documents: list[dict]) -> list[str]:
    return [task["register"] for task in _walk(documents) if "register" in task]


def _scoped(documents: list[dict]) -> list[str]:
    return [name for task in _walk(documents) for name in task.get("vars", {})]


def test_default_off_and_password_only_from_the_controller_environment() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == INPUTS
    assert CONFIRMATION in TASKS
    guards, outer = _documents()
    # The refusals sit in their own top-level block, outside the document scope.
    assert set(guards) == {"name", "block"}
    assert [task["name"] for task in guards["block"]] == [
        EXPLAIN,
        PASSWORD_VARIABLE,
        PRESET,
    ]
    enabled = f"{PREFIX}enabled | bool"
    assert _task(EXPLAIN)["when"] == f"not ({enabled})"
    assert _task(PASSWORD_VARIABLE)["when"] == enabled
    assert _task(PRESET)["when"] == enabled
    assert outer["when"] == enabled
    assert [task["name"] for task in _block()] == [
        IDENTITY,
        HOST,
        PASSWORD,
        ACCOUNTS,
        ACCOUNT_CHECK,
        LISTING,
        LIVE,
        DIRECTORIES,
        WRITE,
        REINSPECT,
        SEALED,
        DIGEST,
        REPORT,
    ]
    # No variable can carry the password; one supplied with -e is refused first.
    assert _task(PASSWORD_VARIABLE)["ansible.builtin.assert"]["that"] == [
        f"{PREFIX}password is not defined"
    ]
    assert set(outer["vars"]) == {f"{PREFIX}entries", f"{PREFIX}document"}
    assert not any("PASSWORD" in entry for entry in outer["vars"][f"{PREFIX}entries"])
    assert PASSWORD_LOOKUP in outer["vars"][f"{PREFIX}document"]
    assert f"{PREFIX}password:" not in TASKS


def test_preset_results_and_document_variables_are_refused_outside_their_scope() -> (
    None
):
    documents = _documents()
    guards = _guards()
    # Nothing is registered and no variable is scoped before the refusals, so
    # every role-prefixed name they see came from defaults or an outside source.
    assert not any("register" in task or "vars" in task for task in _walk(guards))
    assert "vars" not in documents[0]
    registered = _registered(documents)
    assert registered == [
        f"{PREFIX}identity",
        f"{PREFIX}accounts",
        f"{PREFIX}units",
        f"{PREFIX}files",
    ]
    scoped = _scoped(documents)
    assert scoped == [f"{PREFIX}entries", f"{PREFIX}document"]
    that = _task(PRESET)["ansible.builtin.assert"]["that"]
    assert that[:-1] == [f"{name} is not defined" for name in registered + scoped]
    assert _flat(that[-1]) == _flat(
        f"lookup('ansible.builtin.varnames', '^{PREFIX}', wantlist=True) | sort == "
        + "["
        + ", ".join(f"'{name}'" for name in sorted(INPUTS))
        + "]"
    )
    # Every role-prefixed name the tasks use is an input, a registered result, a
    # scoped document variable or the refused password: the list above is total.
    used = set(re.findall(rf"\b{PREFIX}\w+", PARSED))
    assert used == {*INPUTS, *registered, *scoped, f"{PREFIX}password"}
    message = _task(PRESET)["ansible.builtin.assert"]["fail_msg"]
    assert "Nothing was written" in message and "{{" not in message
    assert "{{" not in _task(PASSWORD_VARIABLE)["ansible.builtin.assert"]["fail_msg"]


PROBED_IDENTITY = {
    "hostname": "ditto-coding-hosted-v2",
    "architecture": "x86_64",
    "distribution": "Debian",
    "distribution_major_version": "13",
}


def test_identity_comes_only_from_registered_probes_and_no_facts_are_gathered() -> None:
    identity = _task(IDENTITY)
    assert identity["ansible.builtin.setup"] == {
        "gather_subset": ["!all", "!min", "platform", "distribution"]
    }
    assert identity["register"] == f"{PREFIX}identity"
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    assert that[:5] == [
        "inventory_hostname in groups.get('role_coding_hosted', [])",
        *(
            f"{PREFIX}identity.ansible_facts.ansible_{key} == '{value}'"
            for key, value in PROBED_IDENTITY.items()
        ),
    ]
    accounts = _task(ACCOUNTS)
    assert accounts["ansible.builtin.getent"] == {"database": "passwd"}
    assert accounts["register"] == f"{PREFIX}accounts"
    lines = _task(ACCOUNT_CHECK)["ansible.builtin.assert"]["that"]
    assert len(lines) == 5
    assert all(
        line.startswith(f"{PREFIX}accounts.ansible_facts.getent_passwd")
        for line in lines
    )
    # A -e ansible_facts value replaces gathered facts, so no guard reads them.
    assert PARSED.count("ansible_facts") == (
        PARSED.count(f"{PREFIX}identity.ansible_facts")
        + PARSED.count(f"{PREFIX}accounts.ansible_facts")
    )
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["gather_facts"] is False


def test_revision_and_database_host_refuse_a_trailing_newline() -> None:
    that = _task(HOST)["ansible.builtin.assert"]["that"]
    host_pattern = "^10[.]30[.]0[.]([2-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-3])$"
    assert that[5:] == [
        f"{PREFIX}source_revision is string",
        f"{PREFIX}source_revision is match('^[0-9a-f]{{40}}$')",
        f"{PREFIX}source_revision | length == 40",
        f"{PREFIX}confirmation == '{CONFIRMATION}'",
        f"{PREFIX}host is string",
        f"{PREFIX}host == {PREFIX}host | trim",
        f"{PREFIX}host is match('{host_pattern}')",
    ]
    # Ansible's match test is re.match, whose '$' also accepts one trailing
    # newline; the exact length and the trim comparison are what refuse it.
    assert re.match("^[0-9a-f]{40}$", REVISION + "\n")
    assert len(REVISION + "\n") != 40
    assert re.match(host_pattern, DATABASE_HOST + "\n")
    assert (DATABASE_HOST + "\n").strip() != DATABASE_HOST + "\n"


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
UNIT_PATTERN = (
    "^(ditto-coding-hosted-worker[.]service|ditto-coding-custody@\\\\S+[.]service)"
    "\\\\s+\\\\S+\\\\s+(inactive|failed)(\\\\s|$)"
)


def test_live_unit_refusal_is_an_allow_list() -> None:
    names = [task["name"] for task in _block()]
    assert names.index(LIVE) == names.index(LISTING) + 1
    listing = _task(LISTING)
    assert listing["ansible.builtin.command"]["argv"] == [
        "/usr/bin/systemctl",
        "list-units",
        "--all",
        "--plain",
        "--no-legend",
        "--full",
        "ditto-coding-hosted-worker.service",
        "ditto-coding-custody@*.service",
    ]
    assert listing["register"] == f"{PREFIX}units"
    assert listing["check_mode"] is False and listing["changed_when"] is False
    (refuse,) = _task(LIVE)["ansible.builtin.assert"]["that"]
    assert _flat(refuse) == _flat(
        f"{PREFIX}units.stdout_lines | reject('match', '{UNIT_PATTERN}') "
        "| list | length == 0"
    )
    # Jinja decodes the doubled backslashes; Ansible's match test is re.match.
    regex = re.compile(UNIT_PATTERN.replace("\\\\", "\\"))
    for line in ALLOWED_UNIT_LINES:
        assert regex.match(line), line
    for line in REFUSED_UNIT_LINES:
        assert not regex.match(line), line
    # The listing is the only systemctl use; nothing is stopped or started.
    assert PARSED.count("systemctl") == 1
    for forbidden in ("systemd:", "service:", "state: stopped", "state: started"):
        assert forbidden not in PARSED, forbidden


def test_credentials_are_never_logged_read_back_or_diffed() -> None:
    write = _task(WRITE)
    assert write["no_log"] is True and write["diff"] is False
    for forbidden in (
        "slurp",
        "fetch",
        "set -x",
        "gcloud secrets",
        "extra_vars",
    ):
        assert forbidden not in TASKS
    report = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert "password" not in report.lower()
    assert "{{" not in report
    secret_uses = [
        task["name"]
        for task in _walk(_documents())
        if "block" not in task
        and (
            PASSWORD_LOOKUP in yaml.safe_dump(task, width=10_000)
            or f"{PREFIX}document" in yaml.safe_dump(task)
            or f"{PREFIX}files" in yaml.safe_dump(task)
        )
    ]
    # The preset guard names these variables only to require them undefined.
    assert secret_uses == [PRESET, PASSWORD, WRITE, REINSPECT, SEALED, DIGEST]
    for name in secret_uses[1:]:
        task = _task(name)
        # The owner/mode assert prints only the path label and never the digest.
        if name == SEALED:
            assert "checksum" not in yaml.safe_dump(task)
            assert task["loop_control"]["label"] == "{{ item.item.path }}"
            continue
        assert task.get("no_log") is True, name


def test_copies_are_verified_by_owner_mode_and_digest_only() -> None:
    stat = _task(REINSPECT)
    assert stat["ansible.builtin.stat"] == {
        "path": "{{ item.path }}",
        "follow": False,
        "get_checksum": True,
        "checksum_algorithm": "sha256",
        "get_mime": False,
    }
    # The inspected paths and owners are exactly the written ones.
    assert stat["loop"] == _task(WRITE)["loop"] == COPIES
    assert stat["register"] == f"{PREFIX}files"
    digest = _task(DIGEST)
    assert digest["ansible.builtin.assert"]["that"] == [
        f"item.stat.checksum == {PREFIX}document | hash('sha256')"
    ]
    assert digest["loop"] == f"{{{{ {PREFIX}files.results }}}}"
    sealed = _task(SEALED)
    assert sealed["loop"] == f"{{{{ {PREFIX}files.results }}}}"
    that = sealed["ansible.builtin.assert"]["that"]
    assert "item.stat.pw_name == item.item.owner" in that
    assert "item.stat.mode == '0600'" in that
    assert "item.stat.nlink == 1" in that
    assert "not item.stat.islnk" in that


def test_each_reader_gets_its_own_owner_only_copy_for_the_admitted_principal() -> None:
    write = _task(WRITE)
    assert write["ansible.builtin.copy"]["mode"] == "0600"
    assert write["loop"] == COPIES
    assert write["ansible.builtin.copy"]["content"] == (f"{{{{ {PREFIX}document }}}}")
    entries = _documents()[1]["vars"][f"{PREFIX}entries"]
    assert "POSTGRES_USER=ditto" in entries
    assert "POSTGRES_DB=ditto_platform_prod" in entries
    directories = _task(DIRECTORIES)
    assert directories["ansible.builtin.file"]["mode"] == "0700"
    assert {item["owner"] for item in directories["loop"]} == set(OWNERS)
    homes = dict(
        re.findall(
            r"getent_passwd\['([^']+)'\]\[4\] == '([^']+)'",
            " ".join(_task(ACCOUNT_CHECK)["ansible.builtin.assert"]["that"]),
        )
    )
    for item in directories["loop"]:
        assert str(Path(item["path"]).parent) == homes[item["owner"]]


def test_playbook_group_connection_and_ci_registration() -> None:
    (play,) = yaml.safe_load(PLAYBOOK.read_text())
    assert play["hosts"] == "role_coding_hosted"
    assert play["become"] is True and play["gather_facts"] is False
    assert play["roles"] == ["coding_hosted_postgres_environment"]
    assert set(play) == {"name", "hosts", "become", "gather_facts", "roles"}
    (fixture,) = yaml.safe_load(
        (
            ROOT / "infra/ansible/tests/coding-hosted-postgres-environment.yml"
        ).read_text()
    )
    assert fixture["hosts"] == "localhost" and fixture["connection"] == "local"
    assert fixture["become"] is False and fixture["gather_facts"] is False
    assert fixture["roles"] == ["coding_hosted_postgres_environment"]
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


def test_docs_describe_every_forgery_guard() -> None:
    docs = (ROOT / "infra/docs/coding-hosted-postgres-v2.md").read_text()
    section = _flat(
        docs.split("## Native environment files", 1)[1].split("\n## ", 1)[0]
    )
    for phrase in (
        "`coding_hosted_postgres_environment_*`",
        "extra vars",
        "`coding_hosted_postgres_environment_units`",
        "`coding_hosted_postgres_environment_document`",
        "gathers no facts",
        "`ansible_facts`",
        "exactly 40 lowercase hex characters",
        "trailing newline",
        "`inactive` or `failed`",
        "`refreshing`",
        "`maintenance`",
        "An empty listing",
    ):
        assert phrase in section, phrase
