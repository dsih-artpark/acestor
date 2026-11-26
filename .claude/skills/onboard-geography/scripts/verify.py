"""Verification after each pipeline run: prepared data, then predictions."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from contracts import PREDICTIONS, PREPARED_CASES, PREPARED_WEATHER, validate
from qc import MIN_RECENT_CASES, PLAUSIBLE_FACTOR
from report import FAIL, PASS, WARN, Check, failed

FORECAST_WEEKS = 4


def matches_inspected(prepared: pd.DataFrame, expected_file: Path,
                      boundary_units: set[str]) -> Check:
    """Prepared cases against the counts `inspect` read, per unit and date.

    A total can survive a day/month swap or cases landing in the wrong unit.
    A row-by-row comparison cannot.
    """
    expected = pd.read_csv(expected_file, dtype={"region_id": str})
    expected = expected[expected.region_id.isin(boundary_units)]
    merged = expected.merge(prepared, on=["region_id", "date"], how="outer")
    differ = merged[merged.cases.fillna(0) != merged.case_count.fillna(0)]
    return Check(
        "prepared_matches_inspected", FAIL if len(differ) else PASS,
        f"{len(differ)} unit/date counts differ between the source file and the "
        "prepared data." if len(differ)
        else f"All {len(expected)} unit/date counts match the source file.",
        {"examples": differ.head(5).to_dict("records")})


def tuned_hyperparameters(run_dir: Path) -> dict:
    """Settings chosen by the model search, kept so a forecast can be traced to them."""
    return {path.stem.removesuffix("_best_params"): json.loads(path.read_text())
            for path in sorted((run_dir.parent / "hp").glob("*_best_params.json"))}


def series_reach_run_date(table: pd.DataFrame, run_date: str) -> Check:
    """Units whose last row is before the run date.

    The pipeline ends each unit's series at its own last row, not at the run
    date. Such a unit is forecast from older weeks, or as zero, with no error.
    """
    last = pd.to_datetime(table.date).groupby(table.region_id).max()
    short = (pd.Timestamp(run_date) - last).dt.days
    short = short[short > 0].sort_values(ascending=False)
    return Check(
        "series_reach_run_date", WARN if len(short) else PASS,
        f"{len(short)} units have their last row before the run date. The pipeline "
        "ends their series there, so their forecasts rest on older weeks and can "
        "come out as zero. Compare these units' forecasts with their recent counts."
        if len(short) else "Every unit's series reaches the run date.",
        {"days_short_per_unit": short.to_dict()})


def prepared_cases(path: Path, boundary_units: set[str], expected_total: int | None,
                   expected_file: Path | None = None,
                   run_date: str | None = None) -> tuple[dict, list[Check]]:
    if not path.is_file():
        return {}, [Check("prepared_cases_present", FAIL, f"Missing {path}.")]
    table = pd.read_csv(path, dtype={"region_id": str})
    if "case" in table.columns and "case_count" not in table.columns:
        table = table.rename(columns={"case": "case_count"})
    checks = validate(table, PREPARED_CASES)
    total = int(pd.to_numeric(table.case_count, errors="coerce").sum())
    stray = sorted(set(table.region_id) - boundary_units)
    checks.append(Check("prepared_units_known", FAIL if stray else PASS,
                        f"Units without a boundary: {stray}." if stray
                        else "All prepared units have a boundary."))
    if expected_total is not None:
        checks.append(Check(
            "prepared_total_as_expected", PASS if total == expected_total else FAIL,
            f"Prepared total {total}, expected {expected_total}."))
    if expected_file is not None and expected_file.is_file():
        checks.append(matches_inspected(table, expected_file, boundary_units))
    if run_date is not None and not failed(checks):
        checks.append(series_reach_run_date(table, run_date))
    return {"prepared_cases": total, "prepared_rows": len(table)}, checks


def prepared_weather(path: Path, boundary_units: set[str], start: str,
                     end: str) -> list[Check]:
    """Weather must cover every unit for every day the model will read."""
    if not path.is_file():
        return [Check("weather_present", FAIL, f"Missing {path}.")]
    table = pd.read_csv(path, dtype={"region_id": str})
    checks = validate(table, PREPARED_WEATHER)
    missing = sorted(boundary_units - set(table.region_id))
    checks.append(Check("weather_all_units", FAIL if missing else PASS,
                        f"No weather for units: {missing}." if missing
                        else "Weather present for every unit."))
    dates = pd.to_datetime(table.date, errors="coerce")
    expected = len(pd.date_range(start, end))
    days = table[dates.between(start, end)].groupby("region_id").date.nunique()
    short = sorted(days.index[days < expected])
    checks.append(Check("weather_all_days", FAIL if short else PASS,
                        f"Units missing days between {start} and {end}: {short}."
                        if short else f"Every unit has all {expected} days from "
                        f"{start} to {end}."))
    return checks


def forecast_near_recent(table: pd.DataFrame, observed_file: Path) -> Check:
    """First forecast week against the last four observed weeks, per unit.

    A run can finish with success and still forecast near zero, for example
    when a unit's series stops before the run date. A real turn of the season
    can look the same, so this warns and a person decides.
    """
    observed = pd.read_csv(observed_file)
    unit = next(c for c in observed.columns if c.startswith("location."))
    recent = (observed.sort_values("metadata.primaryDate").groupby(unit).case
              .apply(lambda cases: cases.tail(4).mean()))
    first_week = table[table.startDatePredictedWeek == table.startDatePredictedWeek.min()]
    forecast = first_week.groupby("regionID").predictionRaw.mean()
    both = pd.DataFrame({"recent": recent, "forecast": forecast}).dropna()
    both = both[both.recent >= MIN_RECENT_CASES]
    apart = both[(both.forecast < both.recent / PLAUSIBLE_FACTOR)
                 | (both.forecast > both.recent * PLAUSIBLE_FACTOR)]
    return Check(
        "forecast_near_recent_counts", WARN if len(apart) else PASS,
        f"{len(apart)} of {len(both)} units are forecast at under half or over twice "
        "their mean of the last four observed weeks. Check the run date and each "
        "unit's last row before using these numbers." if len(apart)
        else "First-week forecasts are within a factor of two of recent counts.",
        {"units": apart.round(1).to_dict("index")})


def predictions(path: Path, boundary_units: set[str],
                observed_file: Path | None = None) -> tuple[dict, list[Check]]:
    """A finished forecast should cover every unit for each forecast week."""
    if not path.is_file():
        return {}, [Check("predictions_present", FAIL, f"Missing {path}.")]
    table = pd.read_csv(path, dtype={"regionID": str})
    checks = validate(table, PREDICTIONS)
    if checks[0].status == FAIL:
        return {}, checks
    missing = sorted(boundary_units - set(table.regionID))
    checks.append(Check("forecast_all_units", WARN if missing else PASS,
                        f"{len(missing)} units have no forecast, usually from too "
                        f"little history or missing weather: {missing}." if missing
                        else "Every unit has a forecast."))
    weeks = table.groupby(["regionID", "model"]).startDatePredictedWeek.nunique()
    short = int((weeks < FORECAST_WEEKS).sum())
    checks.append(Check("forecast_all_weeks", WARN if short else PASS,
                        f"{short} unit/model pairs have fewer than {FORECAST_WEEKS} "
                        "forecast weeks." if short else
                        f"Every unit/model pair has {FORECAST_WEEKS} forecast weeks."))
    if observed_file is not None and observed_file.is_file():
        checks.append(forecast_near_recent(table, observed_file))
    return {"models": sorted(table.model.unique()),
            "forecast_weeks": sorted(table.startDatePredictedWeek.unique()),
            "prediction_rows": len(table)}, checks
