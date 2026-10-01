"""Tests for text, JSON and SARIF rendering of findings."""

import json

import pytest

from ci_gate_watch.drift import Drift
from ci_gate_watch.report import (
    build_payload,
    render,
    render_json,
    render_sarif,
    render_text,
)
from ci_gate_watch.workflows import WorkflowDrift

ERROR = Drift(
    drift_type="enforce_admins_disabled",
    severity="error",
    message="policy sets enforce_admins: true but enforce_admins is not enabled on the branch",
    key="enforce_admins",
    expected=True,
    actual=False,
)
WARNING = Drift(
    drift_type="undeclared_check",
    severity="warning",
    message="check 'legacy' is required on the branch but the policy does not mention it",
    key="legacy",
    expected=None,
    actual="required",
)


def test_text_lists_severity_and_type():
    text = render_text([ERROR])
    assert "enforce_admins_disabled" in text
    assert "error" in text


def test_text_reports_a_clean_audit():
    assert "No drift detected" in render_text([])


def test_text_includes_the_expected_and_actual_values():
    text = render_text([ERROR])
    assert "True" in text and "False" in text


def test_json_is_valid_and_shaped_for_ci():
    payload = json.loads(render_json([ERROR], repo="owner/repo", branch="main"))
    assert payload["tool"]["name"] == "ci-gate-watch"
    assert payload["repo"] == "owner/repo"
    assert payload["branch"] == "main"
    assert payload["summary"]["errors"] == 1
    assert payload["results"][0]["drift_type"] == "enforce_admins_disabled"


def test_json_summary_separates_errors_from_warnings():
    payload = json.loads(render_json([ERROR, WARNING], repo="owner/repo"))
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["errors"] == 1
    assert payload["summary"]["warnings"] == 1


def test_json_includes_the_policy_that_was_audited():
    payload = json.loads(
        render_json([ERROR], repo="owner/repo", policy={"required_reviews": 2})
    )
    assert payload["policy"]["required_reviews"] == 2


def test_sarif_is_a_valid_2_1_0_document():
    payload = json.loads(render_sarif([ERROR], repo="owner/repo"))
    assert payload["version"] == "2.1.0"
    assert payload["runs"][0]["tool"]["driver"]["name"] == "ci-gate-watch"


def test_sarif_has_one_result_and_one_rule_per_type():
    payload = json.loads(render_sarif([ERROR, WARNING], repo="owner/repo"))
    run = payload["runs"][0]
    assert len(run["results"]) == 2
    assert {rule["id"] for rule in run["tool"]["driver"]["rules"]} == {
        "enforce_admins_disabled",
        "undeclared_check",
    }


def test_sarif_maps_severity_to_level():
    payload = json.loads(render_sarif([ERROR, WARNING], repo="owner/repo"))
    assert [r["level"] for r in payload["runs"][0]["results"]] == ["error", "warning"]


def test_sarif_with_no_findings_is_still_valid():
    payload = json.loads(render_sarif([], repo="owner/repo"))
    assert payload["runs"][0]["results"] == []


def test_render_dispatches_on_format():
    assert render([ERROR], "json", repo="o/r").lstrip().startswith("{")
    assert render([ERROR], "sarif", repo="o/r").lstrip().startswith("{")
    assert "enforce_admins_disabled" in render([ERROR], "text", repo="o/r")


def test_render_rejects_an_unknown_format():
    with pytest.raises(ValueError):
        render([ERROR], "xml", repo="o/r")


def test_workflow_drift_objects_are_rendered_like_policy_drift():
    workflow_finding = WorkflowDrift(
        drift_type="required_check_orphaned",
        severity="error",
        message="required check 'typecheck' is not produced by any workflow",
        key="typecheck",
    )
    assert "required_check_orphaned" in render_text([workflow_finding], repo="o/r")
    payload = json.loads(render_json([workflow_finding], repo="o/r"))
    assert payload["results"][0]["key"] == "typecheck"


def test_payload_lists_the_workflows_that_were_parsed():
    from ci_gate_watch.workflows import parse_workflow

    workflows = [parse_workflow("on: [push]\njobs:\n  lint:\n    steps: []\n", "ci.yml")]
    payload = build_payload([], repo="o/r", workflows=workflows)
    assert payload["workflows"][0]["jobs"][0]["name"] == "lint"


def test_json_output_is_parseable_even_with_no_findings():
    payload = json.loads(render_json([], repo="o/r"))
    assert payload["summary"]["total"] == 0


def test_sarif_locations_point_at_the_policy_file():
    payload = json.loads(render_sarif([ERROR], repo="owner/repo"))
    location = payload["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == ".ci-gate-policy.yml"


def test_sarif_result_without_a_key_has_no_location():
    no_key = Drift(drift_type="linear_history_disabled", severity="error", message="m")
    payload = json.loads(render_sarif([no_key], repo="owner/repo"))
    assert "locations" not in payload["runs"][0]["results"][0]
