from __future__ import annotations

import io
import os
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from acestor.core.sources import FileSystemSource, S3Source
from pipelines.dengue.configs import (
    WeatherDownloadConfig,
    WeatherParseConfig,
    _section,
)
from pipelines.dengue.lib import weather
from pipelines.dengue.lib.case_data import get_latest_sampling_day
from pipelines.dengue.results import (
    ParseWeatherDataResult,
    SamplingDayResult,
    WeatherDownloadResult,
)


@dataclass(frozen=True)
class ParseWeatherDataInputs:
    identify_sampling_day: SamplingDayResult
    download_weather_data: WeatherDownloadResult


class ParseWeatherDataStep(BaseStep[ParseWeatherDataInputs, ParseWeatherDataResult]):
    input_type: ClassVar[type] = ParseWeatherDataInputs

    def _build_weather_source(self, cfg: WeatherDownloadConfig):
        backend = (cfg.source_backend or "filesystem").strip().lower()
        source_path = (cfg.source_path or "").strip()
        if source_path and backend == "s3" and source_path.startswith("s3://"):
            bucket_and_prefix = source_path[len("s3://") :]
            bucket, _, prefix = bucket_and_prefix.partition("/")
            return S3Source(
                bucket=bucket,
                base_prefix=prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )
        if source_path and backend == "filesystem":
            return FileSystemSource(base_path=source_path)
        if backend == "s3" and cfg.s3_bucket:
            return S3Source(
                bucket=cfg.s3_bucket,
                base_prefix=cfg.s3_prefix,
                aws_profile=(os.getenv("AWS_PROFILE", "").strip() or None),
                region=(os.getenv("AWS_REGION", "").strip() or None),
                cache_enabled=cfg.cache_enabled,
                cache_dir=cfg.cache_dir,
                strategy=cfg.cache_strategy,
            )
        return FileSystemSource(base_path=cfg.filesystem_base_path or "")

    def _write_intermediate(
        self,
        context: PipelineContext,
        df: pd.DataFrame,
        region_type: str,
        folder: str,
        col_rename: dict[str, str],
    ) -> None:
        """Write per-month partitioned intermediate files matching SOT structure.

        Path: datasets/{folder}/{region_type}/{year}/{year}_{month:02d}.csv
        Columns renamed per col_rename before writing.
        """
        out = df.copy()
        rename_map = {k: v for k, v in col_rename.items() if k in out.columns}
        if rename_map:
            out = out.rename(columns=rename_map)
        out["date"] = pd.to_datetime(out["date"])
        for (year, month), grp in out.groupby(
            [out["date"].dt.year, out["date"].dt.month]
        ):
            rel = f"inputs/{folder}/{region_type}/{year}/{year}_{month:02d}.csv"
            dest = context.artifact_path(rel)
            context.artifacts.write_text(grp.to_csv(index=False), dest)

    def _read_bytes(self, context: PipelineContext, source, ref: str) -> bytes:
        if ref.startswith("filesystem://"):
            with open(ref[len("filesystem://") :], "rb") as fh:
                return fh.read()
        if ref.startswith("fs://"):
            with open(ref[len("fs://") :], "rb") as fh:
                return fh.read()
        if ref.startswith("s3://"):
            return source.read(ref[len("s3://") :])
        # Backward compatibility for older runs that wrote artifact keys.
        return context.artifacts.read(ref)

    def run(
        self, context: PipelineContext, inputs: ParseWeatherDataInputs
    ) -> ParseWeatherDataResult:
        cfg = WeatherParseConfig.from_raw(
            _section(context.config, "data.weather_parse")
        )
        dl_cfg = WeatherDownloadConfig.from_raw(
            _section(context.config, "data.weather_download")
        )
        source = self._build_weather_source(dl_cfg)

        files = inputs.download_weather_data.downloaded_files
        raw_dfs = [
            pd.read_csv(
                io.BytesIO(self._read_bytes(context, source, f)), low_memory=False
            )
            for f in files
        ]
        if not raw_dfs:
            dest = context.artifact_path(
                f"inputs/weather_{cfg.region_type}_sampled.csv"
            )
            context.artifacts.write_text("", dest)
            return ParseWeatherDataResult(
                weather_csv_path=dest, region_type=cfg.region_type
            )

        merged = pd.concat(raw_dfs, ignore_index=True)
        merged = weather.normalise_columns(merged)

        if "region_id" not in merged.columns:
            for candidate in [
                "location.admin3.ID",
                "location.admin2.ID",
                "location.admin4.ID",
            ]:
                if candidate in merged.columns:
                    merged.rename(columns={candidate: "region_id"}, inplace=True)
                    break
        if "date" not in merged.columns and "metadata.primaryDate" in merged.columns:
            merged.rename(columns={"metadata.primaryDate": "date"}, inplace=True)

        daily = weather.aggregate_daily(
            merged,
            cfg.weather_variables,
            daily_agg=cfg.daily_agg,
        )
        rolling = weather.rolling_aggregate(
            daily,
            cfg.weather_variables,
            n_days=cfg.rolling_n_days,
            rolling_agg=cfg.rolling_agg,
        )

        run_date = pd.Timestamp(inputs.identify_sampling_day.run_date).normalize()
        daily = daily[pd.to_datetime(daily["date"]) <= run_date]
        rolling = rolling[pd.to_datetime(rolling["date"]) <= run_date]

        if cfg.write_agg_daily:
            self._write_intermediate(
                context,
                daily,
                cfg.region_type,
                "agg_daily",
                cfg.intermediate_col_rename,
            )
        if cfg.write_agg_ndays:
            self._write_intermediate(
                context,
                rolling,
                cfg.region_type,
                "agg_Ndays",
                cfg.intermediate_col_rename,
            )

        max_date = pd.Timestamp(rolling["date"].max())
        latest_day = get_latest_sampling_day(
            max_date, inputs.identify_sampling_day.sampling_day
        )
        sampled = weather.sample_data(
            rolling,
            end_date=latest_day,
            sample_from="end",
            sampling_rate=cfg.sampling_rate,
        )

        rename_map = {
            k: v for k, v in cfg.intermediate_col_rename.items() if k in sampled.columns
        }
        renamed = sampled.rename(columns=rename_map) if rename_map else sampled
        dest = context.artifact_path(f"inputs/weather_{cfg.region_type}_sampled.csv")
        context.artifacts.write_text(renamed.to_csv(index=False), dest)
        context.log.info(
            "parse_weather_data: %d rows, region_type=%s", len(renamed), cfg.region_type
        )

        return ParseWeatherDataResult(
            weather_csv_path=dest, region_type=cfg.region_type
        )
