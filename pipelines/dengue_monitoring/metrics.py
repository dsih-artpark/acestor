"""Pure error calculations; no fetching, forecasting or notification side effects."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .actuals import validate_weekly

GROUP = ["scope", "spatial_scale", "derivation", "model", "lead"]
SERIES = GROUP + ["region_id"]
QUANTILES = (5, 10, 25, 50, 75, 90, 95)


def score(
    forecasts: pd.DataFrame,
    daily: pd.DataFrame,
    as_of: str,
    *,
    frequency: str = "daily",
) -> pd.DataFrame:
    """Score exact seven-day windows against sparse prepared daily case counts.

    Internal absent dates mean zero, as in acestor preparation. Outside the
    observed file's global date extent, or for unknown regions, actuals remain
    unavailable. This is an availability boundary, NOT reporting completeness.
    """
    if frequency == "weekly":
        return score_weekly(forecasts, daily, as_of)
    if frequency != "daily":
        raise ValueError("Actuals frequency must be daily or weekly")
    daily = daily.copy()
    daily["region_id"] = daily.region_id.astype(str)
    daily["date"] = pd.to_datetime(daily.date).dt.normalize()
    daily["case_count"] = pd.to_numeric(daily.case_count, errors="raise")
    if daily.empty or daily[["region_id", "date", "case_count"]].isna().any().any():
        raise ValueError("Actuals must contain non-null region_id, date, case_count")
    if not np.isfinite(daily.case_count).all() or (daily.case_count < 0).any():
        raise ValueError("Actual case counts must be finite and non-negative")
    if daily.duplicated(["region_id", "date"]).any():
        raise ValueError("Duplicate region/date rows in prepared actuals")
    lower, upper = daily.date.min(), daily.date.max()
    by_region = {
        str(k): g.set_index("date").case_count for k, g in daily.groupby("region_id")
    }
    records = []
    for row in forecasts.to_dict("records"):
        start, end = pd.Timestamp(row["target_start"]), pd.Timestamp(row["target_end"])
        actual, error = np.nan, np.nan
        if end > pd.Timestamp(as_of):
            status = "pending"
        elif row["region_id"] not in by_region or start < lower or end > upper:
            status = "missing_actual"
        else:
            actual = float(
                by_region[row["region_id"]]
                .reindex(pd.date_range(start, end), fill_value=0)
                .sum()
            )
            if pd.isna(row["predicted_cases"]):
                status = "missing_prediction"
            elif actual == 0:
                status = "zero_actual"
            else:
                status = "evaluated"
                error = (row["predicted_cases"] - actual) / actual
        records.append(
            {
                **row,
                "actual_cases": actual,
                "signed_error": error,
                "status": status,
                "actuals_maturity": "unverified",
                "negative_prediction": bool(row["predicted_cases"] < 0),
            }
        )
    return pd.DataFrame(records)


def score_weekly(
    forecasts: pd.DataFrame, weekly: pd.DataFrame, as_of: str
) -> pd.DataFrame:
    """Join exact windows; absent weekly rows are never zero-filled or shifted."""
    weekly = weekly.copy()
    for column in ("target_start", "target_end"):
        weekly[column] = pd.to_datetime(weekly[column])
    validate_weekly(weekly)
    # An absent week on the right calendar is missing data. A different weekly
    # calendar is a configuration mismatch and must not silently look like gaps.
    for region, group in forecasts.groupby("region_id"):
        observed = weekly[weekly.region_id == region]
        if not observed.empty and not set(
            pd.to_datetime(group.target_end).dt.dayofweek
        ) <= set(observed.target_end.dt.dayofweek):
            raise ValueError(
                f"Weekly actuals window alignment mismatch for region {region}; do not shift weekly totals"
            )
    out = forecasts.merge(
        weekly[["region_id", "target_start", "target_end", "actual_cases"]],
        on=["region_id", "target_start", "target_end"],
        how="left",
        validate="many_to_one",
    )
    pending = pd.to_datetime(out.target_end) > pd.Timestamp(as_of)
    out.loc[pending, "actual_cases"] = np.nan
    out["status"] = np.select(
        [
            pending,
            out.actual_cases.isna(),
            out.predicted_cases.isna(),
            out.actual_cases.eq(0),
        ],
        ["pending", "missing_actual", "missing_prediction", "zero_actual"],
        default="evaluated",
    )
    out["signed_error"] = np.nan
    valid = out.status.eq("evaluated")
    out.loc[valid, "signed_error"] = (
        out.loc[valid, "predicted_cases"] - out.loc[valid, "actual_cases"]
    ) / out.loc[valid, "actual_cases"]
    out["actuals_maturity"] = "unverified"
    out["negative_prediction"] = out.predicted_cases < 0
    return out


def baseline(errors: pd.DataFrame) -> pd.DataFrame:
    """Pool unit-week errors within scope/scale/model/lead; never weight years."""
    records = []
    for keys, group in errors.groupby(GROUP, sort=True):
        values = np.sort(group.signed_error.dropna().to_numpy())
        n = len(values)
        row = dict(zip(GROUP, keys))
        row.update(
            n=n,
            total=len(group),
            zero_actual=int((group.actual_cases == 0).sum()),
            excluded=len(group) - n,
            mean=float(values.mean()) if n else np.nan,
            sd=float(values.std(ddof=1)) if n > 1 else np.nan,
        )
        for q in QUANTILES:
            row[f"p{q:02}"] = float(values[math.ceil(q / 100 * n) - 1]) if n else np.nan
        for k in (1, 2):
            row[f"mean_minus_{k}sd"] = row["mean"] - k * row["sd"]
            row[f"mean_plus_{k}sd"] = row["mean"] + k * row["sd"]
        records.append(row)
    return pd.DataFrame(records)


def compare(current: pd.DataFrame, reference: pd.DataFrame) -> pd.DataFrame:
    """Strict upper-p75 flags; two consecutive target weeks alert per region.

    Undefined errors and absent calendar weeks reset the sequence. Negative
    errors independently flag underprediction and never escalate on their own.
    """
    out = current.merge(
        reference[GROUP + ["p75", "n"]], on=GROUP, how="left", validate="many_to_one"
    )
    out["baseline_available"] = out.p75.notna()
    out["upper_flag"] = out.signed_error > out.p75
    out["underprediction_flag"] = out.signed_error < 0
    out["consecutive_upper_weeks"] = 0
    out["alert"] = False
    if out.duplicated(SERIES + ["target_end"]).any():
        raise ValueError(
            "Multiple selected forecasts for the same region/model/lead/target; select one run"
        )
    for _, group in out.groupby(SERIES, sort=True):
        previous, streak = None, 0
        for index, row in group.sort_values("target_end").iterrows():
            target = pd.Timestamp(row.target_end)
            consecutive = previous is not None and target - previous == pd.Timedelta(
                days=7
            )
            streak = (streak + 1 if consecutive else 1) if row.upper_flag else 0
            out.loc[index, "consecutive_upper_weeks"] = streak
            out.loc[index, "alert"] = streak >= 2
            previous = target
    return out.sort_values(SERIES + ["target_end"]).reset_index(drop=True)
