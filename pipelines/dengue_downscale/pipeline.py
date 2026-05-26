"""dengue_downscale pipeline DAG.

Reads district-level predictions from a completed dengue run and disaggregates
them to any child spatial level using proportional rolling case shares derived
from child-level geojsons and case history.

DAG shape::

    load_predictions → downscale_predictions → generate_downscale_brief
"""

from __future__ import annotations

from acestor import PipelineConfig, PipelineDAG, PipelineStep

from pipelines.dengue_downscale.steps.load_predictions import LoadPredictionsStep
from pipelines.dengue_downscale.steps.downscale_predictions import (
    DownscalePredictionsStep,
)
from pipelines.dengue_downscale.steps.generate_downscale_brief import (
    GenerateDownscaleBriefStep,
)


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
    load_predictions = PipelineStep(name="load_predictions", impl=LoadPredictionsStep())
    downscale_predictions = PipelineStep(
        name="downscale_predictions", impl=DownscalePredictionsStep()
    )
    generate_downscale_brief = PipelineStep(
        name="generate_downscale_brief", impl=GenerateDownscaleBriefStep()
    )

    load_predictions >> downscale_predictions
    downscale_predictions >> generate_downscale_brief

    return PipelineDAG.from_steps(
        [load_predictions, downscale_predictions, generate_downscale_brief]
    )
