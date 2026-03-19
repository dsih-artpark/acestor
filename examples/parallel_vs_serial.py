"""Example: show clear difference between serial and parallel execution."""

from __future__ import annotations

import time
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
class StartStep:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        return {"start": time.time()}


@dataclass
class SlowA:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        time.sleep(2.0)
        return {"a_done_at": time.time()}


@dataclass
class SlowB:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        time.sleep(2.0)
        return {"b_done_at": time.time()}


@dataclass
class EndStep:
    def run(self, context: PipelineContext, inputs: Dict[str, Any]) -> Dict[str, Any]:
        end = time.time()
        return {"end": end}


def build_dag() -> PipelineDAG:
    start = PipelineStep(name="start", impl=StartStep())
    slow_a = PipelineStep(name="slow_a", impl=SlowA())
    slow_b = PipelineStep(name="slow_b", impl=SlowB())
    end = PipelineStep(name="end", impl=EndStep())

    start >> [slow_a, slow_b]
    [slow_a, slow_b] >> end

    return PipelineDAG.from_steps([start, slow_a, slow_b, end])


def serial_dag() -> PipelineDAG:
    start = PipelineStep(name="start", impl=StartStep())
    slow_a = PipelineStep(name="slow_a", impl=SlowA())
    slow_b = PipelineStep(name="slow_b", impl=SlowB())
    end = PipelineStep(name="end", impl=EndStep())

    start >> slow_a >> slow_b >> end

    return PipelineDAG.from_steps([start, slow_a, slow_b, end])


def main() -> None:
    config = PipelineConfig.from_yaml("pipeline_configs/parallel_vs_serial.yaml")
    context = PipelineContext.from_config(config, run_id="parallel-vs-serial")

    dag = build_dag()
    runner = PipelineRunner(dag=dag, context=context)

    t0 = time.time()
    result = runner.run()
    elapsed = time.time() - t0

    print(f"RUN: status={result.status}, elapsed={elapsed:.2f}s")
    print(result)

    dag = serial_dag()
    runner = PipelineRunner(dag=dag, context=context)

    t0 = time.time()
    result = runner.run()
    elapsed = time.time() - t0

    print(f"SERIAL RUN: status={result.status}, elapsed={elapsed:.2f}s")
    print(result)


if __name__ == "__main__":
    main()
