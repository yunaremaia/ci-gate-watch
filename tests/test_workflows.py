"""Tests for workflow parsing and workflow/check-context drift (issues #5, #6)."""

import pytest

from ci_gate_watch.workflows import (
    check_contexts_for,
    load_workflows,
    parse_workflow,
    workflow_drift,
)

CI = """\
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.13"]
    steps:
      - run: pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
"""

PUSH_ONLY = """\
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


def test_parses_workflow_name_and_jobs():
    workflow = parse_workflow(CI, "ci.yml")
    assert workflow.name == "CI"
    assert [job.name for job in workflow.jobs] == ["test", "lint"]


def test_records_job_run_names_for_matrix_expansion():
    workflow = parse_workflow(CI, "ci.yml")
    test_job = workflow.job("test")
    assert sorted(test_job.contexts) == ["test (3.11)", "test (3.13)"]


def test_non_matrix_job_has_a_bare_context():
    workflow = parse_workflow(CI, "ci.yml")
    assert workflow.job("lint").contexts == ("lint",)


def test_records_triggers():
    workflow = parse_workflow(CI, "ci.yml")
    assert "push" in workflow.triggers
    assert "pull_request" in workflow.triggers


def test_push_only_workflow_records_only_push():
    assert parse_workflow(PUSH_ONLY, "release.yml").triggers == {"push"}


def test_explicit_run_name_overrides_the_generated_context():
    text = """\
on: [push]
jobs:
  test:
    name: unit tests
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""
    assert parse_workflow(text, "ci.yml").job("test").contexts == ("unit tests",)


def test_job_lookup_returns_none_for_an_unknown_job():
    assert parse_workflow(CI, "ci.yml").job("nope") is None


def test_malformed_workflow_is_skipped_not_fatal(tmp_path):
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "bad.yml").write_text("jobs: [unclosed\n", encoding="utf-8")
    (directory / "ci.yml").write_text(CI, encoding="utf-8")
    workflows = load_workflows(tmp_path)
    assert [w.path.name for w in workflows] == ["ci.yml"]


def test_load_workflows_reads_the_workflows_directory(tmp_path):
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text(CI, encoding="utf-8")
    (directory / "release.yaml").write_text(PUSH_ONLY, encoding="utf-8")
    workflows = load_workflows(tmp_path)
    assert sorted(w.path.name for w in workflows) == ["ci.yaml", "ci.yml", "release.yaml"][1:]


def test_load_workflows_returns_empty_when_there_is_no_directory(tmp_path):
    assert load_workflows(tmp_path) == []


def test_check_contexts_for_collects_every_workflow_context():
    workflows = [parse_workflow(CI, "ci.yml"), parse_workflow(PUSH_ONLY, "release.yml")]
    contexts = check_contexts_for(workflows)
    assert "lint" in contexts
    assert "test (3.11)" in contexts
    assert "publish" in contexts


def test_required_check_no_workflow_produces_is_found():
    workflows = [parse_workflow(CI, "ci.yml")]
    findings = workflow_drift(workflows, required_checks=("lint", "typecheck"))
    assert [f.drift_type for f in findings] == ["required_check_orphaned"]
    assert findings[0].key == "typecheck"


def test_bare_matrix_job_name_is_orphaned_because_github_names_it_per_version():
    # GitHub names matrix checks "test (3.11)", so requiring plain "test"
    # gates nothing -- exactly the rename drift this check exists to catch.
    workflows = [parse_workflow(CI, "ci.yml")]
    findings = workflow_drift(workflows, required_checks=("test",))
    assert [f.drift_type for f in findings] == ["required_check_orphaned"]


def test_orphaned_required_check_names_the_missing_one():
    workflows = [parse_workflow(CI, "ci.yml")]
    finding = workflow_drift(workflows, required_checks=("typecheck",))[0]
    assert finding.key == "typecheck"
    assert finding.severity == "error"


