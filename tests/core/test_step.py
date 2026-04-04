"""Tests for acestor.core.step."""

from dataclasses import dataclass
from typing import Optional

import pytest

from acestor.core.step import PipelineStep, NoInputs, build_typed_inputs


def _step(name: str) -> PipelineStep:
    return PipelineStep(name=name, impl=object())


# ---------------------------------------------------------------------------
# PipelineStep wiring
# ---------------------------------------------------------------------------


def test_rshift_links_steps():
    a, b = _step("a"), _step("b")
    a >> b
    assert b in a.downstream
    assert a in b.upstream


def test_rshift_chain_returns_last():
    a, b, c = _step("a"), _step("b"), _step("c")
    result = a >> b >> c
    assert result is c


def test_rshift_list_links_all():
    a, b, c = _step("a"), _step("b"), _step("c")
    a >> [b, c]
    assert b in a.downstream
    assert c in a.downstream


def test_rrshift_list_links_all():
    a, b, c = _step("a"), _step("b"), _step("c")
    [a, b] >> c
    assert c in a.downstream
    assert c in b.downstream


def test_rshift_invalid_type_raises():
    a = _step("a")
    with pytest.raises(TypeError):
        a >> "not_a_step"


def test_rshift_list_with_invalid_element_raises():
    a, b = _step("a"), _step("b")
    with pytest.raises(TypeError):
        a >> [b, "bad"]


# ---------------------------------------------------------------------------
# build_typed_inputs
# ---------------------------------------------------------------------------


class _NoInputStep:
    input_type = NoInputs


def test_build_typed_inputs_no_inputs():
    result = build_typed_inputs(_NoInputStep(), {})
    assert isinstance(result, NoInputs)


@dataclass(frozen=True)
class _Inputs:
    step_a: str
    step_b: int


class _TypedStep:
    input_type = _Inputs


def test_build_typed_inputs_populates_fields():
    result = build_typed_inputs(_TypedStep(), {"step_a": "hello", "step_b": 42})
    assert result.step_a == "hello"
    assert result.step_b == 42


def test_build_typed_inputs_missing_required_raises():
    with pytest.raises(ValueError, match="step_b"):
        build_typed_inputs(_TypedStep(), {"step_a": "hello"})


@dataclass(frozen=True)
class _InputsWithDefault:
    step_a: str
    step_b: Optional[int] = None


class _TypedStepWithDefault:
    input_type = _InputsWithDefault


def test_build_typed_inputs_uses_default():
    result = build_typed_inputs(_TypedStepWithDefault(), {"step_a": "hello"})
    assert result.step_a == "hello"
    assert result.step_b is None


def test_build_typed_inputs_no_input_type_attr():
    class _StepNoAttr:
        pass

    result = build_typed_inputs(_StepNoAttr(), {})
    assert isinstance(result, NoInputs)


def test_build_typed_inputs_non_dataclass_input_type():
    class _StepNonDC:
        input_type = str  # not a dataclass

    result = build_typed_inputs(_StepNonDC(), {})
    assert isinstance(result, NoInputs)


def test_rshift_empty_list():
    a = _step("a")
    result = a >> []
    # No downstream links should be created
    assert a.downstream == set()
    # Returns the empty list
    assert result == []


def test_build_typed_inputs_extra_upstream_results_ignored():
    result = build_typed_inputs(
        _TypedStep(), {"step_a": "hello", "step_b": 42, "extra_key": "ignored"}
    )
    assert result.step_a == "hello"
    assert result.step_b == 42
