"""Dengue pipeline DAG definition.

Build the full DAG by wiring all steps with their dependency edges.
"""

from __future__ import annotations

from acestor import PipelineConfig, PipelineDAG, PipelineStep

from pipelines.dengue.steps.identify_sampling_day import IdentifySamplingDayStep
from pipelines.dengue.steps.download_case_data import DownloadCaseDataStep
from pipelines.dengue.steps.download_weather_data import DownloadWeatherDataStep
from pipelines.dengue.steps.parse_case_data import ParseCaseDataStep
from pipelines.dengue.steps.parse_weather_data import ParseWeatherDataStep
from pipelines.dengue.steps.validate_case_data_sufficiency import (
    ValidateCaseDataSufficiencyStep,
)
from pipelines.dengue.steps.identify_cutoff_dates import IdentifyCutoffDatesStep
from pipelines.dengue.steps.generate_thresholds import GenerateThresholdsStep
from pipelines.dengue.steps.train_and_predict import TrainAndPredictStep
from pipelines.dengue.steps.combine_predictions import CombinePredictionsStep
from pipelines.dengue.steps.assess_thresholds import AssessThresholdsStep
from pipelines.dengue.steps.generate_maps import GenerateMapsStep
from pipelines.dengue.steps.generate_report import GenerateReportStep
from pipelines.dengue.steps.send_report import SendReportStep
from pipelines.dengue.sources import filesystem as fs_sources


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
    """Build the dengue DAG.

    DAG shape::

        identify_sampling_day ──┬──> parse_case_data ────────> validate_case_data_sufficiency ──┐
        download_case_data ─────┘                                                                  ├──> identify_cutoff_dates
        identify_sampling_day ──┬──> parse_weather_data ───────────────────────────────────────────┘
        download_weather_data ──┘
                                     identify_cutoff_dates ──> generate_thresholds
                                     generate_thresholds ──> train_and_predict
                                     train_and_predict ──┬──> combine_predictions
                                     identify_cutoff_dates ┘
                                     combine_predictions ──> assess_thresholds
                                     assess_thresholds ──┬──> generate_maps
                                     combine_predictions ┘
                                     assess_thresholds ──┬──> generate_report ──> send_report
                                     generate_maps ──────┤
                                     identify_cutoff_dates ┘
    """
    # Apply data.geojson.base_path from YAML before any step runs.
    fs_sources.configure_from_yaml(config.raw)

    identify_sampling_day = PipelineStep(
        name="identify_sampling_day", impl=IdentifySamplingDayStep()
    )
    download_case_data = PipelineStep(
        name="download_case_data", impl=DownloadCaseDataStep()
    )
    download_weather_data = PipelineStep(
        name="download_weather_data", impl=DownloadWeatherDataStep()
    )
    parse_case_data = PipelineStep(name="parse_case_data", impl=ParseCaseDataStep())
    validate_case_data_sufficiency = PipelineStep(
        name="validate_case_data_sufficiency",
        impl=ValidateCaseDataSufficiencyStep(),
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
    send_report = PipelineStep(name="send_report", impl=SendReportStep())

    # --- Wire the DAG ---

    # download + sampling_day feed into parse steps
    [identify_sampling_day, download_case_data] >> parse_case_data
    parse_case_data >> validate_case_data_sufficiency
    [identify_sampling_day, download_weather_data] >> parse_weather_data

    # validated case parse + weather + sampling_day feed cutoff identification
    [
        validate_case_data_sufficiency,
        parse_weather_data,
        identify_sampling_day,
    ] >> identify_cutoff_dates

    # cutoffs -> thresholds -> train -> combine -> assess
    identify_cutoff_dates >> generate_thresholds
    [generate_thresholds, identify_cutoff_dates] >> train_and_predict
    [train_and_predict, identify_cutoff_dates] >> combine_predictions
    combine_predictions >> assess_thresholds

    # assess + combined predictions -> maps
    [assess_thresholds, combine_predictions] >> generate_maps

    # assess + maps + cutoffs (rep_dict epi/weather dates) -> report -> send email
    [assess_thresholds, generate_maps, identify_cutoff_dates] >> generate_report
    generate_report >> send_report

    return PipelineDAG.from_steps(
        [
            identify_sampling_day,
            download_case_data,
            download_weather_data,
            parse_case_data,
            validate_case_data_sufficiency,
            parse_weather_data,
            identify_cutoff_dates,
            generate_thresholds,
            train_and_predict,
            combine_predictions,
            assess_thresholds,
            generate_maps,
            generate_report,
            send_report,
        ]
    )
