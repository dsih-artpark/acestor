"""Typed result dataclasses for every step in the dengue_prep pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PrepCaseDownloadResult:
    enabled: bool
    copied_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrepCaseParseResult:
    """Daily aggregated case counts written to prepared_data/{region_type}/cases_daily.csv."""

    region_type: str
    prepared_data_path: str  # absolute path to cases_daily.csv
    total_rows: int


@dataclass(frozen=True)
class PrepWeatherDownloadResult:
    enabled: bool
    downloaded_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrepWeatherParseResult:
    """Daily aggregated weather written to prepared_data/{region_type}/weather_daily.csv.

    Columns are stored with original ERA5 names (2mTemperature, totalPrecipitation, etc.)
    so that Pipeline 2 can still apply rolling aggregation before renaming.
    """

    region_type: str
    prepared_data_path: str  # absolute path to weather_daily.csv
    total_rows: int
