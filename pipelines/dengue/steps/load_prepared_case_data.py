"""Load pre-aggregated daily case data and apply rolling aggregation + sampling.

This step replaces download_case_data + parse_case_data in the main dengue pipeline
when prepared_data is produced externally (by dengue_prep or a manual data drop).

The step is registered in the DAG under the name ``"parse_case_data"`` so that
all downstream steps (validate_case_data_sufficiency, etc.) require no changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pandas as pd

from acestor import BaseStep, PipelineContext
from pipelines.dengue.configs import (
    CaseParseConfig,
    CaseSufficiencyConfig,
    PreparedDataConfig,
    _section,
)
from pipelines.dengue.lib import case_data
from pipelines.dengue.results import ParseCaseDataResult, SamplingDayResult


@dataclass(frozen=True)
class LoadPreparedCaseDataInputs:
    identify_sampling_day: SamplingDayResult


def check_data_recency(
    daily: pd.DataFrame,
    run_date: pd.Timestamp,
    max_staleness_days: int,
) -> int | None:
    """Return staleness in days, or raise if it exceeds ``max_staleness_days``.

    Returns ``None`` when the guard is disabled (``max_staleness_days <= 0``)
    or when the dataframe is empty (no signal). Callers can log the returned
    staleness on the happy path.
    """
    if max_staleness_days <= 0 or daily.empty:
        return None
    max_observed = pd.Timestamp(daily["date"].max()).normalize()
    staleness_days = (run_date - max_observed).days
    if staleness_days > max_staleness_days:
        raise ValueError(
            f"data recency check failed: run_date={run_date.date()} is "
            f"{staleness_days} day(s) ahead of the most recent case date in "
            f"prepared data ({max_observed.date()}), exceeding "
            f"data.case_sufficiency.max_staleness_days={max_staleness_days}. "
            f"The prepared data is likely stale — re-run dengue_prep or use "
            f"an earlier run_date. Set max_staleness_days=0 to disable this "
            f"guard."
        )
    return staleness_days


class LoadPreparedCaseDataStep(
    BaseStep[LoadPreparedCaseDataInputs, ParseCaseDataResult]
):
    """Read cases_daily.csv, apply 7-day rolling aggregation, sample, write to artifacts."""

    input_type: ClassVar[type] = LoadPreparedCaseDataInputs

    def run(
        self, context: PipelineContext, inputs: LoadPreparedCaseDataInputs
    ) -> ParseCaseDataResult:
        cfg = PreparedDataConfig.from_raw(
            _section(context.config, "data.prepared_data")
        )
        parse_cfg = CaseParseConfig.from_raw(
            _section(context.config, "data.case_parse")
        )

        cases_path = Path(cfg.base_dir) / cfg.region_type / "cases_daily.csv"
        if not cases_path.is_file():
            raise FileNotFoundError(
                f"\n"
                f"  Prepared case data not found: {cases_path}\n\n"
                f"  Pipeline 2 requires Pipeline 1 to be run first.\n"
                f"  Suggested command:\n\n"
                f"    uv run python -m acestor.run \\\n"
                f"      --pipeline pipelines.dengue_prep.pipeline:build_pipeline \\\n"
                f"      --config configs/ap_{cfg.region_type}_prep.yaml\n\n"
                f"  Alternatively, place cases_daily.csv manually at: {cases_path}"
            )

        daily = pd.read_csv(cases_path, low_memory=False)
        daily["date"] = pd.to_datetime(daily["date"])

        # prepared_data uses "case_count"; rolling_aggregate expects "case"
        if "case_count" in daily.columns and "case" not in daily.columns:
            daily = daily.rename(columns={"case_count": "case"})

        run_date = pd.Timestamp(inputs.identify_sampling_day.run_date).normalize()
        if parse_cfg.date_start:
            daily = daily[
                daily["date"] >= pd.Timestamp(parse_cfg.date_start).normalize()
            ]

        # Recency guard — runs BEFORE the clamp at `daily["date"] <= run_date`,
        # which would otherwise make any recency check vacuous. Catches the
        # genuine failure mode of run_date running ahead of available data
        # (every model's prediction window ends past the data → NaN case lags
        # → silent model dropout).
        sufficiency_cfg = CaseSufficiencyConfig.from_raw(
            _section(context.config, "data.case_sufficiency")
        )
        staleness = check_data_recency(
            daily, run_date, sufficiency_cfg.max_staleness_days
        )
        if staleness is not None:
            context.log.info(
                "load_prepared_case_data: recency ok — staleness=%d day(s) "
                "(threshold=%d)",
                staleness,
                sufficiency_cfg.max_staleness_days,
            )

        daily = daily[daily["date"] <= run_date]

        # Reindex each region onto a contiguous daily grid (zero-fill no-case days)
        # so that dates[::-7] sampling produces the same calendar dates as the
        # dense daily weather grid — otherwise sparse case dates make the sampled
        # cases and weather diverge and the merge in train_and_predict drops to ~0.
        def _fill_daily(group: pd.DataFrame) -> pd.DataFrame:
            full = pd.date_range(start=group["date"].min(), end=group["date"].max())
            g = group.set_index("date").reindex(full).rename_axis("date").reset_index()
            g["case"] = g["case"].fillna(0)
            g["region_id"] = g["region_id"].ffill()
            return g

        if not daily.empty:
            daily = (
                daily.groupby("region_id", group_keys=False)[
                    ["region_id", "date", "case"]
                ]
                .apply(_fill_daily)
                .reset_index(drop=True)
            )

        rolling = case_data.rolling_aggregate(daily, n_days=7)
        max_date = pd.Timestamp(rolling["date"].max())
        latest_day = case_data.get_latest_sampling_day(
            max_date, inputs.identify_sampling_day.sampling_day
        )
        sampled = case_data.sample_data(rolling, end_date=latest_day)
        renamed = case_data.rename_columns_for_output(sampled, cfg.region_type)

        dest = context.artifact_path(f"inputs/cases_{cfg.region_type}_sampled.csv")
        context.artifacts.write_text(renamed.to_csv(index=False), dest)
        context.log.info(
            "load_prepared_case_data: region_type=%s rows=%d sampled_rows=%d",
            cfg.region_type,
            len(daily),
            len(renamed),
        )

        return ParseCaseDataResult(
            sampled_csv_path=dest,
            region_type=cfg.region_type,
            sampled_by_region_type={cfg.region_type: dest},
        )
