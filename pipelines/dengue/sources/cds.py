from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class CDSSource:
    """Environment-backed CDS configuration (no YAML knobs).

    Weather download uses `pipelines.gba_dengue.lib.cds` for the actual
    download/parse pipeline.
    """

    dataset: str
    variables: list[str]
    cds_url: str
    cds_key: str

    # Local working directories used internally by CDS download/parse.
    cache_path: str
    parsed_output_path: str

    # Defaults aligned with existing YAML defaults.
    DEFAULT_DATASET: ClassVar[str] = "reanalysis-era5-land"
    DEFAULT_VARIABLES: ClassVar[list[str]] = [
        "2m_temperature",
        "2m_dewpoint_temperature",
        "total_precipitation",
    ]
    DEFAULT_CACHE_PATH: ClassVar[str] = "datasets/netcdf"
    DEFAULT_PARSED_OUTPUT_PATH: ClassVar[str] = "datasets/parsednetcdf"

    @classmethod
    def from_env(cls) -> CDSSource:
        dataset = os.getenv("GBA_CDS_DATASET", "").strip() or cls.DEFAULT_DATASET
        variables_raw = os.getenv("GBA_CDS_VARIABLES", "").strip()
        if variables_raw:
            variables = [v.strip() for v in variables_raw.split(",") if v.strip()]
        else:
            variables = list(cls.DEFAULT_VARIABLES)

        # These are optional; if empty, cdsapi will fall back to ~/.cdsapirc.
        cds_url = os.getenv("CDS_API_URL", "").strip()
        cds_key = os.getenv("CDS_API_KEY", "").strip()

        cache_path = (
            os.getenv("GBA_CDS_CACHE_PATH", "").strip() or cls.DEFAULT_CACHE_PATH
        )
        parsed_output_path = (
            os.getenv("GBA_CDS_PARSED_OUTPUT_PATH", "").strip()
            or cls.DEFAULT_PARSED_OUTPUT_PATH
        )

        return cls(
            dataset=dataset,
            variables=variables,
            cds_url=cds_url,
            cds_key=cds_key,
            cache_path=cache_path,
            parsed_output_path=parsed_output_path,
        )


_CACHED: CDSSource | None = None


def get_cds_source() -> CDSSource:
    global _CACHED
    if _CACHED is None:
        _CACHED = CDSSource.from_env()
    return _CACHED
