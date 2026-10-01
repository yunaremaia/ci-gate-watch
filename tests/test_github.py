"""Tests for the GitHub client.

The client shells out to ``gh api`` rather than importing PyGithub. That keeps
the token handling where GitHub already keeps it (the user's ``gh auth``), avoids
a second credential path, and makes the client trivially testable: the runner is
injectable, so no test touches the network or a real credential.
"""

import json

import pytest

from ci_gate_watch.github import (
    BranchNotProtected,
    GitHubClient,
    GitHubError,
    protection_path,
    repo_slug,
)


class FakeRunner:
    """Stands in for subprocess, recording calls and replaying canned output."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        key = " ".join(args[1:3])
        for pattern, response in self.responses.items():
            if pattern in key:
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected gh call: {args}")


def gh_json(payload):
    return json.dumps(payload)


def test_repo_slug_normalises_the_input():
    assert repo_slug("yunaremaia/env-drift") == "yunaremaia/env-drift"
    assert repo_slug("  Yunaremaia/Env-Drift  ") == "yunaremaia/env-drift"


def test_repo_slug_rejects_a_missing_owner_or_name():
    with pytest.raises(GitHubError):
        repo_slug("env-drift")
    with pytest.raises(GitHubError):
        repo_slug("owner/")


def test_protection_path_is_the_documented_endpoint():
    assert protection_path("yunaremaia/env-drift", "main") == (
        "repos/yunaremaia/env-drift/branches/main/protection"
    )


def test_protection_path_url_encodes_the_branch():
    assert protection_path("o/r", "release/1.0") == "repos/o/r/branches/release%2F1.0/protection"


def test_fetch_protection_parses_the_json_body():
    payload = {"required_pull_request_reviews": {"required_approving_review_count": 2}}
    runner = FakeRunner({"protection": gh_json(payload)})
    client = GitHubClient(runner=runner)
    assert client.fetch_protection("yunaremaia/env-drift", "main") == payload


def test_fetch_protection_defaults_to_the_main_branch():
    runner = FakeRunner({"protection": gh_json({})})
    GitHubClient(runner=runner).fetch_protection("o/r")
    assert "branches/main/protection" in " ".join(runner.calls[0])


def test_unauthenticated_client_is_reported_clearly():
    runner = FakeRunner({"protection": gh_json({"message": "Bad credentials"})})
    client = GitHubClient(runner=runner)
    with pytest.raises(GitHubError) as excinfo:
        client.fetch_protection("o/r", "main")
    assert "Bad credentials" in str(excinfo.value)


def test_missing_protection_is_reported_as_missing_not_broken():
    runner = FakeRunner({"protection": gh_json({"message": "Branch not protected"})})
    with pytest.raises(GitHubError) as excinfo:
        GitHubClient(runner=runner).fetch_protection("o/r", "main")
    assert "not protected" in str(excinfo.value).lower()


def test_invalid_json_is_reported_as_an_error():
    runner = FakeRunner({"protection": "not json at all"})
    with pytest.raises(GitHubError):
        GitHubClient(runner=runner).fetch_protection("o/r", "main")


def test_gh_not_installed_is_reported_clearly():
    def boom(args):
        raise FileNotFoundError("gh")

    with pytest.raises(GitHubError) as excinfo:
        GitHubClient(runner=boom).fetch_protection("o/r", "main")
    assert "gh" in str(excinfo.value).lower()


def test_client_uses_the_documented_api_surface():
    runner = FakeRunner({"protection": gh_json({})})
    GitHubClient(runner=runner).fetch_protection("o/r", "main")
    assert runner.calls[0][0] == "gh"
    assert runner.calls[0][1] == "api"


def test_check_runs_are_fetched_for_a_commit():
    runner = FakeRunner({"commits/abc/check-runs": gh_json({"check_runs": []})})
    GitHubClient(runner=runner).fetch_check_runs("o/r", "abc")
    assert "commits/abc/check-runs" in " ".join(runner.calls[0])


def test_unprotected_branch_raises_a_specific_exception():
    runner = FakeRunner({"protection": gh_json({"message": "Branch not protected"})})
    with pytest.raises(BranchNotProtected):
        GitHubClient(runner=runner).fetch_protection("o/r", "main")


def test_unprotected_branch_is_distinguishable_from_an_auth_failure():
    runner = FakeRunner({"protection": gh_json({"message": "Bad credentials"})})
    with pytest.raises(GitHubError) as excinfo:
        GitHubClient(runner=runner).fetch_protection("o/r", "main")
    assert not isinstance(excinfo.value, BranchNotProtected)
