"""Command line interface for ci-gate-watch.

Commands
--------
``ci-gate-watch audit --repo OWNER/NAME``
    Compare a live repository's branch protection against a policy file.
``ci-gate-watch checks [PATH]``
    Compare the required checks in a policy file against the workflows in a
    local checkout. Entirely offline: no token, no network.
``ci-gate-watch check-contexts [PATH]``
    List every check context the workflows in a checkout can produce.

Exit codes: ``0`` no drift, ``1`` drift found, ``2`` configuration or usage
error (bad policy, bad repo slug, GitHub unreachable).

The split is deliberate. ``checks`` is the check that catches a renamed job
*before* merge and runs in a normal CI job with no credentials. ``audit`` is the
check that catches someone weakening branch protection on the live branch, and
needs ``gh auth``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .drift import Drift
from .drift import audit as audit_policy
from .github import BranchNotProtected, GitHubClient, GitHubError, repo_slug
from .policy import POLICY_FILENAME, PolicyError, load_policy
from .report import FORMATS, render
from .workflows import check_contexts_for, load_workflows, workflow_drift

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

FAIL_LEVELS = ("none", "warning", "error")


class UsageError(Exception):
    """Any condition that must exit with code 2."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ci-gate-watch",
        description="Detect CI gate drift between declared policy and actual configuration.",
    )
    parser.add_argument("--version", action="version", version=f"ci-gate-watch {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit", help="audit a live repository against a policy")
    audit.add_argument("--repo", metavar="OWNER/NAME", required=False, help="repository to audit")
    audit.add_argument("--branch", default="main")
    audit.add_argument(
        "--policy",
        metavar="FILE",
        help=f"policy file (default: {POLICY_FILENAME} in the current directory)",
    )
    audit.add_argument(
        "--with-workflows",
        action="store_true",
        help="also check the local workflows against the policy's required checks",
    )
    audit.add_argument(
        "--no-workflows",
        action="store_true",
        help="skip workflow parsing even when --with-workflows is given",
    )
    audit.add_argument("--format", choices=FORMATS, default="text")
    audit.add_argument("--fail-on", choices=FAIL_LEVELS, default="error")

    checks = subparsers.add_parser(
        "checks", help="check required checks against local workflows (offline)"
    )
    checks.add_argument("path", nargs="?", default=".", help="repository checkout to inspect")
    checks.add_argument("--policy", metavar="FILE", help=f"default: {POLICY_FILENAME}")
    checks.add_argument("--format", choices=FORMATS, default="text")
    checks.add_argument("--fail-on", choices=FAIL_LEVELS, default="error")

    contexts = subparsers.add_parser(
        "check-contexts", help="list every check context the workflows produce"
    )
    contexts.add_argument("path", nargs="?", default=".")
    contexts.add_argument("--format", choices=FORMATS, default="text")

    return parser


def _policy_for(path: Path, explicit: str | None, required: bool = True) -> dict[str, object]:
    """Load the policy for a directory.

    ``required=False`` is for the offline command: with no policy file there are
    no required checks to compare against, which is an empty result rather than
    an error. ``audit`` sets ``required=True`` -- being asked to audit a branch
    against a policy that does not exist is a mistake worth reporting.
    """
    policy_path = Path(explicit) if explicit else path / POLICY_FILENAME
    if not policy_path.is_file():
        if required:
            raise UsageError(f"policy file not found: {policy_path}")
        return {}
    try:
        return load_policy(policy_path).as_dict()
    except PolicyError as exc:
        raise UsageError(str(exc)) from exc


def _fail(findings, threshold: str) -> int:
    if threshold == "none" or not findings:
        return EXIT_OK
    if threshold == "warning":
        return EXIT_DRIFT
    return EXIT_DRIFT if any(f.severity == "error" for f in findings) else EXIT_OK


def _cmd_checks(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if not root.is_dir():
        raise UsageError(f"not a directory: {args.path}")

    policy = _policy_for(root, args.policy, required=False)
    workflows = load_workflows(root)
    findings = workflow_drift(workflows, tuple(policy.get("required_checks") or ()))

    print(
        render(findings, args.format, repo=str(root), policy=policy, workflows=workflows),
        end="",
    )
    return _fail(findings, args.fail_on)


def _cmd_check_contexts(args: argparse.Namespace) -> int:
    root = Path(args.path)
    if not root.is_dir():
        raise UsageError(f"not a directory: {args.path}")
    workflows = load_workflows(root)
    contexts = sorted(check_contexts_for(workflows))

    if args.format == "json":
        import json

        print(
            json.dumps(
                {
                    "workflows": [w.as_dict() for w in workflows],
                    "contexts": contexts,
                },
                indent=2,
            )
        )
        return EXIT_OK

    if not contexts:
        print("no workflow jobs found")
        return EXIT_OK
    for context in contexts:
        print(context)
    return EXIT_OK


def _cmd_audit(args: argparse.Namespace) -> int:
    if not args.repo:
        raise UsageError("--repo OWNER/NAME is required")
    slug = repo_slug(args.repo)

    root = Path.cwd()
    policy = _policy_for(root, args.policy)

    client = GitHubClient()
    findings: list[object] = []
    try:
        actual = client.fetch_protection(slug, args.branch)
    except BranchNotProtected:
        # No protection at all is drift, not a tooling failure: report it as the
        # most serious finding there is rather than exiting with no findings.
        findings.append(
            Drift(
                drift_type="protection_missing",
                severity="error",
                message=(
                    f"branch {args.branch} of {slug} has no branch protection configured, so no "
                    "policy gate can be satisfied"
                ),
                key=args.branch,
                expected="branch protection",
                actual=None,
            )
        )
        actual = {}
    except GitHubError as exc:
        raise UsageError(str(exc)) from exc

    findings.extend(audit_policy(policy, actual))
    workflows = []
    if args.with_workflows and not args.no_workflows:
        workflows = load_workflows(root)
        findings.extend(workflow_drift(workflows, tuple(policy.get("required_checks") or ())))

    print(
        render(findings, args.format, repo=slug, branch=args.branch, policy=policy,
               workflows=workflows),
        end="",
    )
    return _fail(findings, args.fail_on)


_HANDLERS = {
    "audit": _cmd_audit,
    "checks": _cmd_checks,
    "check-contexts": _cmd_check_contexts,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _HANDLERS[args.command](args)
    except (UsageError, GitHubError, PolicyError, ValueError, AttributeError) as exc:
        print(f"ci-gate-watch: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
