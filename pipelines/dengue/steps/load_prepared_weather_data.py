"""Load pre-aggregated daily weather data and apply rolling aggregation + sampling.

This step replaces download_weather_data + parse_weather_data in the main dengue
pipeline when prepared_data is produced externally (by dengue_prep or a manual drop).

The step is registered in the DAG under the name ``"parse_weather_data"`` so that
all downstream steps (identify_cutoff_dates, etc.) require no changes.

The prepared weather_daily.csv stores original ERA5 column names (2mTemperature, etc.)
so that rolling aggregation here can use the same config as before.  Column renaming
(t2m_mean, tp_sum, etc.) is applied after rolling, matching existing behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import PreparedDataConfig, WeatherParseConfig, _section
from pipelines.dengue.lib import weather
from pipelines.dengue.lib.case_data import get_latest_sampling_day
from pipelines.dengue.results import ParseWeatherDataResult, SamplingDayResult


@dataclass(frozen=True)
class LoadPreparedWeatherDataInputs:
    identify_sampling_day: SamplingDayResult


class LoadPreparedWeatherDataStep(
    BaseStep[LoadPreparedWeatherDataInputs, ParseWeatherDataResult]
):
    """Read weather_daily.csv, apply N-day rolling aggregation, sample, write to artifacts."""

    input_type: ClassVar[type] = LoadPreparedWeatherDataInputs

    def run(
        self, context: PipelineContext, inputs: LoadPreparedWeatherDataInputs
    ) -> ParseWeatherDataResult:
        cfg = PreparedDataConfig.from_raw(
            _section(context.config, "data.prepared_data")
        )
        weather_cfg = WeatherParseConfig.from_raw(
            _section(context.config, "data.weather_parse")
        )

        weather_path = Path(cfg.base_dir) / cfg.region_type / "weather_daily.csv"
        if not weather_path.is_file():
            raise FileNotFoundError(
                f"load_prepared_weather_data: file not found: {weather_path}\n"
                "Run the dengue_prep pipeline first, or place weather_daily.csv manually."
            )

        daily = pd.read_csv(weather_path, low_memory=False)
        daily["date"] = pd.to_datetime(daily["date"])

        run_date = pd.Timestamp(inputs.identify_sampling_day.run_date).normalize()
        daily = daily[daily["date"] <= run_date]

        # Reindex each region onto a contiguous daily grid so dates[::-7] sampling
        # produces calendar-aligned 7-day spacing. Source data has end-of-month
        # gaps (~49/1700 days missing); without this, weather and case sampled
        # dates phase-shift mid-stream and the merge in train_and_predict drops
        # rows whose lag features fall on missing dates.
        if not daily.empty:
            num_cols = daily.select_dtypes(include="number").columns.tolist()
            meta_cols = [
                c
                for c in daily.columns
                if c not in num_cols and c not in {"region_id", "date"}
            ]

            filled_parts: list[pd.DataFrame] = []
            for region_id, group in daily.groupby("region_id"):
                full = pd.date_range(start=group["date"].min(), end=group["date"].max())
                g = (
                    group.set_index("date")
                    .reindex(full)
                    .rename_axis("date")
                    .reset_index()
                )
                g["region_id"] = region_id
                if num_cols:
                    g[num_cols] = g[num_cols].interpolate(
                        method="linear", limit_direction="both"
                    )
                for c in meta_cols:
                    g[c] = g[c].ffill().bfill()
                filled_parts.append(g)
            daily = pd.concat(filled_parts, ignore_index=True)

        rolling = weather.rolling_aggregate(
            daily,
            weather_cfg.weather_variables,
            n_days=weather_cfg.rolling_n_days,
            rolling_agg=weather_cfg.rolling_agg,
        )

        max_date = pd.Timestamp(rolling["date"].max())
        latest_day = get_latest_sampling_day(
            max_date, inputs.identify_sampling_day.sampling_day
        )
        sampled = weather.sample_data(
            rolling,
            end_date=latest_day,
            sample_from="end",
            sampling_rate=weather_cfg.sampling_rate,
        )

        rename_map = {
            k: v
            for k, v in weather_cfg.intermediate_col_rename.items()
            if k in sampled.columns
        }
        renamed = sampled.rename(columns=rename_map) if rename_map else sampled

        dest = context.artifact_path(f"inputs/weather_{cfg.region_type}_sampled.csv")
        context.artifacts.write_text(renamed.to_csv(index=False), dest)
        context.log.info(
            "load_prepared_weather_data: region_type=%s rows=%d",
            cfg.region_type,
            len(renamed),
        )

        return ParseWeatherDataResult(
            weather_csv_path=dest, region_type=cfg.region_type
        )
