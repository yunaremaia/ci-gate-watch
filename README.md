# ci-gate-watch

Detect when a repository's CI/CD validation gates drift from declared policy — required reviews, branch protection, required checks, CI enforcement.

> **Status: 0.1.0.** Policy-vs-branch-protection auditing and workflow-vs-required-check auditing are implemented and tested. Org-wide scanning, App ID drift and lookback-window checks (issues #5, #6 in part) are **not** implemented — see [Limitations](#limitations).

## Problem

Branch protection is the last line of defence, and it is edited by hand, through a UI, by people in a hurry:

- A policy says 2 required reviews; someone sets it to 1 "just to unblock a hotfix" and nobody notices.
- A required check is renamed in a workflow. Branch protection still requires the old name, which no run ever produces — the gate blocks nothing while looking configured.
- A matrix dimension is added, so the check becomes `test (3.11)`, `test (3.12)`, `test (3.13)`, and the required `test` context now matches nothing.
- `enforce_admins` is off, so the gates apply to everyone except the people who deploy.

Nothing reports any of this. `gh api` can tell you what the configuration *is*; nothing tells you it no longer matches what you intended.

## Solution

`ci-gate-watch` compares a declared policy against the real configuration, and fails with a non-zero exit code when they disagree.

| Command | What it compares | Needs a token |
|---------|------------------|----------------|
| `ci-gate-watch checks` | Required checks in the policy vs the workflows in a local checkout | **no** |
| `ci-gate-watch check-contexts` | Lists every check context the workflows produce | **no** |
| `ci-gate-watch audit --repo OWNER/NAME` | Policy vs live branch protection | yes (`gh auth`) |

The offline commands are the ones that run on every PR: a renamed job is caught by `ci-gate-watch checks` **before merge**, with no credentials. `audit` catches someone weakening protection on the live branch.

### Install

ci-gate-watch is not on PyPI yet. Install it from the repository:

```bash
pip install git+https://github.com/yunaremaia/ci-gate-watch.git
```

`audit` shells out to the GitHub CLI, so it also needs [`gh`](https://cli.github.com) authenticated (`gh auth login`). The offline commands need neither.

## Usage

### The policy file

`.ci-gate-policy.yml` in the repository root:

```yaml
required_reviews: 2
required_checks:
  - lint
  - test (3.11)
enforce_admins: true
allow_force_pushes: false
allow_deletions: false
required_linear_history: true
required_signed_commits: false
```

| Key | Meaning | Default |
|-----|---------|---------|
| `required_reviews` | Minimum approving reviews | `0` |
| `required_checks` | Required status check context names | none |
| `enforce_admins` | Admins must obey the gates | `false` |
| `required_linear_history` | Squash/rebase history required | `false` |
| `required_signed_commits` | Signed commits required | `false` |
| `require_code_owner_reviews` | Code owner review required | `false` |
| `allow_force_pushes` | **Policy value is the requirement**: `false` means force pushes must be blocked | not enforced unless declared |
| `allow_deletions` | Same, for branch deletion | not enforced unless declared |

**An absent key means "this policy does not care".** It is never treated as a requirement, so a policy file that never mentions force pushes will not start failing a branch that allows them. To require a field, write it down. A wrong *type* (`required_checks: lint` instead of a list) is a hard error, because silently accepting it would produce a finding indistinguishable from a real one.

### Offline: required checks vs workflows

```bash
# What check contexts do these workflows actually produce?
ci-gate-watch check-contexts
# lint
# test (3.11)
# test (3.13)

# Compare them against the policy (exit 1 on drift)
ci-gate-watch checks
```

```
$ ci-gate-watch checks
Auditing .

  [  error] required_check_orphaned
            required check 'typecheck' is not produced by any workflow in .github/workflows/
            expected: 'a workflow job'  actual: None

1 finding(s): 1 error(s), 0 warning(s)
```

Contexts are mapped the way GitHub names them:

| Workflow | Context(s) it produces |
|----------|------------------------|
| `jobs.lint` | `lint` |
| `jobs.test` with `matrix.python-version: ["3.11", "3.13"]` | `test (3.11)`, `test (3.13)` |
| `jobs.test` with `name: unit tests` | `unit tests` |
| multiple matrix axes | one context per combination, comma-joined |

This catches the rename drift that matters most: requiring plain `test` for a matrix job gates nothing, because GitHub reports `test (3.11)`.

### Online: policy vs live branch protection

```bash
ci-gate-watch audit --repo yunaremaia/env-drift
ci-gate-watch audit --repo yunaremaia/env-drift --branch main --format json
ci-gate-watch audit --repo yunaremaia/env-drift --with-workflows   # also check local workflows
```

Drift types:

| Drift type | Severity | Meaning |
|------------|----------|---------|
| `protection_missing` | error | The branch has no protection at all |
| `required_reviews_missing` | error | Reviews are required but no review rule exists |
| `required_reviews_low` | error | Fewer reviews configured than the policy requires |
| `required_check_missing` | error | A policy-required check is not a required status check |
| `undeclared_check` | warning | A required check the policy never mentions |
| `enforce_admins_disabled` | error | Admins bypass the gates |
| `linear_history_disabled` | error | Policy requires linear history |
| `signed_commits_disabled` | error | Policy requires signed commits |
| `force_pushes_allowed` | error | Branch allows force pushes, policy forbids them |
| `deletions_allowed` | error | Branch allows deletions, policy forbids them |
| `required_check_orphaned` | error | Required check no workflow produces |
| `required_check_not_on_pull_request` | error | The check's workflow never runs on PRs |

One finding per fact. A missing review requirement is reported as `required_reviews_missing`, not as both "missing" and "lower than 2" — a reviewer who sees two findings for one problem learns to ignore one.

### Output and exit codes

`--format text` (default), `json` (CI) or `sarif` (GitHub code scanning). SARIF results point at `.ci-gate-policy.yml`, because every finding is a statement about that file.

- `0` — no drift at or above the threshold
- `1` — drift found
- `2` — configuration or usage error (bad policy, bad repo slug, GitHub unreachable)

`--fail-on warning` fails on warnings too; `--fail-on none` always exits `0`.

### Library use

```python
from ci_gate_watch import GitHubClient, audit, load_policy

policy = load_policy(".ci-gate-policy.yml")
actual = GitHubClient().fetch_protection("owner/name", "main")
for finding in audit(policy.as_dict(), actual):
    print(finding.severity, finding.drift_type, finding.message)
```

`audit()` is a pure function: policy dict in, protection payload out. Nothing else touches the network, so the whole detection surface is testable without a credential.

## Limitations

- **Single repository.** `audit` takes one `--repo`. Org-wide scanning is not implemented.
- **No lookback window.** Issue #5 asks whether each required check matched a recent successful run (stale contexts, App ID drift). That needs the check-runs API and is not implemented — `checks` is the offline approximation, and it compares against workflow definitions rather than run history.
- **Workflow triggers are read statically.** `required_check_not_on_pull_request` inspects the `on:` block. A job gated by `if:` conditions at runtime can still skip on PRs.
- **Matrix `include`/`exclude` are not expanded.** Contexts are the cartesian product of the declared axes, so a hand-written `include` producing an unusual name is reported as orphaned.
- **No `--fix`.** Issue #1 sketches an `audit --fix` mode; this reports only.

## Roadmap

- [x] Policy loading and validation from `.ci-gate-policy.yml`
- [x] Branch protection drift detection (reviews, checks, enforce_admins, force pushes, deletions, linear history, signed commits)
- [x] Unprotected-branch detection
- [x] Workflow parsing and required-check context mapping (including matrix expansion)
- [x] Offline `checks` command — no token needed
- [x] `audit --repo` via `gh api`
- [x] text / JSON / SARIF output
- [x] CLI with `0` / `1` / `2` exit codes
- [ ] Org-wide scanning
- [ ] Required-check lookback against recent check runs (stale contexts, App ID drift)
- [ ] Matrix `include` / `exclude` expansion
- [ ] `audit --fix` to open issues or apply branch protection changes

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

CI runs the suite plus an offline CLI smoke test on Python 3.11, 3.12 and 3.13.

## Stack

- **Language:** Python 3.11+
- **GitHub access:** `gh api` via subprocess — no PyGithub, no token handling in this project
- **Dependencies:** PyYAML
- **Output:** Terminal, JSON, SARIF 2.1.0
- **Tests:** `pytest`

## License

MIT

## Project Links

- Repository: https://github.com/yunaremaia/ci-gate-watch
- Issues: https://github.com/yunaremaia/ci-gate-watch/issues
