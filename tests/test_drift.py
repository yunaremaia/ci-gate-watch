"""Tests for drift detection between declared policy and actual configuration."""

from ci_gate_watch.drift import audit

# The shape GitHub returns for GET /repos/{o}/{r}/branches/{b}/protection.
COMPLIANT = {
    "required_pull_request_reviews": {
        "required_approving_review_count": 2,
        "dismiss_stale_reviews": True,
    },
    "required_status_checks": {
        "strict": True,
        "contexts": ["lint", "test"],
    },
    "enforce_admins": {"enabled": True},
    "required_linear_history": {"enabled": True},
    "required_signatures": {"enabled": False},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
}

POLICY = {
    "required_reviews": 2,
    "required_checks": ["lint", "test"],
    "enforce_admins": True,
    "required_linear_history": True,
    "required_signed_commits": False,
    "allow_force_pushes": False,
    "allow_deletions": False,
}


def relaxed(**overrides):
    policy = dict(POLICY, **overrides)
    return policy


def kinds(findings):
    return sorted(f.drift_type for f in findings)


def only(findings, kind):
    return [f for f in findings if f.drift_type == kind]


def test_matching_configuration_produces_no_findings():
    assert audit(POLICY, COMPLIANT) == []


def test_missing_required_reviews_is_found():
    actual = {**COMPLIANT, "required_pull_request_reviews": None}
    found = only(audit(POLICY, actual), "required_reviews_missing")
    assert len(found) == 1
    assert "2" in found[0].message


def test_lower_review_count_is_found():
    actual = {**COMPLIANT, "required_pull_request_reviews": {"required_approving_review_count": 1}}
    found = only(audit(POLICY, actual), "required_reviews_low")
    assert found[0].severity == "error"
    assert "1" in found[0].message and "2" in found[0].message


def test_higher_review_count_is_not_drift():
    actual = {**COMPLIANT, "required_pull_request_reviews": {"required_approving_review_count": 3}}
    assert only(audit(POLICY, actual), "required_reviews_low") == []


def test_missing_required_check_is_found():
    actual = {**COMPLIANT, "required_status_checks": {"strict": True, "contexts": ["lint"]}}
    found = only(audit(POLICY, actual), "required_check_missing")
    assert [f.key for f in found] == ["test"]
    assert found[0].severity == "error"


def test_disabled_status_checks_reports_every_required_check():
    actual = {**COMPLIANT, "required_status_checks": None}
    found = only(audit(POLICY, actual), "required_check_missing")
    assert sorted(f.key for f in found) == ["lint", "test"]


def test_extra_actual_check_is_reported_as_a_warning():
    actual = {
        **COMPLIANT,
        "required_status_checks": {"strict": True, "contexts": ["lint", "test", "legacy"]},
    }
    found = only(audit(POLICY, actual), "undeclared_check")
    assert [f.key for f in found] == ["legacy"]
    assert found[0].severity == "warning"


def test_disabled_enforce_admins_is_found():
    actual = {**COMPLIANT, "enforce_admins": {"enabled": False}}
    found = only(audit(POLICY, actual), "enforce_admins_disabled")
    assert found[0].severity == "error"


def test_enabled_force_pushes_is_found():
    actual = {**COMPLIANT, "allow_force_pushes": {"enabled": True}}
    assert kinds(audit(POLICY, actual)) == ["force_pushes_allowed"]


def test_enabled_deletions_is_found():
    actual = {**COMPLIANT, "allow_deletions": {"enabled": True}}
    assert kinds(audit(POLICY, actual)) == ["deletions_allowed"]


def test_disabled_linear_history_is_found():
    actual = {**COMPLIANT, "required_linear_history": {"enabled": False}}
    assert kinds(audit(POLICY, actual)) == ["linear_history_disabled"]


def test_disabled_signed_commits_is_found():
    policy = relaxed(required_signed_commits=True)
    actual = {**COMPLIANT, "required_signatures": {"enabled": False}}
    assert kinds(audit(policy, actual)) == ["signed_commits_disabled"]


def test_signed_commits_not_required_is_not_drift():
    actual = {**COMPLIANT, "required_signatures": {"enabled": False}}
    assert audit(POLICY, actual) == []


def test_policy_that_requires_nothing_finds_nothing_in_an_empty_config():
    assert audit({}, {}) == []


def test_findings_are_sorted_errors_first():
    actual = {
        **COMPLIANT,
        "enforce_admins": {"enabled": False},
        "required_status_checks": {"strict": True, "contexts": ["lint", "test", "legacy"]},
    }
    findings = audit(POLICY, actual)
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key=lambda s: 0 if s == "error" else 1)
    assert severities[0] == "error"


def test_drift_serialises_for_json_output():
    actual = {**COMPLIANT, "enforce_admins": {"enabled": False}}
    payload = audit(POLICY, actual)[0].as_dict()
    assert payload["drift_type"] == "enforce_admins_disabled"
    assert payload["severity"] == "error"
    assert isinstance(payload["message"], str)


def test_finding_carries_the_expected_and_actual_values():
    actual = {**COMPLIANT, "required_pull_request_reviews": {"required_approving_review_count": 1}}
    found = only(audit(POLICY, actual), "required_reviews_low")[0]
    assert found.expected == 2
    assert found.actual == 1


def test_a_policy_requiring_more_reviews_than_configured_reports_one_finding():
    # Guards against double-reporting "missing" and "too low" for the same fact.
    actual = {**COMPLIANT, "required_pull_request_reviews": None}
    findings = audit(POLICY, actual)
    assert len(only(findings, "required_reviews_missing")) == 1
    assert only(findings, "required_reviews_low") == []
