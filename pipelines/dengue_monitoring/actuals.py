"""Normalize daily counts or explicitly labelled weekly totals."""

from pathlib import Path

import numpy as np
import pandas as pd


def validate_actuals_config(config: dict) -> str:
    frequency = config.get("frequency", "daily")
    if frequency not in ("daily", "weekly"):
        raise ValueError("actuals.frequency must be daily or weekly")
    if frequency == "weekly":
        if not config.get("path"):
            raise ValueError("Weekly actuals require actuals.path")
        if config.get("date_column") and config.get("week_label") not in (
            "start",
            "end",
        ):
            raise ValueError("Labelled weekly actuals require week_label: start or end")
        if config.get("week_label") and not config.get("date_column"):
            raise ValueError("week_label requires date_column")
    return frequency


def _dates(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, errors="raise")
    if (
        parsed.isna().any()
        or parsed.dt.tz is not None
        or not parsed.eq(parsed.dt.normalize()).all()
    ):
        raise ValueError("Actuals dates must be non-null, timezone-free calendar dates")
    return parsed


def load_actuals(config: dict, default_path: Path) -> tuple[pd.DataFrame, Path, str]:
    frequency = validate_actuals_config(config)
    path = Path(config.get("path", default_path))
    region = config.get("region_column", "region_id")
    value = config.get(
        "value_column", "case_count" if frequency == "daily" else "actual_cases"
    )
    raw = pd.read_csv(path, dtype={region: str})
    if raw.empty or raw[region].isna().any() or raw[region].str.strip().eq("").any():
        raise ValueError("Actuals require nonempty region identifiers")
    counts = pd.to_numeric(raw[value], errors="raise")
    if (counts.dropna() < 0).any() or not np.isfinite(counts.dropna()).all():
        raise ValueError("Actual counts must be finite and non-negative when supplied")
    out = pd.DataFrame({"region_id": raw[region]})
    if frequency == "daily":
        out["date"] = _dates(raw[config.get("date_column", "date")])
        out["case_count"] = counts
    else:
        if config.get("date_column"):
            label = _dates(raw[config["date_column"]])
            out["target_start"] = (
                label
                if config["week_label"] == "start"
                else label - pd.Timedelta(days=6)
            )
            out["target_end"] = out.target_start + pd.Timedelta(days=6)
        else:
            out["target_start"] = _dates(
                raw[config.get("start_column", "target_start")]
            )
            out["target_end"] = _dates(raw[config.get("end_column", "target_end")])
        out["actual_cases"] = counts
        validate_weekly(out)
    return out, path, frequency


def validate_weekly(weekly: pd.DataFrame) -> None:
    required = ["region_id", "target_start", "target_end", "actual_cases"]
    if not set(required) <= set(weekly):
        raise ValueError(f"Weekly actuals require {required}")
    if weekly.empty or weekly[required[:-1]].isna().any().any():
        raise ValueError("Weekly actuals require region IDs and both window dates")
    if not (weekly.target_end - weekly.target_start).eq(pd.Timedelta(days=6)).all():
        raise ValueError(
            "Weekly actual windows must cover exactly seven inclusive days"
        )
    if weekly.duplicated(["region_id", "target_start", "target_end"]).any():
        raise ValueError("Duplicate weekly actuals for the same region and window")
    if (weekly.actual_cases.dropna() < 0).any() or not np.isfinite(
        weekly.actual_cases.dropna()
    ).all():
        raise ValueError("Actual counts must be finite and non-negative when supplied")
    for _, group in weekly.groupby("region_id"):
        ordered = group.sort_values("target_start")
        if (ordered.target_start.diff().dropna() < pd.Timedelta(days=7)).any():
            raise ValueError("Overlapping weekly actual windows within a region")
