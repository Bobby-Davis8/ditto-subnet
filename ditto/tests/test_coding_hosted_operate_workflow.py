import ast
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/coding-hosted-operate.yml"
VERIFIER = ROOT / "infra/scripts/coding-hosted-verify.py"
STACK = ROOT / "infra/terraform/stacks/gcp-platform/coding-hosted-operate.tf"
WIRING = ROOT / "infra/terraform/stacks/gcp-platform/coding-hosted.tf"
ENVIRONMENT = ROOT / "infra/github/coding-hosted-operate-environment.json"
RULESET = ROOT / "infra/github/coding-hosted-operate-ruleset.json"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_operate_is_manual_main_only_with_fixed_operations() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow[True])
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"operation", "confirmation"}
    assert inputs["operation"]["type"] == "choice"
    assert inputs["operation"]["options"] == ["verify"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert set(workflow["jobs"]) == {"verify"}
    job = workflow["jobs"]["verify"]
    assert job["environment"] == "coding-hosted-operate"
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "inputs.operation == 'verify'" in job["if"]
    assert "inputs.confirmation == 'OPERATE CODING HOST verify'" in job["if"]
    assert job["permissions"] == {"contents": "read", "id-token": "write"}


def test_every_root_capable_job_requires_the_protected_environment() -> None:
    for job in _workflow()["jobs"].values():
        assert job["environment"] == "coding-hosted-operate"
    environment = json.loads(ENVIRONMENT.read_text())
    assert environment["prevent_self_review"] is True
    assert {reviewer["id"] for reviewer in environment["reviewers"]} == {
        6766068,
        170978465,
    }
    assert len(environment["reviewers"]) == 2
    assert environment["deployment_branch_policy"] == {
        "protected_branches": False,
        "custom_branch_policies": True,
    }


def test_operate_uses_exact_clean_source_before_authentication() -> None:
    steps = _workflow()["jobs"]["verify"]["steps"]
    assert steps[0]["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }
    clean = _step(steps, "Verify exact clean source before authentication")
    auth = next(
        step
        for step in steps
        if step.get("uses", "").startswith("google-github-actions/auth@")
    )
    assert steps.index(clean) < steps.index(auth)
    assert 'test -z "$(git status --porcelain)"' in clean["run"]
    assert auth["with"] == {
        "workload_identity_provider": (
            "${{ vars.GCP_CODING_HOSTED_OPERATE_WIF_PROVIDER }}"
        ),
        "service_account": "${{ vars.GCP_CODING_HOSTED_OPERATE_SA }}",
    }


def test_operate_has_no_command_inputs_secrets_or_broad_authority() -> None:
    text = WORKFLOW.read_text()
    for step in _workflow()["jobs"]["verify"]["steps"]:
        command = step.get("run", "")
        assert "${{" not in command
        assert "set -x" not in command
    for forbidden in (
        "secrets.",
        "gcloud secrets",
        "terraform",
        "GCP_TF_APPLY_SA",
        "upload-artifact",
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
    ):
        assert forbidden not in text
    run = _step(
        _workflow()["jobs"]["verify"]["steps"],
        "Run the fixed read-only host verifier",
    )["run"]
    assert "gcloud compute ssh ditto-coding-hosted-v2 \\" in run
    assert "--project=ditto-app-dev" in run
    assert "--zone=us-central1-a" in run
    assert "--tunnel-through-iap" in run
    assert "--command='sudo -n /usr/bin/python3 -I -'" in run
    assert "< infra/scripts/coding-hosted-verify.py" in run
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in run
    # The only retried failure is runner-side key propagation, never a verdict.
    assert "@compute\\.[0-9]+: Permission denied \\(publickey\\)" in run
    assert '[ "$attempt" -ge 3 ]' in run


def test_operate_key_is_short_lived_and_always_removed() -> None:
    steps = _workflow()["jobs"]["verify"]["steps"]
    add = _step(steps, "Register a short-lived OS Login key")["run"]
    assert "--ttl 30m" in add
    remove = _step(steps, "Remove the short-lived OS Login key")
    assert remove["if"] == "always()"
    assert "gcloud compute os-login ssh-keys remove" in remove["run"]


def _verifier_calls() -> list[list[object]]:
    tree = ast.parse(VERIFIER.read_text())
    argvs: list[list[object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run":
            argument = node.args[0]
            assert isinstance(argument, ast.List)
            argv: list[object] = [
                element.value if isinstance(element, ast.Constant) else None
                for element in argument.elts
            ]
            argvs.append(argv)
    return argvs


def test_verifier_runs_only_fixed_read_only_commands() -> None:
    source = VERIFIER.read_text()
    assert "shell=True" not in source
    assert "open(" not in source
    assert "write_text" not in source and "write_bytes" not in source
    assert "os.remove" not in source and "unlink" not in source
    assert "subprocess.run(" in source and source.count("subprocess.run(") == 1
    commands = _verifier_calls()
    assert commands, "verifier must run fixed commands"
    for argv in commands:
        head = argv[0]
        if head == "systemctl":
            assert argv[1] == "is-active"
        elif head == "openssl":
            assert argv[:3] == ["openssl", "pkey", "-pubin"]
        elif head == "runuser":
            # argv[2] is the RUNTIME_USER constant; the tail is one fixed check.
            assert argv[1] == "-u" and argv[2] is None and argv[3] == "--"
            assert argv[-4:] in (
                ["systemctl", "--user", "is-active", "coding-hosted-docker.service"],
                ["/usr/bin/python3", "-I", None, "verify"],
            )
        else:
            raise AssertionError(f"unexpected verifier command: {head}")


def test_verifier_never_opens_private_material() -> None:
    source = VERIFIER.read_text()
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    private = {
        "CUSTODY_PRIVATE_KEY",
        "CUSTODY_RECEIPT",
        "CUSTODY_POSTGRES_ENVIRONMENT",
        "WORKER_POSTGRES_ENVIRONMENT",
    }
    seen = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id in private):
            continue
        if isinstance(node.ctx, ast.Store):
            continue
        seen.add(node.id)
        parent = parents[node]
        # Private paths may only reach lstat metadata or a report label.
        assert (
            isinstance(parent, ast.Call)
            and getattr(parent.func, "id", None) == "metadata"
        ) or isinstance(parent, (ast.Tuple, ast.Dict)), ast.unparse(parent)
        assert not any(
            isinstance(call, ast.Call) and getattr(call.func, "id", None) == "run"
            for call in ast.walk(parent)
        )
    assert seen == private
    assert "gcloud" not in source and "secretmanager" not in source.lower()


def test_verifier_off_host_reports_nothing_about_the_machine() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(VERIFIER)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["schema"] == "ditto-coding-host-verify-v1"
    assert report["ok"] is False
    assert report["reads_secrets"] is False
    assert report["mutates"] is False
    assert [check["name"] for check in report["checks"]] == ["host identity"]
    assert socket.gethostname() not in result.stdout


def test_workflow_identity_is_pinned_to_this_main_workflow() -> None:
    text = STACK.read_text()
    for clause in (
        "assertion.repository_id == '1224630318'",
        "assertion.repository_owner_id == '148669063'",
        "assertion.ref == 'refs/heads/main'",
        "assertion.event_name == 'workflow_dispatch'",
        "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/.github/workflows/"
        "coding-hosted-operate.yml@refs/heads/main'",
        "assertion.actor_id in ['6766068', '170978465']",
    ):
        assert clause in text
    assert (
        '"repo:ditto-assistant/ditto-subnet:environment:coding-hosted-operate"' in text
    )
    for forbidden in (
        "google_project_iam_member",
        "google_project_iam_custom_role",
        "secret_manager",
        "storage_bucket_iam",
        "roles/owner",
        "roles/editor",
    ):
        assert forbidden not in text
    assert "default     = false" in text
    wiring = WIRING.read_text()
    assert "var.enable_coding_hosted_operate_workflow" in wiring
    prod = (ROOT / "infra/terraform/stacks/gcp-platform/prod.auto.tfvars").read_text()
    assert re.search(
        r"(?m)^enable_coding_hosted_operate_workflow\s*=\s*false\s*$", prod
    )
    assert (
        '"serviceAccount:${google_service_account.coding_hosted_operate.email}"'
        in wiring
    )


def test_review_ruleset_covers_every_root_capable_surface() -> None:
    ruleset = json.loads(RULESET.read_text())
    parameters = ruleset["rules"][0]["parameters"]
    assert parameters["require_last_push_approval"] is True
    (reviewer,) = parameters["required_reviewers"]
    assert reviewer["minimum_approvals"] == 1
    assert set(reviewer["file_patterns"]) == {
        ".github/workflows/coding-hosted-operate.yml",
        "infra/scripts/coding-hosted-verify.py",
        "infra/github/coding-hosted-operate-*.json",
        "infra/terraform/stacks/gcp-platform/coding-hosted*.tf",
        "infra/terraform/modules/coding-hosted-host/**",
    }
