"""Cutoff-date estimation.

Translated from GBA ``IdentifyCutoffDates.py``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

log = logging.getLogger(__name__)


def estimate_cutoff_date(
    df: pd.DataFrame, min_regions: int = 10
) -> pd.Timestamp | None:
    """Find the most recent date where at least ``min_regions`` have data."""
    summary = df.groupby("recordDate").size().reset_index(name="n_regions")
    all_dates = sorted(df["recordDate"].unique(), reverse=True)
    for d in all_dates:
        n = summary.loc[summary["recordDate"] == d, "n_regions"].iloc[0]
        if n >= min_regions:
            return pd.Timestamp(d)
    log.warning(
        "cutoffs: no date found where at least %d region(s) have data "
        "(max regions on any single date: %d across %d dates) → "
        "returning None, pipeline will raise downstream",
        min_regions,
        summary["n_regions"].max() if not summary.empty else 0,
        len(all_dates),
    )
    return None


def compute_prediction_dates(
    cutoff: pd.Timestamp,
    pred_upto: pd.Timestamp,
) -> list[str]:
    """Derive the list of prediction-week start dates."""
    n_weeks = max(0, int((pred_upto - cutoff).days / 7))
    if n_weeks == 0:
        log.warning(
            "cutoffs: prediction horizon collapsed to 0 weeks "
            "(cutoff=%s, pred_upto=%s, gap=%d days) → no prediction dates will be generated; "
            "maps will be empty",
            cutoff.date(),
            pred_upto.date(),
            (pred_upto - cutoff).days,
        )
    raw = [(pred_upto - timedelta(days=i * 7)).date() for i in range(n_weeks)]
    raw = sorted(raw)[-4:]
    return [d.strftime("%Y-%m-%d") for d in raw]


def identify_cutoff_dates(
    cutoff_case: pd.Timestamp | None,
    cutoff_weather: pd.Timestamp | None,
    *,
    horizon_anchor: str = "weather_cutoff",
    horizon_weeks: int = 4,
    run_date: pd.Timestamp | None = None,
    sampling_dayofweek: int | None = None,
) -> tuple[pd.Timestamp, pd.Timestamp, list[str]]:
    """Determine the training cutoff, prediction horizon, and prediction dates.

    Returns ``(cutoff, pred_upto, prediction_dates)``.

    ``horizon_anchor`` picks how ``pred_upto`` is computed:
      * ``weather_cutoff`` (default, legacy): ``cutoff_weather + horizon_weeks*7d``.
        Horizon wobbles based on the case-vs-weather cutoff gap.
      * ``case_cutoff``: ``cutoff_case + horizon_weeks*7d``. Always ``horizon_weeks``
        target weeks, anchored on the case cutoff.
      * ``today``: ``today aligned to sampling_dayofweek + (horizon_weeks-1)*7d``.
        Always ``horizon_weeks`` target weeks anchored on today's calendar week.
        Requires ``run_date`` and ``sampling_dayofweek`` (0=Mon..6=Sun) to
        align pred_upto to the same weekday as other dates.
    """
    if cutoff_case is None or cutoff_weather is None:
        raise ValueError(
            "Cannot identify cutoff dates when case or weather cutoff is None."
        )
    horizon_days = timedelta(days=horizon_weeks * 7)

    if horizon_anchor == "weather_cutoff":
        if cutoff_case < (cutoff_weather + horizon_days):
            cutoff = cutoff_case
            pred_upto = cutoff_weather + horizon_days
            log.info(
                "cutoffs: anchor=weather_cutoff, case-data-limited — case cutoff (%s) "
                "is before weather+%dd (%s); training cutoff=%s, pred_upto=%s",
                cutoff_case.date(),
                horizon_weeks * 7,
                (cutoff_weather + horizon_days).date(),
                cutoff.date(),
                pred_upto.date(),
            )
        else:
            cutoff = cutoff_weather + horizon_days
            pred_upto = cutoff
            log.info(
                "cutoffs: anchor=weather_cutoff, weather-data-limited — case cutoff "
                "(%s) is NOT before weather+%dd (%s); training cutoff=pred_upto=%s "
                "(prediction horizon may be zero)",
                cutoff_case.date(),
                horizon_weeks * 7,
                (cutoff_weather + horizon_days).date(),
                cutoff.date(),
            )
    elif horizon_anchor == "case_cutoff":
        cutoff = cutoff_case
        pred_upto = cutoff_case + horizon_days
        log.info(
            "cutoffs: anchor=case_cutoff — training cutoff=%s, pred_upto=%s "
            "(case+%dd)",
            cutoff.date(),
            pred_upto.date(),
            horizon_weeks * 7,
        )
    elif horizon_anchor == "today":
        if run_date is None or sampling_dayofweek is None:
            raise ValueError(
                "horizon_anchor='today' requires run_date and sampling_dayofweek."
            )
        # Align today to the sampling weekday (0=Mon..6=Sun): step FORWARD to
        # the next sampling day (or today if today is already that weekday).
        # This makes "this week's forecast target" the coming sampling day —
        # e.g. today=Wed, sampling=Fri → anchor is Friday of the same ISO week
        # (2 days from now, not 5 days ago).
        offset = (sampling_dayofweek - run_date.dayofweek) % 7
        this_week_anchor = (run_date + timedelta(days=offset)).normalize()
        cutoff = cutoff_case
        pred_upto = this_week_anchor + timedelta(days=(horizon_weeks - 1) * 7)
        log.info(
            "cutoffs: anchor=today — this-week anchor=%s (sampling day of week=%d), "
            "training cutoff=%s (case), pred_upto=%s",
            this_week_anchor.date(),
            sampling_dayofweek,
            cutoff.date(),
            pred_upto.date(),
        )
    else:  # defensive — validated by HorizonConfig, but be explicit here too
        raise ValueError(f"unknown horizon_anchor: {horizon_anchor!r}")

    prediction_dates = compute_prediction_dates(cutoff, pred_upto)

    # Diagnostic: did we emit the expected number of target weeks?
    actual_weeks = len(prediction_dates)
    if actual_weeks < horizon_weeks:
        reason = _horizon_shortfall_reason(
            horizon_anchor=horizon_anchor,
            horizon_weeks=horizon_weeks,
            actual_weeks=actual_weeks,
            cutoff_case=cutoff_case,
            cutoff_weather=cutoff_weather,
            cutoff=cutoff,
            pred_upto=pred_upto,
        )
        log.warning(
            "cutoffs: emitted %d target weeks, expected %d. anchor=%s. %s",
            actual_weeks,
            horizon_weeks,
            horizon_anchor,
            reason,
        )
    else:
        log.info(
            "cutoffs: emitted %d target weeks (expected %d). anchor=%s",
            actual_weeks,
            horizon_weeks,
            horizon_anchor,
        )

    return cutoff, pred_upto, prediction_dates


def _horizon_shortfall_reason(
    *,
    horizon_anchor: str,
    horizon_weeks: int,
    actual_weeks: int,
    cutoff_case: pd.Timestamp,
    cutoff_weather: pd.Timestamp,
    cutoff: pd.Timestamp,
    pred_upto: pd.Timestamp,
) -> str:
    """Produce a human-readable explanation for why fewer weeks were emitted."""
    if horizon_anchor == "weather_cutoff":
        gap_days = (cutoff_weather - cutoff_case).days
        expected_pred_upto = cutoff_weather + timedelta(days=horizon_weeks * 7)
        return (
            f"case_cutoff={cutoff_case.date()}, weather_cutoff={cutoff_weather.date()} "
            f"(weather is {gap_days:+d} days from case). pred_upto={pred_upto.date()} "
            f"but training cutoff={cutoff.date()} — gap only "
            f"{(pred_upto - cutoff).days} days, producing {actual_weeks} weeks. "
            f"To force {horizon_weeks} weeks anchored on the case cutoff, set "
            f"cutoff.horizon.anchor=case_cutoff. To anchor on today, use "
            f"anchor=today. Expected pred_upto if weather were current: "
            f"{expected_pred_upto.date()}."
        )
    if horizon_anchor == "case_cutoff":
        return (
            f"case_cutoff={cutoff_case.date()}, pred_upto={pred_upto.date()} — "
            f"only {actual_weeks} sampling-day slots fell inside "
            f"({pred_upto - cutoff}). This should not happen for "
            f"horizon_weeks*7 gap; check sampling day alignment."
        )
    if horizon_anchor == "today":
        return (
            f"today-anchored pred_upto={pred_upto.date()}, training cutoff="
            f"{cutoff.date()} (case). {actual_weeks} sampling-day slots fell "
            f"inside — usually indicates case data is far enough in the past "
            f"that some target weeks map to the same sampling week after "
            f"floor-to-week."
        )
    return f"unknown anchor={horizon_anchor!r}"
