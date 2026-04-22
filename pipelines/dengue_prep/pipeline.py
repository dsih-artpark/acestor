"""dengue_prep pipeline DAG.

Runs download + daily aggregation for case and weather data, upserting results
into prepared_data/{region_type}/ for consumption by the dengue pipeline.

DAG shape::

    download_case_data ────> parse_case_data
    download_weather_data ─> parse_weather_data
"""

from __future__ import annotations

from acestor import PipelineConfig, PipelineDAG, PipelineStep

from pipelines.dengue_prep.steps.download_case_data import PrepDownloadCaseDataStep
from pipelines.dengue_prep.steps.parse_case_data import PrepParseCaseDataStep
from pipelines.dengue_prep.steps.download_weather_data import (
    PrepDownloadWeatherDataStep,
)
from pipelines.dengue_prep.steps.parse_weather_data import PrepParseWeatherDataStep


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
    download_case_data = PipelineStep(
        name="download_case_data", impl=PrepDownloadCaseDataStep()
    )
    download_weather_data = PipelineStep(
        name="download_weather_data", impl=PrepDownloadWeatherDataStep()
    )
    parse_case_data = PipelineStep(name="parse_case_data", impl=PrepParseCaseDataStep())
    parse_weather_data = PipelineStep(
        name="parse_weather_data", impl=PrepParseWeatherDataStep()
    )

    download_case_data >> parse_case_data
    download_weather_data >> parse_weather_data

    return PipelineDAG.from_steps(
        [
            download_case_data,
            download_weather_data,
            parse_case_data,
            parse_weather_data,
        ]
    )
