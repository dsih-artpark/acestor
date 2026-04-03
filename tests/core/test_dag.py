"""Tests for acestor.core.dag."""

import pytest

from acestor.core.step import PipelineStep
from acestor.core.dag import PipelineDAG


def _step(name: str) -> PipelineStep:
    return PipelineStep(name=name, impl=object())


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_linear_dag():
    a, b, c = _step("a"), _step("b"), _step("c")
    a >> b >> c
    dag = PipelineDAG.from_steps([a, b, c])
    assert set(dag.edges["a"]) == {"b"}
    assert set(dag.edges["b"]) == {"c"}
    assert set(dag.edges["c"]) == set()


def test_fork_dag():
    a, b, c = _step("a"), _step("b"), _step("c")
    a >> [b, c]
    dag = PipelineDAG.from_steps([a, b, c])
    assert dag.edges["a"] == {"b", "c"}


def test_join_dag():
    a, b, c = _step("a"), _step("b"), _step("c")
    a >> c
    b >> c
    dag = PipelineDAG.from_steps([a, b, c])
    assert dag.parents("c") == {"a", "b"}


def test_single_step_dag():
    a = _step("a")
    dag = PipelineDAG.from_steps([a])
    assert dag.topological_order() == ["a"]


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_duplicate_names_raise():
    a1, a2 = _step("a"), _step("a")
    with pytest.raises(ValueError, match="unique"):
        PipelineDAG.from_steps([a1, a2])


def test_missing_step_in_list_raises():
    a, b = _step("a"), _step("b")
    a >> b
    with pytest.raises(ValueError, match="not included"):
        PipelineDAG.from_steps([a])  # b is missing


def test_cycle_raises():
    a, b, c = _step("a"), _step("b"), _step("c")
    a >> b >> c
    # manually wire a cycle
    c.downstream.add(a)
    a.upstream.add(c)
    with pytest.raises(ValueError, match="cycle"):
        PipelineDAG.from_steps([a, b, c])


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_parents_and_children():
    a, b, c = _step("a"), _step("b"), _step("c")
    a >> b >> c
    dag = PipelineDAG.from_steps([a, b, c])
    assert dag.parents("b") == {"a"}
    assert dag.children("b") == {"c"}
    assert dag.parents("a") == set()
    assert dag.children("c") == set()


def test_topological_order_respects_dependencies():
    a, b, c = _step("a"), _step("b"), _step("c")
    a >> b >> c
    dag = PipelineDAG.from_steps([a, b, c])
    order = dag.topological_order()
    assert order.index("a") < order.index("b")
    assert order.index("b") < order.index("c")


def test_topological_order_fork():
    a, b, c, d = _step("a"), _step("b"), _step("c"), _step("d")
    a >> [b, c]
    [b, c] >> d
    dag = PipelineDAG.from_steps([a, b, c, d])
    order = dag.topological_order()
    assert len(order) == 4
    assert order[0] == "a"
    assert order[-1] == "d"
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_empty_dag():
    dag = PipelineDAG.from_steps([])
    assert dag.steps == []
    assert dag.edges == {}


def test_children_nonexistent_name():
    a = _step("a")
    dag = PipelineDAG.from_steps([a])
    assert dag.children("nonexistent") == set()


def test_parents_nonexistent_name():
    a = _step("a")
    dag = PipelineDAG.from_steps([a])
    assert dag.parents("nonexistent") == set()
