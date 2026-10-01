"""End-to-end tests for the ci-gate-watch CLI, run as real subprocesses.

The ``workflows`` and ``checks`` commands are fully offline, so most of these
tests exercise the real binary without a token. The GitHub-touching path is
covered by driving the CLI against a fake ``gh`` on PATH.
"""

import json
import os
import subprocess
import sys

import pytest

CI_WORKFLOW = """\
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.11", "3.13"]
    runs-on: ubuntu-latest
    steps:
      - run: pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
"""

RELEASE_WORKFLOW = """\
name: Release
on:
  push:
    branches: [main]
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - run: ./publish.sh
"""

POLICY = """\
required_reviews: 2
required_checks:
  - lint
  - test (3.11)
  - typecheck
enforce_admins: true
allow_force_pushes: false
"""


@pytest.fixture
def repo(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(CI_WORKFLOW, encoding="utf-8")
    (workflows / "release.yml").write_text(RELEASE_WORKFLOW, encoding="utf-8")
    (tmp_path / ".ci-gate-policy.yml").write_text(POLICY, encoding="utf-8")
    return tmp_path


def run(*args, cwd, expect=None, env=None):
    result = subprocess.run(
        [sys.executable, "-m", "ci_gate_watch", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if expect is not None:
        assert result.returncode == expect, (
            f"expected exit {expect}, got {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def test_help_exits_zero(repo):
    assert "audit" in run("--help", cwd=repo, expect=0).stdout


def test_version_exits_zero(repo):
    assert "ci-gate-watch" in run("--version", cwd=repo, expect=0).stdout


def test_check_contexts_lists_every_context(repo):
    result = run("check-contexts", cwd=repo, expect=0)
    assert "lint" in result.stdout
    assert "test (3.11)" in result.stdout
    assert "publish" in result.stdout


def test_check_contexts_json_is_parseable(repo):
    payload = json.loads(run("check-contexts", "--format", "json", cwd=repo, expect=0).stdout)
    assert "test (3.13)" in payload["contexts"]


def test_checks_passes_when_required_checks_all_exist(repo):
    (repo / ".ci-gate-policy.yml").write_text(
        "required_checks:\n  - lint\n  - test (3.11)\n", encoding="utf-8"
    )
    run("checks", cwd=repo, expect=0)


def test_checks_fails_on_an_orphaned_required_check(repo):
    result = run("checks", cwd=repo, expect=1)
    assert "required_check_orphaned" in result.stdout
    assert "typecheck" in result.stdout


def test_checks_flags_a_required_check_that_never_runs_on_prs(repo):
    (repo / ".ci-gate-policy.yml").write_text(
        "required_checks:\n  - publish\n", encoding="utf-8"
    )
    result = run("checks", cwd=repo, expect=1)
    assert "required_check_not_on_pull_request" in result.stdout


def test_checks_json_has_both_drift_types(repo):
    (repo / ".ci-gate-policy.yml").write_text(
        "required_checks:\n  - publish\n  - typecheck\n", encoding="utf-8"
    )
    payload = json.loads(run("checks", "--format", "json", cwd=repo, expect=1).stdout)
    assert sorted(payload["summary"]["by_type"]) == [
        "required_check_not_on_pull_request",
        "required_check_orphaned",
    ]


def test_checks_sarif_is_valid(repo):
    payload = json.loads(run("checks", "--format", "sarif", cwd=repo, expect=1).stdout)
    assert payload["version"] == "2.1.0"


def test_checks_without_a_policy_file_uses_an_empty_required_set(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    run("checks", cwd=tmp_path, expect=0)


def test_checks_rejects_a_malformed_policy(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".ci-gate-policy.yml").write_text("required_reviews: [oops\n", encoding="utf-8")
    result = run("checks", cwd=tmp_path, expect=2)
    assert "policy" in result.stderr.lower()


def test_audit_requires_a_repo(repo):
    result = run("audit", cwd=repo, expect=2)
    assert "--repo" in result.stderr


def test_audit_rejects_a_malformed_repo_slug(repo):
    run("audit", "--repo", "not-a-slug", cwd=repo, expect=2)


def test_audit_rejects_an_invalid_format(repo):
    run("audit", "--repo", "o/r", "--format", "xml", cwd=repo, expect=2)


def test_audit_reports_drift_from_a_fake_gh(repo, tmp_path):
    protection = {
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "required_status_checks": {"strict": True, "contexts": ["lint", "test (3.11)"]},
        "enforce_admins": {"enabled": False},
    }
    fake_gh = _write_fake_gh(tmp_path, protection)

    env = dict(os.environ, PATH=f"{fake_gh.parent}:{os.environ['PATH']}")
    result = run("audit", "--repo", "owner/repo", "--policy", str(repo / ".ci-gate-policy.yml"),
                 cwd=repo, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "required_reviews_low" in result.stdout
    assert "enforce_admins_disabled" in result.stdout
    assert "required_check_missing" in result.stdout


def test_audit_exits_zero_when_the_branch_matches_the_policy(repo, tmp_path):
    protection = {
        "required_pull_request_reviews": {"required_approving_review_count": 2},
        "required_status_checks": {
            "strict": True,
            "contexts": ["lint", "test (3.11)", "test (3.13)", "typecheck"],
        },
        "enforce_admins": {"enabled": True},
        "allow_force_pushes": {"enabled": False},
    }
    fake_gh = _write_fake_gh(tmp_path, protection)
    env = dict(os.environ, PATH=f"{fake_gh.parent}:{os.environ['PATH']}")

    policy = repo / "compliant.yml"
    policy.write_text(
        "required_reviews: 2\n"
        "required_checks:\n  - lint\n  - test (3.11)\n  - test (3.13)\n  - typecheck\n"
        "enforce_admins: true\n",
        encoding="utf-8",
    )
    result = run("audit", "--repo", "owner/repo", "--policy", str(policy), cwd=repo, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No drift detected" in result.stdout


def test_audit_json_carries_the_policy_and_workflows(repo, tmp_path):
    protection = {"enforce_admins": {"enabled": False}}
    fake_gh = _write_fake_gh(tmp_path, protection)
    env = dict(os.environ, PATH=f"{fake_gh.parent}:{os.environ['PATH']}")
    result = run("audit", "--repo", "owner/repo", "--policy", str(repo / ".ci-gate-policy.yml"),
                 "--format", "json", cwd=repo, env=env)
    payload = json.loads(result.stdout)
    assert payload["repo"] == "owner/repo"
    assert payload["policy"]["required_reviews"] == 2
    # Without --with-workflows no local workflows are parsed.
    assert payload["workflows"] == []


def test_audit_reports_a_github_error_clearly(repo, tmp_path):
    fake_gh = _write_fake_gh(tmp_path, {"message": "Bad credentials"})
    env = dict(os.environ, PATH=f"{fake_gh.parent}:{os.environ['PATH']}")
    result = run("audit", "--repo", "owner/repo", "--policy", str(repo / ".ci-gate-policy.yml"),
                 cwd=repo, env=env)
    assert result.returncode == 2
    assert "Bad credentials" in result.stderr
    assert "gh auth login" in result.stderr


def test_audit_can_also_run_the_workflow_check(repo, tmp_path):
    protection = {"enforce_admins": {"enabled": False}}
    fake_gh = _write_fake_gh(tmp_path, protection)
    env = dict(os.environ, PATH=f"{fake_gh.parent}:{os.environ['PATH']}")
    result = run("audit", "--repo", "owner/repo", "--policy", str(repo / ".ci-gate-policy.yml"),
                 "--with-workflows", cwd=repo, env=env)
    assert "required_check_orphaned" in result.stdout


def test_missing_policy_file_exits_two(repo):
    run("audit", "--repo", "o/r", "--policy", "nope.yml", cwd=repo, expect=2)


def _write_fake_gh(tmp_path, payload):
    """Write an executable `gh` stub that always returns ``payload``."""
    directory = tmp_path / "fakebin"
    directory.mkdir(exist_ok=True)
    script = directory / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"sys.stdout.write({json.dumps(json.dumps(payload))})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_unprotected_branch_is_reported_as_drift_not_a_tool_error(repo, tmp_path):
    # A branch with no protection at all is the most serious drift there is,
    # so it must be a finding with exit 1, not a usage error with exit 2.
    fake_gh = _write_fake_gh(tmp_path, {"message": "Branch not protected"})
    env = dict(os.environ, PATH=f"{fake_gh.parent}:{os.environ['PATH']}")
    result = run("audit", "--repo", "owner/repo", "--policy", str(repo / ".ci-gate-policy.yml"),
                 cwd=repo, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "protection_missing" in result.stdout


def test_auth_failure_is_still_exit_two(repo, tmp_path):
    fake_gh = _write_fake_gh(tmp_path, {"message": "Bad credentials"})
    env = dict(os.environ, PATH=f"{fake_gh.parent}:{os.environ['PATH']}")
    result = run("audit", "--repo", "owner/repo", "--policy", str(repo / ".ci-gate-policy.yml"),
                 cwd=repo, env=env)
    assert result.returncode == 2
