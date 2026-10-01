"""Rendering of audit findings as text, JSON or SARIF.

Findings from the policy audit (:mod:`ci_gate_watch.drift`) and from the
workflow audit (:mod:`ci_gate_watch.workflows`) are rendered through the same
code path -- they expose an identical ``as_dict()``, and the drift type becomes
the SARIF rule id. An auditor that needed two output paths would eventually
print the workflow findings in a format CI cannot parse.

Nothing here talks to the network: rendering takes already-computed findings, so
every format is testable without a token.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .policy import POLICY_FILENAME

__all__ = ["FORMATS", "build_payload", "render", "render_json", "render_sarif", "render_text"]

FORMATS = ("text", "json", "sarif")

_SARIF_SCHEMA = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)

_HELP: dict[str, str] = {
    "required_reviews_missing": "The policy requires approving reviews but none are configured.",
    "required_reviews_low": "Fewer approving reviews are configured than the policy requires.",
    "required_check_missing": "A check the policy requires is not a required status check.",
    "undeclared_check": "A required status check is not mentioned in the policy.",
    "enforce_admins_disabled": "Admins are exempt from the gates the policy requires.",
    "required_linear_history_disabled": "The policy requires a linear history; the branch does not.",
    "required_signed_commits_disabled": "The policy requires signed commits; the branch does not.",
    "linear_history_disabled": "The policy requires a linear history; the branch does not.",
    "signed_commits_disabled": "The policy requires signed commits; the branch does not.",
    "force_pushes_allowed": "The branch allows force pushes although the policy forbids them.",
    "deletions_allowed": "The branch allows deletions although the policy forbids them.",
    "required_check_orphaned": "A required check is not produced by any workflow.",
    "required_check_not_on_pull_request": "A required check's workflow does not run on pull_request.",
}


class _Finding(Protocol):
    drift_type: str
    severity: str
    message: str
    key: str | None

    def as_dict(self) -> dict[str, Any]: ...


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("ci-gate-watch")
    except PackageNotFoundError:  # running from a source checkout
        return "0.1.0"


def summarize(findings: list[_Finding]) -> dict[str, object]:
    """Count findings by severity and by drift type."""
    by_type: dict[str, int] = {}
    for finding in findings:
        by_type[finding.drift_type] = by_type.get(finding.drift_type, 0) + 1
    return {
        "total": len(findings),
        "errors": sum(1 for f in findings if f.severity == "error"),
        "warnings": sum(1 for f in findings if f.severity == "warning"),
        "by_type": dict(sorted(by_type.items())),
    }


def build_payload(
    findings: list[_Finding],
    repo: str = "",
    branch: str = "",
    policy: dict[str, Any] | None = None,
    workflows: list[Any] | None = None,
) -> dict[str, object]:
    """The canonical JSON payload; text and SARIF are derived from it."""
    return {
        "tool": {"name": "ci-gate-watch", "version": _version()},
        "repo": repo,
        "branch": branch,
        "policy": dict(policy or {}),
        "workflows": [workflow.as_dict() for workflow in (workflows or [])],
        "summary": summarize(findings),
        "results": [finding.as_dict() for finding in findings],
    }


def render_text(
    findings: list[_Finding],
    repo: str = "",
    branch: str = "",
    policy: dict[str, Any] | None = None,
    workflows: list[Any] | None = None,
) -> str:
    """Human-readable audit summary, one block per finding."""
    header = f"Auditing {repo}" + (f" (branch {branch})" if branch else "")
    if not findings:
        return f"{header}\nNo drift detected\n"

    lines = [header, ""]
    for finding in findings:
        lines.append(f"  [{finding.severity:>7}] {finding.drift_type}")
        lines.append(f"            {finding.message}")
        payload = finding.as_dict()
        if payload.get("expected") is not None or payload.get("actual") is not None:
            lines.append(
                f"            expected: {payload.get('expected')!r}  actual: {payload.get('actual')!r}"
            )

    summary = summarize(findings)
    lines.append("")
    lines.append(
        f"{summary['total']} finding(s): {summary['errors']} error(s), "
        f"{summary['warnings']} warning(s)"
    )
    return "\n".join(lines) + "\n"


def render_json(
    findings: list[_Finding],
    repo: str = "",
    branch: str = "",
    policy: dict[str, Any] | None = None,
    workflows: list[Any] | None = None,
) -> str:
    """Machine-readable payload for CI."""
    payload = build_payload(findings, repo, branch, policy, workflows)
    return json.dumps(payload, indent=2) + "\n"


def render_sarif(
    findings: list[_Finding],
    repo: str = "",
    branch: str = "",
    policy: dict[str, Any] | None = None,
    workflows: list[Any] | None = None,
) -> str:
    """SARIF 2.1.0 for GitHub code scanning."""
    rules: list[dict[str, Any]] = [
        {
            "id": drift_type,
            "name": drift_type,
            "shortDescription": {"text": _HELP.get(drift_type, drift_type)},
            "defaultConfiguration": {"level": "warning"},
        }
        for drift_type in sorted({finding.drift_type for finding in findings})
    ]

    results: list[dict[str, Any]] = []
    for finding in findings:
        result: dict[str, Any] = {
            "ruleId": finding.drift_type,
            "level": finding.severity,
            "message": {"text": finding.message},
            "properties": {"key": finding.as_dict().get("key")},
        }
        if finding.key:
            # Every finding is a statement about the policy file, so that is
            # what a reviewer in code scanning needs to open.
            result["locations"] = [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": POLICY_FILENAME,
                            "uriBaseId": "%SRCROOT%",
                        },
                        "region": {"startLine": 1},
                    }
                }
            ]
        results.append(result)

    document = {
        "$schema": _SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ci-gate-watch",
                        "version": _version(),
                        "informationUri": "https://github.com/yunaremaia/ci-gate-watch",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(document, indent=2) + "\n"


def render(
    findings: list[_Finding],
    fmt: str = "text",
    repo: str = "",
    branch: str = "",
    policy: dict[str, Any] | None = None,
    workflows: list[Any] | None = None,
) -> str:
    """Render ``findings`` in ``fmt`` (text, json or sarif)."""
    renderers = {"text": render_text, "json": render_json, "sarif": render_sarif}
    try:
        renderer = renderers[fmt]
    except KeyError:
        raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}") from None
    return renderer(findings, repo, branch, policy, workflows)
