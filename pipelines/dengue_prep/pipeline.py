"""dengue_prep pipeline DAG.

Runs download + daily aggregation for case and weather data, upserting results
into prepared_data/{region_type}/ for consumption by the dengue pipeline.

DAG shape::

    download_geojsons ──┬─> parse_case_data
                        └─> parse_weather_data
    download_case_data ──> parse_case_data
    download_weather_data ─> parse_weather_data
"""

from __future__ import annotations

from acestor import PipelineConfig, PipelineDAG, PipelineStep

from pipelines.dengue_prep.steps.download_case_data import PrepDownloadCaseDataStep
from pipelines.dengue_prep.steps.download_geojsons import PrepDownloadGeojsonsStep
from pipelines.dengue_prep.steps.parse_case_data import PrepParseCaseDataStep
from pipelines.dengue_prep.steps.download_weather_data import (
    PrepDownloadWeatherDataStep,
)
from pipelines.dengue_prep.steps.parse_weather_data import PrepParseWeatherDataStep


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
    download_geojsons = PipelineStep(
        name="download_geojsons", impl=PrepDownloadGeojsonsStep()
    )
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

    # Both parse steps need the geojson tree populated (region-id allowlist +
    # rollup checks, weather region-centroid lookup). Wiring the fetch first
    # avoids a race where a parallel download_case_data starts parsing before
    # the geojsons exist on disk.
    download_geojsons >> parse_case_data
    download_geojsons >> parse_weather_data
    download_case_data >> parse_case_data
    download_weather_data >> parse_weather_data

    return PipelineDAG.from_steps(
        [
            download_geojsons,
            download_case_data,
            download_weather_data,
            parse_case_data,
            parse_weather_data,
        ]
    )
