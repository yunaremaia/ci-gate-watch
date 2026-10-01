"""ci-gate-watch: detect when CI gates drift from declared policy.

Public surface::

    from ci_gate_watch import GitHubClient, audit, load_policy

    policy = load_policy(".ci-gate-policy.yml")
    actual = GitHubClient().fetch_protection("owner/name", "main")
    findings = audit(policy.as_dict(), actual)

The drift comparison is a pure function, so it can be tested without a token and
reused by anything able to produce the branch protection payload.
"""

__version__ = "0.1.0"

from .drift import Drift, audit
from .github import GitHubClient, GitHubError
from .policy import Policy, PolicyError, load_policy
from .workflows import Workflow, WorkflowDrift, load_workflows, workflow_drift

__all__ = [
    "Drift",
    "GitHubClient",
    "GitHubError",
    "Policy",
    "PolicyError",
    "Workflow",
    "WorkflowDrift",
    "__version__",
    "audit",
    "load_policy",
    "load_workflows",
    "workflow_drift",
]
