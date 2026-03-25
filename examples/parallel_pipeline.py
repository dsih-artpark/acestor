"""Example: parallel branches in an acestor pipeline."""

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
class FanOut:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return {"base": 10}


@dataclass
class BranchA:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        base = inputs["fan_out"]["base"]
        return {"value_a": base + 1}


@dataclass
class BranchB:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        base = inputs["fan_out"]["base"]
        return {"value_b": base + 2}


@dataclass
class Join:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        a = inputs["branch_a"]["value_a"]
        b = inputs["branch_b"]["value_b"]
        return {"sum": a + b}


def main() -> None:
    config = PipelineConfig.from_yaml("example.yaml")
    context = PipelineContext.from_config(config, run_id="parallel-example-run")

    fan_out = PipelineStep(name="fan_out", impl=FanOut())
    branch_a = PipelineStep(name="branch_a", impl=BranchA())
    branch_b = PipelineStep(name="branch_b", impl=BranchB())
    join = PipelineStep(name="join", impl=Join())

    fan_out >> [branch_a, branch_b]
    [branch_a, branch_b] >> join

    dag = PipelineDAG.from_steps([fan_out, branch_a, branch_b, join])

    runner = PipelineRunner(dag=dag, context=context)
    result = runner.run()
    print(result)


if __name__ == "__main__":
    main()
