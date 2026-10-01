"""Tests for CI gate policy loading and validation."""

import pytest

from ci_gate_watch.policy import Policy, PolicyError, load_policy

MINIMAL = """\
required_reviews: 2
required_checks:
  - lint
  - test
enforce_admins: true
allow_force_pushes: false
allow_deletions: false
"""


def write(tmp_path, text, name=".ci-gate-policy.yml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_the_documented_policy_file(tmp_path):
    policy = load_policy(write(tmp_path, MINIMAL))
    assert policy.required_reviews == 2
    assert policy.required_checks == ("lint", "test")
    assert policy.enforce_admins is True
    assert policy.allow_force_pushes is False
    assert policy.allow_deletions is False


def test_defaults_are_permissive_when_a_field_is_absent(tmp_path):
    policy = load_policy(write(tmp_path, "required_reviews: 1\n"))
    assert policy.required_reviews == 1
    assert policy.required_checks == ()
    assert policy.enforce_admins is False
    assert policy.required_linear_history is False
    assert policy.required_signed_commits is False


def test_missing_policy_file_is_an_error(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "nope.yml")


def test_empty_policy_is_valid_and_empty(tmp_path):
    policy = load_policy(write(tmp_path, "{}\n"))
    assert policy.required_reviews == 0
    assert policy.required_checks == ()


def test_negative_review_count_is_rejected(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(write(tmp_path, "required_reviews: -1\n"))


def test_non_integer_review_count_is_rejected(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(write(tmp_path, "required_reviews: two\n"))


def test_checks_must_be_a_list_of_strings(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(write(tmp_path, "required_checks: lint\n"))
    with pytest.raises(PolicyError):
        load_policy(write(tmp_path, "required_checks:\n  - 3\n"))


def test_boolean_fields_reject_non_booleans(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(write(tmp_path, "enforce_admins: yes-please\n"))


def test_malformed_yaml_is_rejected(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(write(tmp_path, "required_reviews: [unclosed\n"))


def test_top_level_must_be_a_mapping(tmp_path):
    with pytest.raises(PolicyError):
        load_policy(write(tmp_path, "- just\n- a list\n"))


def test_unknown_keys_are_ignored_not_fatal(tmp_path):
    policy = load_policy(write(tmp_path, "required_reviews: 1\nfuture_option: true\n"))
    assert policy.required_reviews == 1


def test_check_names_are_deduplicated_and_sorted(tmp_path):
    policy = load_policy(write(tmp_path, "required_checks:\n  - test\n  - lint\n  - test\n"))
    assert policy.required_checks == ("lint", "test")


def test_policy_serialises_for_reporting(tmp_path):
    policy = load_policy(write(tmp_path, MINIMAL))
    payload = policy.as_dict()
    assert payload["required_checks"] == ["lint", "test"]
    assert payload["required_reviews"] == 2


def test_empty_default_policy_exists():
    assert Policy().required_reviews == 0


def test_as_dict_omits_keys_the_policy_never_declared(tmp_path):
    # An absent key means "not mentioned". Flattening it to False would make a
    # policy file that never mentions force pushes start failing builds that
    # allow them.
    policy = load_policy(write(tmp_path, "required_reviews: 1\n"))
    payload = policy.as_dict()
    assert payload == {"required_reviews": 1}


def test_policy_omitting_allow_force_pushes_does_not_flag_an_open_branch(tmp_path):
    from ci_gate_watch.drift import audit

    policy = load_policy(write(tmp_path, "required_reviews: 1\n"))
    actual = {"allow_force_pushes": {"enabled": True}}
    findings = audit(policy.as_dict(), actual)
    # required_reviews: 1 with no review rule is a legitimate finding; what
    # must not appear is any force-push finding.
    assert [f.drift_type for f in findings] == ["required_reviews_missing"]


def test_policy_declaring_allow_force_pushes_false_does_flag_an_open_branch(tmp_path):
    from ci_gate_watch.drift import audit

    policy = load_policy(write(tmp_path, "allow_force_pushes: false\n"))
    actual = {"allow_force_pushes": {"enabled": True}}
    findings = audit(policy.as_dict(), actual)
    assert [f.drift_type for f in findings] == ["force_pushes_allowed"]


def test_policy_declaring_allow_deletions_false_does_flag_an_open_branch(tmp_path):
    from ci_gate_watch.drift import audit

    policy = load_policy(write(tmp_path, "allow_deletions: false\n"))
    actual = {"allow_deletions": {"enabled": True}}
    assert [f.drift_type for f in audit(policy.as_dict(), actual)] == ["deletions_allowed"]


def test_end_to_end_policy_to_findings(tmp_path):
    from ci_gate_watch.drift import audit

    path = write(
        tmp_path,
        "required_reviews: 2\nrequired_checks:\n  - lint\nenforce_admins: true\n",
    )
    policy = load_policy(path)
    actual = {
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "required_status_checks": {"strict": True, "contexts": ["lint"]},
        "enforce_admins": {"enabled": False},
    }
    findings = audit(policy.as_dict(), actual)
    assert sorted(f.drift_type for f in findings) == [
        "enforce_admins_disabled",
        "required_reviews_low",
    ]
