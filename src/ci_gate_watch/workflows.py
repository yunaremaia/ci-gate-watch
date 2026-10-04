"""Workflow parsing and workflow/check-context drift.

Branch protection requires *check context names*, and workflows *produce* those
names. When a job is renamed, a matrix dimension is added, or a trigger moves to
push-only, the required checks and the workflows silently stop agreeing -- and a
required check that no PR ever produces is a gate that blocks nothing.

Mapping a job to its context follows GitHub's own rule:

- a job with an explicit ``name:`` uses that string
- a job with a ``strategy.matrix`` produces one check per matrix combination,
  named ``<job> (<value>)`` for every ``include``-free axis value, comma-joined
- otherwise the context is the job key

The drift check is offline and pure: it reads ``.github/workflows/*.yml`` from
disk, so it can be run and tested without a token, and it is the check most
likely to catch a rename before merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Job",
    "Workflow",
    "WorkflowDrift",
    "check_contexts_for",
    "load_workflows",
    "parse_workflow",
    "workflow_drift",
]

WORKFLOW_DIR = Path(".github") / "workflows"


@dataclass(frozen=True)
class Job:
    """A workflow job and the check contexts it produces."""

    name: str
    contexts: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "contexts": list(self.contexts)}


@dataclass(frozen=True)
class Workflow:
    """One parsed workflow file."""

    name: str
    path: Path
    triggers: frozenset[str]
    jobs: tuple[Job, ...]

    def job(self, name: str) -> Job | None:
        for candidate in self.jobs:
            if candidate.name == name:
                return candidate
        return None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": str(self.path),
            "triggers": sorted(self.triggers),
            "jobs": [job.as_dict() for job in self.jobs],
        }


@dataclass(frozen=True)
class WorkflowDrift:
    """A required check that the workflows do not agree with."""

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


def _triggers(data: dict[str, Any]) -> frozenset[str]:
    raw = data.get("on", data.get(True))  # `on:` parses as the boolean True in YAML 1.1
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        return frozenset({raw})
    if isinstance(raw, list):
        return frozenset(str(item) for item in raw)
    if isinstance(raw, dict):
        return frozenset(str(item) for item in raw)
    return frozenset()


def _matrix_values(job_data: dict[str, Any]) -> list[str]:
    """Cartesian product of the matrix axes, in declaration order.

    A `strategy` that is not a mapping (a scalar or a list) means the workflow has
    no matrix at all — GitHub rejects those shapes, so there is nothing to expand.
    Validating here rather than catching AttributeError in the caller keeps a real
    bug in the parser from being silently turned into a skipped file.
    """
    strategy = job_data.get("strategy")
    if not isinstance(strategy, dict):
        return []
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return []
    axes = {
        key: [str(value) for value in values]
        for key, values in matrix.items()
        if key != "include" and isinstance(values, list)
    }
    if not axes:
        return []

    combinations: list[list[str]] = [[]]
    for values in axes.values():
        combinations = [existing + [value] for existing in combinations for value in values]
    return [", ".join(parts) for parts in combinations]


def _contexts_for(job_key: str, job_data: dict[str, Any]) -> tuple[str, ...]:
    explicit = job_data.get("name")
    if isinstance(explicit, str) and explicit.strip():
        return (explicit.strip(),)
    combos = _matrix_values(job_data)
    if combos:
        return tuple(f"{job_key} ({combo})" for combo in combos)
    return (job_key,)


def parse_workflow(text: str, filename: str) -> Workflow:
    """Parse one workflow file's YAML into jobs and check contexts."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise TypeError(f"{filename}: workflow must be a mapping")

    jobs_data = data.get("jobs") or {}
    if not isinstance(jobs_data, dict):
        raise TypeError(f"{filename}: jobs must be a mapping")

    jobs = tuple(
        Job(name=str(key), contexts=_contexts_for(str(key), value or {}))
        for key, value in jobs_data.items()
        if isinstance(value, dict)
    )
    name = data.get("name")
    return Workflow(
        name=str(name).strip() if isinstance(name, str) and name.strip() else Path(filename).stem,
        path=Path(filename),
        triggers=_triggers(data),
        jobs=jobs,
    )


def load_workflows(root: Path) -> list[Workflow]:
    """Load every workflow under ``.github/workflows``; skip unparseable files.

    A broken workflow file is skipped rather than fatal: the point of this check
    is to report drift, and refusing to run because one file has a typo would
    hide drift in every other file.
    """
    directory = Path(root) / WORKFLOW_DIR
    if not directory.is_dir():
        return []

    workflows: list[Workflow] = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".yml", ".yaml") or not path.is_file():
            continue
        try:
            workflows.append(parse_workflow(path.read_text(encoding="utf-8"), path))
        except (yaml.YAMLError, TypeError, ValueError, OSError, UnicodeDecodeError, AttributeError):
            continue
    return workflows


def check_contexts_for(workflows: list[Workflow]) -> set[str]:
    """Every check context any workflow can produce."""
    return {context for workflow in workflows for job in workflow.jobs for context in job.contexts}


def _produces(workflows: list[Workflow], context: str) -> tuple[Workflow, Job] | None:
    for workflow in workflows:
        for job in workflow.jobs:
            if context in job.contexts:
                return workflow, job
    return None


def workflow_drift(
    workflows: list[Workflow],
    required_checks: tuple[str, ...] | list[str],
) -> list[WorkflowDrift]:
    """Compare required check contexts against the workflows on disk."""
    findings: list[WorkflowDrift] = []
    for context in required_checks:
        found = _produces(workflows, context)
        if found is None:
            findings.append(
                WorkflowDrift(
                    drift_type="required_check_orphaned",
                    severity="error",
                    message=(
                        f"required check {context!r} is not produced by any workflow in "
                        f"{WORKFLOW_DIR.as_posix()}/"
                    ),
                    key=context,
                    expected="a workflow job",
                    actual=None,
                )
            )
            continue

        workflow, _job = found
        if "pull_request" not in workflow.triggers:
            findings.append(
                WorkflowDrift(
                    drift_type="required_check_not_on_pull_request",
                    severity="error",
                    message=(
                        f"required check {context!r} comes from workflow {workflow.name!r}, "
                        "which does not run on pull_request"
                    ),
                    key=context,
                    expected="pull_request",
                    actual=sorted(workflow.triggers) or None,
                )
            )
    return findings
