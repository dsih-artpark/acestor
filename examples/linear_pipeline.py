"""Minimal example: linear pipeline using acestor core primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from acestor import (
    PipelineConfig,
    PipelineContext,
    PipelineStep,
    PipelineDAG,
    PipelineRunner,
)


@dataclass
class StepA:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return {"value": 1}


@dataclass
class StepB:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        prev = inputs["step_a"]["value"]
        return {"value": prev * 5}


@dataclass
class StepC:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        prev = inputs["step_b"]["value"]
        return {"value": prev * 10}


def main() -> None:
    config = PipelineConfig.from_yaml("example.yaml")
    context = PipelineContext.from_config(config, run_id="example-run")

    step_a = PipelineStep(name="step_a", impl=StepA())
    step_b = PipelineStep(name="step_b", impl=StepB())
    step_c = PipelineStep(name="step_c", impl=StepC())

    step_a >> step_b
    step_b >> step_c

    dag = PipelineDAG.from_steps([step_a, step_b, step_c])

    runner = PipelineRunner(dag=dag, context=context)
    result = runner.run()
    print(result)


if __name__ == "__main__":
    main()
