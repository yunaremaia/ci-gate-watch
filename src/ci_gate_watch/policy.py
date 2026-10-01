"""CI gate policy: the declared intent a repository is audited against.

The policy file is ``.ci-gate-policy.yml`` in the repository root::

    required_reviews: 2
    required_checks:
      - lint
      - test
    enforce_admins: true
    allow_force_pushes: false
    allow_deletions: false
    required_linear_history: true
    required_signed_commits: false

Every field defaults to permissive (0 required reviews, no required checks,
nothing enforced). That direction is deliberate: a missing field means "this
policy does not care", so an absent key can never manufacture drift and fail
someone's CI. To require something, it must be written down.

Unknown keys are ignored so a newer policy file does not break an older CLI,
but a wrong *type* is a hard error. Silently accepting
``required_checks: lint`` (a string) would compare a string against a list and
report "check lint missing" for a check that is actually configured — a false
finding that looks exactly like a real one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

__all__ = ["POLICY_FILENAME", "Policy", "PolicyError", "load_policy"]

POLICY_FILENAME = ".ci-gate-policy.yml"

_INT_FIELDS = ("required_reviews",)
_BOOL_FIELDS = (
    "enforce_admins",
    "allow_force_pushes",
    "allow_deletions",
    "required_linear_history",
    "required_signed_commits",
    "require_code_owner_reviews",
)
_LIST_FIELDS = ("required_checks",)


class PolicyError(Exception):
    """Raised for any unusable policy input."""


@dataclass(frozen=True)
class Policy:
    """Declared CI gate expectations."""

    required_reviews: int = 0
    required_checks: tuple[str, ...] = ()
    enforce_admins: bool = False
    allow_force_pushes: bool = False
    allow_deletions: bool = False
    required_linear_history: bool = False
    required_signed_commits: bool = False
    require_code_owner_reviews: bool = False
    declared: frozenset[str] = field(default_factory=frozenset, compare=False)
    source: str | None = field(default=None, compare=False)

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly view with lists rather than tuples.

        Only keys the policy file actually declared are emitted. The audit
        treats an absent key as "not mentioned", and flattening every default
        into the dict would turn an undeclared ``allow_force_pushes`` into an
        explicit ``false`` -- which the audit reads as a requirement, and would
        fail repositories whose policy file simply omits the field.
        """
        payload: dict[str, object] = {}
        for name in (
            "required_reviews",
            "required_checks",
            "enforce_admins",
            "allow_force_pushes",
            "allow_deletions",
            "required_linear_history",
            "required_signed_commits",
            "require_code_owner_reviews",
        ):
            if name not in self.declared:
                continue
            value = getattr(self, name)
            payload[name] = list(value) if isinstance(value, tuple) else value
        return payload


def _coerce_int(name: str, value: object) -> int:
    # bool is a subclass of int, so it has to be excluded explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"policy key {name!r} must be an integer, got {value!r}")
    if value < 0:
        raise PolicyError(f"policy key {name!r} must not be negative, got {value!r}")
    return value


def _coerce_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise PolicyError(f"policy key {name!r} must be true or false, got {value!r}")
    return value


def _coerce_checks(name: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PolicyError(f"policy key {name!r} must be a list of check names")
    return tuple(sorted({item.strip() for item in value if item.strip()}))


def load_policy(path: str | Path) -> Policy:
    """Read and validate a policy file."""
    path = Path(path)
    if not path.is_file():
        raise PolicyError(f"policy file not found: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise PolicyError(f"cannot read policy {path}: {exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PolicyError(f"policy file {path} must contain a mapping at the top level")

    values: dict[str, object] = {}
    for name in _INT_FIELDS:
        if name in data:
            values[name] = _coerce_int(name, data[name])
    for name in _BOOL_FIELDS:
        if name in data:
            values[name] = _coerce_bool(name, data[name])
    for name in _LIST_FIELDS:
        if name in data:
            values[name] = _coerce_checks(name, data[name])

    return Policy(source=str(path), declared=frozenset(values), **values)  # type: ignore[arg-type]
