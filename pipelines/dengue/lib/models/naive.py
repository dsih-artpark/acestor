"""Naive persistence baseline for weekly dengue case forecasts.

For every region, repeat its latest available weekly case count at or before
the shared case-data cutoff across every requested forecast week.  The model
uses cases only: it has no training, weather, lag, or tuning requirements.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

MODEL_NAME = "naivePersistence"


def naive_persistence_forecast(
    *,
    case_df: pd.DataFrame,
    spatial_col: str,
    cutoff_case: pd.Timestamp,
    prediction_dates: list[str],
    logger: Any = None,
) -> pd.DataFrame:
    """Repeat each region's latest observed case count over the target dates.

    A region whose most recent usable observation predates ``cutoff_case`` is
    retained using that value and reported through a warning.  Rows after the
    cutoff are ignored so hindcasts cannot leak future observations.
    """
    columns = [spatial_col, "recordDate", "prediction", "model"]
    empty = pd.DataFrame(columns=columns)
    if not prediction_dates:
        return empty

    required = {spatial_col, "recordDate", "case"}
    missing = sorted(required - set(case_df.columns))
    if missing:
        raise ValueError(f"naive: case data is missing required columns: {missing}")

    anchor = pd.Timestamp(cutoff_case).normalize()
    targets: list[pd.Timestamp] = []
    for raw_date in prediction_dates:
        target = pd.to_datetime(raw_date, errors="coerce")
        if pd.isna(target):
            raise ValueError(f"naive: invalid prediction date {raw_date!r}")
        target = pd.Timestamp(target).normalize()
        delta_days = (target - anchor).days
        if delta_days <= 0:
            raise ValueError(
                f"naive: prediction date {target.date()} is not strictly after "
                f"cutoff_case {anchor.date()}"
            )
        if delta_days % 7 != 0:
            raise ValueError(
                f"naive: prediction date {target.date()} is off the weekly grid "
                f"anchored at cutoff_case {anchor.date()}"
            )
        targets.append(target)

    history = case_df[[spatial_col, "recordDate", "case"]].copy()
    all_regions = {str(v) for v in history[spatial_col].dropna().unique()}
    history["recordDate"] = pd.to_datetime(
        history["recordDate"], errors="coerce"
    ).dt.normalize()
    history["case"] = pd.to_numeric(history["case"], errors="coerce")
    history = history.dropna(subset=[spatial_col, "recordDate", "case"])
    history = history[history["recordDate"] <= anchor]
    if history.empty:
        return empty

    latest = (
        history.sort_values("recordDate")
        .groupby(spatial_col, sort=False, as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )

    event_log = logger or log
    forecast_regions = {str(v) for v in latest[spatial_col].unique()}
    skipped_regions = sorted(all_regions - forecast_regions)
    if skipped_regions:
        event_log.warning(
            "naive: %d region(s) have no usable case observation at or before "
            "cutoff_case=%s and will be skipped: %s",
            len(skipped_regions),
            anchor.date(),
            skipped_regions,
        )

    stale = latest[latest["recordDate"] < anchor]
    if not stale.empty:
        stale_regions = sorted(str(v) for v in stale[spatial_col].unique())
        max_staleness_days = int((anchor - stale["recordDate"]).dt.days.max())
        event_log.warning(
            "naive: %d region(s) use their latest observation before "
            "cutoff_case=%s (maximum staleness=%d day(s)): %s",
            len(stale_regions),
            anchor.date(),
            max_staleness_days,
            stale_regions,
        )

    rows = [
        {
            spatial_col: row[spatial_col],
            "recordDate": target,
            "prediction": float(row["case"]),
            "model": MODEL_NAME,
        }
        for row in latest.to_dict("records")
        for target in targets
    ]
    return pd.DataFrame(rows, columns=columns)


from pipelines.dengue.lib.models import ModelContext, register  # noqa: E402


@register("naive")
class NaivePersistenceModel:
    def predict(self, ctx: ModelContext) -> pd.DataFrame:
        return naive_persistence_forecast(
            case_df=ctx.case_df,
            spatial_col=ctx.cfg.spatial_res,
            cutoff_case=pd.Timestamp(ctx.cutoff_case),
            prediction_dates=list(ctx.prediction_dates or []),
            logger=ctx.log,
        )

    def threshold_to_date(self, ctx: ModelContext) -> pd.Timestamp:
        return pd.Timestamp(ctx.cutoff_case)
