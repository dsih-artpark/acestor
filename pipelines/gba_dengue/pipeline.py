"""GBA dengue pipeline definition.

Build the full DAG by wiring all 12 steps with their dependency edges.
"""

from __future__ import annotations

from acestor import PipelineConfig, PipelineDAG, PipelineStep

from pipelines.gba_dengue.steps.identify_sampling_day import IdentifySamplingDayStep
from pipelines.gba_dengue.steps.download_case_data import DownloadCaseDataStep
from pipelines.gba_dengue.steps.download_weather_data import DownloadWeatherDataStep
from pipelines.gba_dengue.steps.parse_nonstd_case_data import (
    ParseNonStandardCaseDataStep,
)
from pipelines.gba_dengue.steps.parse_weather_data import ParseWeatherDataStep
from pipelines.gba_dengue.steps.identify_cutoff_dates import IdentifyCutoffDatesStep
from pipelines.gba_dengue.steps.generate_thresholds import GenerateThresholdsStep
from pipelines.gba_dengue.steps.train_and_predict import TrainAndPredictStep
from pipelines.gba_dengue.steps.combine_predictions import CombinePredictionsStep
from pipelines.gba_dengue.steps.assess_thresholds import AssessThresholdsStep
from pipelines.gba_dengue.steps.generate_maps import GenerateMapsStep
from pipelines.gba_dengue.steps.generate_report import GenerateReportStep


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
    """Build the GBA dengue DAG.

    DAG shape::

        identify_sampling_day ──┬──> parse_nonstd_case_data ──────┐
        download_case_data ─────┘                          │
                                                           ├──> identify_cutoff_dates
        identify_sampling_day ──┬──> parse_weather_data ───┘
        download_weather_data ──┘
                                     identify_cutoff_dates ──> generate_thresholds
                                     generate_thresholds ──> train_and_predict
                                     train_and_predict ──┬──> combine_predictions
                                     identify_cutoff_dates ┘
                                     combine_predictions ──> assess_thresholds
                                     assess_thresholds ──┬──> generate_maps
                                     combine_predictions ┘
                                     assess_thresholds ──┬──> generate_report
                                     generate_maps ──────┘
    """
    identify_sampling_day = PipelineStep(
        name="identify_sampling_day", impl=IdentifySamplingDayStep()
    )
    download_case_data = PipelineStep(
        name="download_case_data", impl=DownloadCaseDataStep()
    )
    download_weather_data = PipelineStep(
        name="download_weather_data", impl=DownloadWeatherDataStep()
    )
    parse_nonstd_case_data = PipelineStep(
        name="parse_nonstd_case_data", impl=ParseNonStandardCaseDataStep()
    )
    parse_weather_data = PipelineStep(
        name="parse_weather_data", impl=ParseWeatherDataStep()
    )
    identify_cutoff_dates = PipelineStep(
        name="identify_cutoff_dates", impl=IdentifyCutoffDatesStep()
    )
    generate_thresholds = PipelineStep(
        name="generate_thresholds", impl=GenerateThresholdsStep()
    )
    train_and_predict = PipelineStep(
        name="train_and_predict", impl=TrainAndPredictStep()
    )
    combine_predictions = PipelineStep(
        name="combine_predictions", impl=CombinePredictionsStep()
    )
    assess_thresholds = PipelineStep(
        name="assess_thresholds", impl=AssessThresholdsStep()
    )
    generate_maps = PipelineStep(name="generate_maps", impl=GenerateMapsStep())
    generate_report = PipelineStep(name="generate_report", impl=GenerateReportStep())

    # --- Wire the DAG ---

    # download + sampling_day feed into parse steps
    [identify_sampling_day, download_case_data] >> parse_nonstd_case_data
    [identify_sampling_day, download_weather_data] >> parse_weather_data

    # parse steps feed into cutoff identification
    [parse_nonstd_case_data, parse_weather_data] >> identify_cutoff_dates

    # cutoffs -> thresholds -> train -> combine -> assess
    identify_cutoff_dates >> generate_thresholds
    generate_thresholds >> train_and_predict
    [train_and_predict, identify_cutoff_dates] >> combine_predictions
    combine_predictions >> assess_thresholds

    # assess + combined predictions -> maps
    [assess_thresholds, combine_predictions] >> generate_maps

    # assess + maps -> report
    [assess_thresholds, generate_maps] >> generate_report

    return PipelineDAG.from_steps(
        [
            identify_sampling_day,
            download_case_data,
            download_weather_data,
            parse_nonstd_case_data,
            parse_weather_data,
            identify_cutoff_dates,
            generate_thresholds,
            train_and_predict,
            combine_predictions,
            assess_thresholds,
            generate_maps,
            generate_report,
        ]
    )
