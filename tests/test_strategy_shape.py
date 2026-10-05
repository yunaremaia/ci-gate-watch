"""Regression tests for non-mapping `strategy:` and `strategy.matrix:` values.

A workflow with a scalar or list `strategy` used to raise AttributeError out of
`_matrix_values`, which aborted the whole audit with exit 1 — the same code as a
real drift failure, so `audit --with-workflows` threw away every finding it had
already computed and reported zero.

`_matrix_values` reads `strategy` as a mapping twice (`.get("strategy")` then
`.get("matrix")`) and only validated `matrix` afterwards. The fix belongs there,
not in the caller's exception tuple: `load_workflows` must skip unparseable files
and nothing else, so an AttributeError anywhere else in the parser has to stay
visible. These tests double as the guard for that — if the shape check is ever
removed and AttributeError re-added to the caller instead, the parametrized cases
below fail with the original traceback.

`tests/test_workflows.py::test_non_mapping_strategy_keeps_the_workflow` covers the
same three shapes through `load_workflows`.
"""

import pytest

from ci_gate_watch.workflows import _matrix_values, load_workflows, parse_workflow

# --- the root cause: _matrix_values must not assume strategy is a mapping ---


@pytest.mark.parametrize(
    "job_data",
    [
        {"strategy": "fast"},              # scalar
        {"strategy": ["a", "b"]},          # list
        {"strategy": {"matrix": 3.11}},    # scalar matrix
        {"strategy": {"matrix": ["a"]}},   # list matrix, not a mapping of axes
    ],
)
def test_matrix_values_ignores_a_strategy_that_is_not_a_mapping(job_data):
    """A non-mapping strategy means "no matrix", not an exception.

    GitHub rejects these shapes, so the right answer is the same as a workflow
    with no strategy at all: no matrix axes, no crash.
    """
    assert _matrix_values(job_data) == []


def test_matrix_values_still_expands_a_real_matrix():
    """Guard the other direction: a valid matrix must keep working.

    Contexts are rendered as the joined axis values, not `key=value` pairs — the
    job key already identifies the matrix.
    """
    job_data = {"strategy": {"matrix": {"os": ["ubuntu", "mac"], "py": ["3.11"]}}}
    assert _matrix_values(job_data) == ["ubuntu, 3.11", "mac, 3.11"]


# --- the reported symptom: one malformed file must not hide every other finding ---


def test_a_malformed_strategy_keeps_the_job_and_does_not_hide_the_other_file(tmp_path):
    """The bug aborted the whole audit, discarding findings it had already computed.

    The fix is not to drop the file. A scalar `strategy` makes the matrix
    unexpandable but leaves the job itself well-formed, so the job must still be
    reported — dropping the file would hide the very check that job needs. What
    must never happen again is one bad file discarding every other workflow.
    """
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "a-good.yml").write_text(
        "name: Good\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
        encoding="utf-8",
    )
    (wf_dir / "b-broken.yml").write_text(
        "name: Broken\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    strategy: fast\n    steps: []\n",
        encoding="utf-8",
    )

    loaded = load_workflows(tmp_path)

    assert sorted(w.name for w in loaded) == ["Broken", "Good"], (
        "a bad strategy must not discard the other workflows"
    )
    broken = next(w for w in loaded if w.name == "Broken")
    assert [j.name for j in broken.jobs] == ["test"], (
        "the job survives; only the matrix expansion is skipped"
    )
    assert broken.jobs[0].contexts == ("test",), (
        "with no matrix the context is the bare job key, same as a job with no strategy"
    )


def test_parse_workflow_still_raises_type_error_on_a_non_mapping_jobs():
    """Malformed *job* data keeps raising; only strategy shape is tolerated.

    parse_workflow already validates that jobs is a mapping. That contract must
    not silently widen into "accept anything" now that strategy is checked with
    isinstance too — the two validations are independent.
    """
    bad = "name: W\non: [push]\njobs: not-a-mapping\n"
    with pytest.raises(TypeError):
        parse_workflow(bad, "w.yml")