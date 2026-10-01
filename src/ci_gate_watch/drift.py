"""Drift detection: declared policy versus actual GitHub configuration.

:func:`audit` is a pure function -- policy dict in, actual protection dict out --
so it can be tested exhaustively without a network call or a token. The GitHub
client is a separate module whose only job is to produce that dict.

One finding per *fact*, never two. A missing review requirement is reported as
``required_reviews_missing``, not as "missing" and "lower than 2", because a
reviewer who sees two findings for one problem learns to ignore one of them.

Severity is uniform on purpose: anything that weakens a gate the policy declares
is an ``error``, while an actual check the policy never mentioned is a
``warning``. Silently-required extra gates are worth knowing about; they are not
policy violations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Drift", "audit"]


@dataclass(frozen=True)
class Drift:
    """One difference between the declared policy and the actual configuration."""

    drift_type: str
    severity: str
    message: str
    key: str | None = None
    expected: Any = None
    actual: Any = None

    def as_dict(self) -> dict[str, object]:
        return {
            "drift_type": self.drift_type,
            "severity": self.severity,
            "message": self.message,
            "key": self.key,
            "expected": self.expected,
            "actual": self.actual,
        }


def _enabled(block: Any) -> bool:
    """Interpret a GitHub protection block as a boolean.

    GitHub omits a block entirely when the feature is off, and represents "on"
    as ``{"enabled": true}``. Both mean the same thing to a policy.
    """
    if block is None:
        return False
    if isinstance(block, dict):
        return bool(block.get("enabled", False))
    return bool(block)


def _actual_reviews(actual: dict[str, Any]) -> int:
    block = actual.get("required_pull_request_reviews")
    if not isinstance(block, dict):
        return 0
    return int(block.get("required_approving_review_count") or 0)


def _actual_contexts(actual: dict[str, Any]) -> list[str]:
    block = actual.get("required_status_checks")
    if not isinstance(block, dict):
        return []
    return list(block.get("contexts") or [])


def _reviews_drift(policy: dict[str, Any], actual: dict[str, Any]) -> list[Drift]:
    required = int(policy.get("required_reviews") or 0)
    if required <= 0:
        return []

    block = actual.get("required_pull_request_reviews")
    if block is None:
        return [
            Drift(
                drift_type="required_reviews_missing",
                severity="error",
                message=f"policy requires {required} approving review(s) but no review rule is configured",
                expected=required,
                actual=0,
            )
        ]

    have = _actual_reviews(actual)
    if have >= required:
        return []
    return [
        Drift(
            drift_type="required_reviews_low",
            severity="error",
            message=f"policy requires {required} approving review(s) but only {have} configured",
            key="required_reviews",
            expected=required,
            actual=have,
        )
    ]


def _checks_drift(policy: dict[str, Any], actual: dict[str, Any]) -> list[Drift]:
    required = [str(name) for name in (policy.get("required_checks") or [])]
    configured = _actual_contexts(actual)
    findings: list[Drift] = []

    for name in required:
        if name not in configured:
            findings.append(
                Drift(
                    drift_type="required_check_missing",
                    severity="error",
                    message=f"policy requires check {name!r} but it is not a required status check",
                    key=name,
                    expected="required",
                    actual="not required",
                )
            )

    for name in configured:
        if name not in required:
            findings.append(
                Drift(
                    drift_type="undeclared_check",
                    severity="warning",
                    message=(
                        f"check {name!r} is required on the branch but the policy does not "
                        "mention it"
                    ),
                    key=name,
                    expected=None,
                    actual="required",
                )
            )
    return findings


def _flag_drift(policy: dict[str, Any], actual: dict[str, Any]) -> list[Drift]:
    """Boolean gate pairs, expressed as (policy key, config key, drift type)."""
    pairs = (
        ("enforce_admins", "enforce_admins", "enforce_admins_disabled"),
        ("required_linear_history", "required_linear_history", "linear_history_disabled"),
        ("required_signed_commits", "required_signatures", "signed_commits_disabled"),
    )
    findings = []
    for policy_key, config_key, drift_type in pairs:
        if not policy.get(policy_key):
            continue
        if _enabled(actual.get(config_key)):
            continue
        findings.append(
            Drift(
                drift_type=drift_type,
                severity="error",
                message=(
                    f"policy sets {policy_key}: true but {config_key} is not enabled on the branch"
                ),
                key=policy_key,
                expected=True,
                actual=False,
            )
        )

    for policy_key, config_key, drift_type in (
        ("allow_force_pushes", "allow_force_pushes", "force_pushes_allowed"),
        ("allow_deletions", "allow_deletions", "deletions_allowed"),
    ):
        # These two are inverted: the policy value is the requirement, so
        # `false` is the enforcing setting. Only a policy that *declares*
        # `false` is audited -- an absent key means "not mentioned", and
        # treating it as `false` would manufacture drift for every repository
        # whose policy file simply omits the field.
        if policy_key not in policy or policy.get(policy_key) is not False:
            continue
        if not _enabled(actual.get(config_key)):
            continue
        findings.append(
            Drift(
                drift_type=drift_type,
                severity="error",
                message=(
                    f"policy sets {policy_key}: false but the branch allows "
                    f"{'force pushes' if 'force' in config_key else 'deletions'}"
                ),
                key=policy_key,
                expected=False,
                actual=True,
            )
        )
    return findings


def audit(policy: dict[str, Any], actual: dict[str, Any]) -> list[Drift]:
    """Compare a policy mapping against actual branch protection.

    ``actual`` is the JSON body of ``GET /repos/{owner}/{repo}/branches/{branch}/
    protection``. Findings are returned errors-first, then alphabetically.
    """
    actual = actual or {}
    findings: list[Drift] = []
    findings.extend(_reviews_drift(policy, actual))
    findings.extend(_checks_drift(policy, actual))
    findings.extend(_flag_drift(policy, actual))
    return sorted(findings, key=lambda f: (0 if f.severity == "error" else 1, f.drift_type))
