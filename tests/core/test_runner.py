"""Tests for acestor.core.runner."""

from dataclasses import dataclass
from typing import Any, Dict


from acestor.core.dag import PipelineDAG
from acestor.core.runner import PipelineRunner, RunResult
from acestor.core.step import PipelineStep, NoInputs, BaseStep


# ---------------------------------------------------------------------------
# Minimal context stub (avoids needing real storage / config)
# ---------------------------------------------------------------------------


class _FakeContext:
    def __init__(self):
        self.run_id = "test-run-001"
        self.logger = None
        self.storages: Dict[str, Any] = {}
        self.run_started_at = None
        self.completed_steps = []
        self.config = {}


# ---------------------------------------------------------------------------
# Minimal step implementations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _OutputA:
    value: int


@dataclass(frozen=True)
class _OutputB:
    value: int


@dataclass(frozen=True)
class _InputsB:
    step_a: _OutputA


class _StepA(BaseStep[NoInputs, _OutputA]):
    input_type = NoInputs

    def run(self, context, inputs):
        return _OutputA(value=1)


class _StepB(BaseStep[_InputsB, _OutputB]):
    input_type = _InputsB

    def run(self, context, inputs):
        return _OutputB(value=inputs.step_a.value + 1)


class _FailingStep(BaseStep[NoInputs, None]):
    input_type = NoInputs

    def run(self, context, inputs):
        raise RuntimeError("intentional failure")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _make_step(name, impl):
    return PipelineStep(name=name, impl=impl)


def test_linear_pipeline_succeeds():
    sa = _make_step("step_a", _StepA())
    sb = _make_step("step_b", _StepB())
    sa >> sb

    dag = PipelineDAG.from_steps([sa, sb])
    runner = PipelineRunner(dag=dag, context=_FakeContext())
    result = runner.run()

    assert result.status == "success"
    assert result.steps["step_a"].value == 1
    assert result.steps["step_b"].value == 2


def test_single_step_pipeline():
    sa = _make_step("step_a", _StepA())
    dag = PipelineDAG.from_steps([sa])
    runner = PipelineRunner(dag=dag, context=_FakeContext())
    result = runner.run()

    assert result.status == "success"
    assert "step_a" in result.steps


def test_failing_step_returns_failed_status():
    sf = _make_step("fail", _FailingStep())
    dag = PipelineDAG.from_steps([sf])
    runner = PipelineRunner(dag=dag, context=_FakeContext())
    result = runner.run()

    assert result.status == "failed"
    assert "RuntimeError" in result.failure_detail


def test_completed_steps_populated():
    sa = _make_step("step_a", _StepA())
    sb = _make_step("step_b", _StepB())
    sa >> sb

    ctx = _FakeContext()
    dag = PipelineDAG.from_steps([sa, sb])
    runner = PipelineRunner(dag=dag, context=ctx)
    runner.run()

    assert "step_a" in ctx.completed_steps
    assert "step_b" in ctx.completed_steps


def test_run_result_fields():
    sa = _make_step("step_a", _StepA())
    dag = PipelineDAG.from_steps([sa])
    runner = PipelineRunner(dag=dag, context=_FakeContext())
    result = runner.run()

    assert isinstance(result, RunResult)
    assert result.run_id == "test-run-001"
    assert result.failure_detail == ""


def test_failing_step_in_chain_marks_pipeline_failed():
    """A step that fails after a successful predecessor must mark the run failed."""
    sa = _make_step("step_a", _StepA())
    sf = _make_step("step_b", _FailingStep())
    sa >> sf

    dag = PipelineDAG.from_steps([sa, sf])
    runner = PipelineRunner(dag=dag, context=_FakeContext())
    result = runner.run()

    assert result.status == "failed"
    assert "RuntimeError: intentional failure" == result.failure_detail


def test_run_writes_metadata_to_runs_storage():
    import json

    written = {}

    class _FakeStorage:
        def write_text(self, text, path):
            written[path] = text

    ctx = _FakeContext()
    ctx.storages["runs"] = _FakeStorage()

    sa = _make_step("step_a", _StepA())
    dag = PipelineDAG.from_steps([sa])
    runner = PipelineRunner(dag=dag, context=ctx)
    runner.run()

    assert any("run.json" in k for k in written)
    key = next(k for k in written if "run.json" in k)
    data = json.loads(written[key])
    assert data["run_id"] == "test-run-001"
    assert data["status"] == "success"
    assert "step_a" in data["steps"]


def test_parallel_independent_steps():
    # Two root steps with no dependency between them — both should run via the
    # ThreadPoolExecutor path and appear in RunResult.steps.
    sa = _make_step("step_a", _StepA())
    sc = _make_step("step_c", _StepA())  # reuse _StepA impl (no inputs)

    dag = PipelineDAG.from_steps([sa, sc])
    runner = PipelineRunner(dag=dag, context=_FakeContext())
    result = runner.run()

    assert result.status == "success"
    assert "step_a" in result.steps
    assert "step_c" in result.steps


def test_partial_failure_retains_successful_step_result():
    # step_a succeeds, step_fail is independent and fails.
    # Overall status is "failed" but step_a's result is still in steps.
    sa = _make_step("step_a", _StepA())
    sf = _make_step("step_fail", _FailingStep())

    dag = PipelineDAG.from_steps([sa, sf])
    runner = PipelineRunner(dag=dag, context=_FakeContext())
    result = runner.run()

    assert result.status == "failed"
    # step_a completed before the failure was surfaced
    assert "step_a" in result.steps
    assert result.steps["step_a"].value == 1


def test_missing_runs_storage_does_not_raise():
    # ctx.storages has no "runs" key — runner should complete without raising.
    ctx = _FakeContext()
    assert "runs" not in ctx.storages

    sa = _make_step("step_a", _StepA())
    dag = PipelineDAG.from_steps([sa])
    runner = PipelineRunner(dag=dag, context=ctx)
    result = runner.run()

    assert result.status == "success"
