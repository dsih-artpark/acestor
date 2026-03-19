"""Incremental pipeline — steps 1–3: identify_sampling_day, downloads, parse_nonstd_case_data."""

from __future__ import annotations

from acestor import PipelineConfig, PipelineDAG, PipelineStep

from pipelines.gba_dengue.steps.identify_sampling_day import IdentifySamplingDayStep
from pipelines.gba_dengue.steps.download_case_data import DownloadCaseDataStep
from pipelines.gba_dengue.steps.download_weather_data import DownloadWeatherDataStep
from pipelines.gba_dengue.steps.parse_nonstd_case_data import (
    ParseNonStandardCaseDataStep,
)


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
    parse_nonstd_case_data = PipelineStep(
        name="parse_nonstd_case_data", impl=ParseNonStandardCaseDataStep()
    )

    [identify_sampling_day, download_case_data] >> parse_nonstd_case_data

    return PipelineDAG.from_steps(
        [
            identify_sampling_day,
            download_case_data,
            download_weather_data,
            parse_nonstd_case_data,
        ]
    )
