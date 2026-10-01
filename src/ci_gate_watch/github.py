"""GitHub access through the ``gh`` CLI.

Deliberately not PyGithub. Shelling out to ``gh api`` keeps authentication where
the user already has it (``gh auth login``), avoids a second place a token can
leak, and means the whole client is testable by injecting a runner -- no test
needs a network or a credential.

Every failure mode a caller can hit is translated into a :class:`GitHubError`
with a message a human can act on: not authenticated, branch not protected, no
such repository. Silently returning ``{}`` for an unauthenticated call would make
"cannot see the config" indistinguishable from "config is empty", and the audit
would report the second one.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

__all__ = ["BranchNotProtected", "GitHubClient", "GitHubError", "protection_path", "repo_slug"]

Runner = Callable[[list[str]], str]

_NOT_AUTHENTICATED = (
    "authentication",
    "bad credentials",
    "requires authentication",
    "gh auth login",
)

_NOT_PROTECTED = ("branch not protected", "protected branch")


def repo_slug(value: str) -> str:
    """Normalise and validate an ``owner/name`` string."""
    cleaned = (value or "").strip().strip("/")
    parts = cleaned.split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubError(f"repository must be given as owner/name, got {value!r}")
    return f"{parts[0].lower()}/{parts[1].lower()}"


def protection_path(slug: str, branch: str = "main") -> str:
    """The REST endpoint for a branch's protection rules."""
    return f"repos/{repo_slug(slug)}/branches/{quote(branch, safe='')}/protection"


def _default_runner(args: list[str]) -> str:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


class GitHubError(Exception):
    """Any failure while talking to GitHub."""


class BranchNotProtected(GitHubError):
    """The branch has no protection rules at all.

    This is not a tooling failure -- it is the most serious drift a policy audit
    can find, so callers get a distinct type and report it as a finding rather
    than exiting with an error and no findings at all.
    """


class GitHubClient:
    """Reads repository configuration via ``gh api``."""

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner: Runner = runner or _default_runner

    def _api(self, path: str) -> Any:
        args = ["gh", "api", path]
        try:
            output = self.runner(args)
        except FileNotFoundError as exc:
            raise GitHubError(
                "the GitHub CLI (gh) is required but was not found on PATH"
            ) from exc

        if not output or not output.strip():
            raise GitHubError(f"gh returned no output for {path}")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"could not parse gh output for {path}: {output.strip()[:200]}") from exc

        if isinstance(payload, dict) and "message" in payload:
            message = str(payload["message"])
            lowered = message.lower()
            if any(marker in lowered for marker in _NOT_AUTHENTICATED):
                raise GitHubError(f"GitHub rejected the request: {message} (run `gh auth login`)")
            if any(marker in lowered for marker in _NOT_PROTECTED):
                raise BranchNotProtected(message)
            raise GitHubError(f"GitHub returned an error for {path}: {message}")
        return payload

    def fetch_protection(self, slug: str, branch: str = "main") -> dict[str, Any]:
        """Branch protection rules for ``slug``/``branch``."""
        payload = self._api(protection_path(slug, branch))
        if not isinstance(payload, dict):
            raise GitHubError(f"unexpected branch protection payload for {slug}@{branch}")
        return payload

    def fetch_check_runs(self, slug: str, ref: str) -> list[dict[str, Any]]:
        """Check runs for a commit SHA or ref."""
        payload = self._api(f"repos/{repo_slug(slug)}/commits/{quote(ref, safe='')}/check-runs")
        if isinstance(payload, dict):
            return list(payload.get("check_runs") or [])
        return []
