"""Incremental pipeline through the report stage (full graph for integration tests).

Stages (in dependency order): sampling day → case/weather download → case parse →
sufficiency gate → weather parse → cutoffs → thresholds → train/predict → combine
predictions → assess thresholds → maps → report (JSON bundle + map zip; PDF optional).
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
from pipelines.dengue.steps.notify_run import NotifyRunStep


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
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
    notify_run = PipelineStep(name="notify_run", impl=NotifyRunStep())

    [identify_sampling_day, download_case_data] >> parse_case_data
    parse_case_data >> validate_case_data_sufficiency
    [identify_sampling_day, download_weather_data] >> parse_weather_data
    [validate_case_data_sufficiency, parse_weather_data] >> identify_cutoff_dates
    identify_cutoff_dates >> generate_thresholds
    [identify_cutoff_dates, generate_thresholds] >> train_and_predict
    [train_and_predict, identify_cutoff_dates] >> combine_predictions
    combine_predictions >> assess_thresholds
    [assess_thresholds, combine_predictions] >> generate_maps
    [train_and_predict, generate_maps, identify_cutoff_dates] >> generate_report
    generate_report >> notify_run

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
            notify_run,
        ]
    )