def test_matrix_context_matches_the_generated_name():
    workflows = [parse_workflow(CI, "ci.yml")]
    assert workflow_drift(workflows, required_checks=("test (3.11)",)) == []


def test_required_check_produced_only_on_push_is_a_trigger_mismatch():
    workflows = [parse_workflow(PUSH_ONLY, "release.yml")]
    findings = workflow_drift(workflows, required_checks=("publish",))
    assert [f.drift_type for f in findings] == ["required_check_not_on_pull_request"]


def test_workflow_that_runs_on_pull_request_has_no_trigger_mismatch():
    workflows = [parse_workflow(CI, "ci.yml")]
    assert workflow_drift(workflows, required_checks=("lint",)) == []


def test_no_required_checks_declared_finds_nothing():
    workflows = [parse_workflow(CI, "ci.yml")]
    assert workflow_drift(workflows, required_checks=()) == []


def test_no_workflows_at_all_flags_every_required_check():
    findings = workflow_drift([], required_checks=("lint",))
    assert [f.drift_type for f in findings] == ["required_check_orphaned"]


def test_trigger_mismatch_does_not_also_report_orphaned():
    workflows = [parse_workflow(PUSH_ONLY, "release.yml")]
    findings = workflow_drift(workflows, required_checks=("publish",))
    assert [f.drift_type for f in findings].count("required_check_orphaned") == 0


def test_orphaned_and_trigger_mismatch_can_both_be_reported():
    workflows = [parse_workflow(PUSH_ONLY, "release.yml")]
    findings = workflow_drift(workflows, required_checks=("publish", "lint"))
    assert sorted(f.drift_type for f in findings) == [
        "required_check_not_on_pull_request",
        "required_check_orphaned",
    ]


def test_workflow_drift_finding_serialises():
    findings = workflow_drift([], required_checks=("lint",))
    payload = findings[0].as_dict()
    assert payload["drift_type"] == "required_check_orphaned"
    assert payload["key"] == "lint"


@pytest.mark.parametrize("suffix", [".yml", ".yaml"])
def test_both_workflow_extensions_are_supported(tmp_path, suffix):
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    (directory / f"ci{suffix}").write_text(CI, encoding="utf-8")
    assert [w.path.name for w in load_workflows(tmp_path)] == [f"ci{suffix}"]


def test_workflow_without_a_name_field_falls_back_to_the_filename():
    text = "on: [push]\njobs:\n  test:\n    steps: []\n"
    assert parse_workflow(text, "unit.yml").name == "unit"


def test_non_mapping_strategy_skipped_silently(tmp_path):
    """Non-mapping strategy: (scalar/list) raises AttributeError and is skipped.

    Regression test for https://github.com/yunaremaia/ci-gate-watch/issues/24
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    # strategy: fast  — a bare string, not a mapping
    (wf_dir / "broken.yml").write_text(
        "name: Broken\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    strategy: fast\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    workflows = load_workflows(tmp_path)
    assert workflows == [], "malformed workflow should be skipped, not crash"


def test_non_mapping_matrix_skipped_silently(tmp_path):
    """Non-mapping matrix (scalar) raises AttributeError and is skipped.

    Regression test for https://github.com/yunaremaia/ci-gate-watch/issues/24
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    # strategy.matrix: 3.11 — a bare scalar, not a mapping
    (wf_dir / "broken.yml").write_text(
        "name: Broken\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    strategy:\n"
        "      matrix: 3.11\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    workflows = load_workflows(tmp_path)
    assert workflows == [], "malformed workflow should be skipped, not crash"


def test_non_mapping_strategy_list_skipped_silently(tmp_path):
    """Non-mapping strategy (list) raises AttributeError and is skipped.

    Regression test for https://github.com/yunaremaia/ci-gate-watch/issues/24
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    # strategy: [a, b] — a list, not a mapping
    (wf_dir / "broken.yml").write_text(
        "name: Broken\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    strategy: [a, b]\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    workflows = load_workflows(tmp_path)
    assert workflows == [], "malformed workflow should be skipped, not crash"
