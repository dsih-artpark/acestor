"""dengue_rollup pipeline DAG.

Sums child-level predictions up to a coarser parent level and re-derives
parent-level zones. The trivial inverse of dengue_downscale.

DAG shape::

    load_predictions → rollup_predictions → generate_rollup_brief
"""

from __future__ import annotations

from acestor import PipelineConfig, PipelineDAG, PipelineStep

from pipelines.dengue_rollup.steps.generate_rollup_brief import (
    GenerateRollupBriefStep,
)
from pipelines.dengue_rollup.steps.load_predictions import LoadPredictionsStep
from pipelines.dengue_rollup.steps.rollup_predictions import RollupPredictionsStep


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
    load_predictions = PipelineStep(name="load_predictions", impl=LoadPredictionsStep())
    rollup_predictions = PipelineStep(
        name="rollup_predictions", impl=RollupPredictionsStep()
    )
    generate_rollup_brief = PipelineStep(
        name="generate_rollup_brief", impl=GenerateRollupBriefStep()
    )

    load_predictions >> rollup_predictions
    rollup_predictions >> generate_rollup_brief

    return PipelineDAG.from_steps(
        [load_predictions, rollup_predictions, generate_rollup_brief]
    )
