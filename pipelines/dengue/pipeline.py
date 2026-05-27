"""Dengue pipeline DAG definition.

Build the full DAG by wiring all steps with their dependency edges.
"""

from __future__ import annotations

from acestor import PipelineConfig, PipelineDAG, PipelineStep

from pipelines.dengue.steps.identify_sampling_day import IdentifySamplingDayStep
from pipelines.dengue.steps.load_prepared_case_data import LoadPreparedCaseDataStep
from pipelines.dengue.steps.load_prepared_weather_data import (
    LoadPreparedWeatherDataStep,
)
from pipelines.dengue.steps.validate_case_data_sufficiency import (
    ValidateCaseDataSufficiencyStep,
)
from pipelines.dengue.steps.identify_cutoff_dates import IdentifyCutoffDatesStep
from pipelines.dengue.steps.generate_thresholds import GenerateThresholdsStep
from pipelines.dengue.steps.train_and_predict import TrainAndPredictStep
from pipelines.dengue.steps.assess_thresholds import AssessThresholdsStep
from pipelines.dengue.steps.generate_maps import GenerateMapsStep
from pipelines.dengue.steps.generate_report import GenerateReportStep
from pipelines.dengue.steps.send_report import SendReportStep
from pipelines.dengue.sources import filesystem as fs_sources


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
    """Build the dengue DAG.

    Expects prepared_data/{region_type}/cases_daily.csv and weather_daily.csv to
    exist (written by the dengue_prep pipeline or placed manually).

    DAG shape::

        identify_sampling_day ──┬──> parse_case_data (load) ──> validate_case_data_sufficiency ──┐
                                │                                                                   ├──> identify_cutoff_dates
                                └──> parse_weather_data (load) ─────────────────────────────────────┘
                                                                                                      also: identify_sampling_day ──┘
                                     identify_cutoff_dates ──> generate_thresholds
                                     generate_thresholds ──> train_and_predict
                                     train_and_predict ──> assess_thresholds
                                     assess_thresholds ──┬──> generate_maps
                                     train_and_predict ──┘
                                     assess_thresholds ──┬──> generate_report ──> send_report
                                     generate_maps ──────┤
                                     identify_cutoff_dates ┘
    """
    fs_sources.configure_from_yaml(config.raw)

    identify_sampling_day = PipelineStep(
        name="identify_sampling_day", impl=IdentifySamplingDayStep()
    )
    # Named "parse_case_data" / "parse_weather_data" so downstream input dataclasses
    # (ValidateCaseDataSufficiencyInputs, IdentifyCutoffDatesInputs) match by field name.
    parse_case_data = PipelineStep(
        name="parse_case_data", impl=LoadPreparedCaseDataStep()
    )
    parse_weather_data = PipelineStep(
        name="parse_weather_data", impl=LoadPreparedWeatherDataStep()
    )
    validate_case_data_sufficiency = PipelineStep(
        name="validate_case_data_sufficiency",
        impl=ValidateCaseDataSufficiencyStep(),
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
    assess_thresholds = PipelineStep(
        name="assess_thresholds", impl=AssessThresholdsStep()
    )
    generate_maps = PipelineStep(name="generate_maps", impl=GenerateMapsStep())
    generate_report = PipelineStep(name="generate_report", impl=GenerateReportStep())
    send_report = PipelineStep(name="send_report", impl=SendReportStep())

    # --- Wire the DAG ---

    identify_sampling_day >> parse_case_data
    identify_sampling_day >> parse_weather_data
    parse_case_data >> validate_case_data_sufficiency

    [
        validate_case_data_sufficiency,
        parse_weather_data,
        identify_sampling_day,
    ] >> identify_cutoff_dates

    identify_cutoff_dates >> generate_thresholds
    [generate_thresholds, identify_cutoff_dates] >> train_and_predict
    train_and_predict >> assess_thresholds

    [assess_thresholds, train_and_predict] >> generate_maps
    [train_and_predict, generate_maps, identify_cutoff_dates] >> generate_report
    generate_report >> send_report

    return PipelineDAG.from_steps(
        [
            identify_sampling_day,
            parse_case_data,
            parse_weather_data,
            validate_case_data_sufficiency,
            identify_cutoff_dates,
            generate_thresholds,
            train_and_predict,
            assess_thresholds,
            generate_maps,
            generate_report,
            send_report,
        ]
    )
