"""Schema contracts for the files that enter and leave the acestor pipelines.

A contract states the columns a table must have, what each holds, and which
columns identify a row. `validate` checks a table against one. The contracts
for prepared data and predictions describe what the pipeline code reads and
writes; `aggregated_cases` is the contract a user's aggregated counts must
meet once their own column names are mapped onto it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from report import FAIL, PASS, Check

DATE, WHOLE, NUMBER, TEXT = "date", "whole number", "number", "text"


@dataclass(frozen=True)
class Column:
    name: str
    kind: str
    meaning: str
    minimum: float | None = None
    maximum: float | None = None
    required: bool = True


@dataclass(frozen=True)
class Contract:
    name: str
    purpose: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]


AGGREGATED_CASES = Contract(
    name="aggregated_cases",
    purpose="Aggregated case counts supplied by a user, after column mapping.",
    columns=(
        Column("unit", TEXT, "Administrative unit code, matching the boundary file."),
        Column("date", DATE, "The day counted, or the day a weekly total is dated on."),
        Column("cases", WHOLE, "Cases in that unit on that date or week.", minimum=0),
    ),
    key=("unit", "date"),
)

PREPARED_CASES = Contract(
    name="prepared_cases",
    purpose="prepared_data/<level>/cases_daily.csv, read by the forecast pipeline.",
    columns=(
        Column("region_id", TEXT, "Unit ID in the form <level>_<code>."),
        Column("date", DATE, "Case date, YYYY-MM-DD."),
        Column("case_count", WHOLE, "Cases on that date. A day without a row is "
               "read as zero, up to the unit's last row.", minimum=0),
    ),
    key=("region_id", "date"),
)

PREPARED_WEATHER = Contract(
    name="prepared_weather",
    purpose="prepared_data/<level>/weather_daily.csv, written by the prep pipeline.",
    columns=(
        Column("region_id", TEXT, "Unit ID in the form <level>_<code>."),
        Column("date", DATE, "Observation date, YYYY-MM-DD."),
        Column("2mTemperature", NUMBER, "Daily mean air temperature, Kelvin.",
               minimum=230, maximum=330),
        Column("2mDewpointTemperature", NUMBER, "Daily mean dewpoint, Kelvin.",
               minimum=220, maximum=320),
        Column("totalPrecipitation", NUMBER, "Daily total precipitation, metres.",
               minimum=0, maximum=2),
    ),
    key=("region_id", "date"),
)

PREDICTIONS = Contract(
    name="predictions",
    purpose="outputs/predictions.csv, written by the forecast pipeline.",
    columns=(
        Column("regionID", TEXT, "Unit ID."),
        Column("startDatePredictedWeek", DATE, "Last day of the forecast week."),
        Column("dateOfComputingPrediction", DATE, "Run date."),
        Column("model", TEXT, "Model that produced the forecast."),
        Column("thresholdMethod", TEXT, "Risk threshold method for the row."),
        Column("predictionRaw", NUMBER, "Forecast cases before rounding.", minimum=0),
        Column("prediction", NUMBER, "Forecast cases, rounded.", minimum=0),
    ),
    key=("regionID", "startDatePredictedWeek", "model", "thresholdMethod"),
)

BOUNDARY_PROPERTIES = ("region_id", "name", "parent")
ALL = (AGGREGATED_CASES, PREPARED_CASES, PREPARED_WEATHER, PREDICTIONS)


def _wrong_kind(values: pd.Series, kind: str) -> int:
    """Rows whose value does not fit the column's kind."""
    if kind == TEXT:
        return int((values.astype(str).str.strip() == "").sum())
    if kind == DATE:
        return int(pd.to_datetime(values, errors="coerce").isna().sum())
    numbers = pd.to_numeric(values, errors="coerce")
    if kind == WHOLE:
        return int((numbers.isna() | (numbers % 1 != 0)).sum())
    return int(numbers.isna().sum())


def validate(table: pd.DataFrame, contract: Contract, key: bool = True) -> list[Check]:
    """Check a table against a contract: columns, kinds, ranges, blanks, key.

    Pass key=False to check the values alone, for rows that are deliberately
    not yet one per key (counts still split into groups).
    """
    prefix = contract.name
    required = [c for c in contract.columns if c.required]
    missing = [c.name for c in required if c.name not in table.columns]
    if missing:
        return [Check(f"{prefix}_columns", FAIL,
                      f"Missing columns {missing}; have {list(table.columns)}.")]
    checks = [Check(f"{prefix}_columns", PASS, "All required columns are present.")]
    for column in required:
        values = table[column.name]
        blank = int(values.isna().sum())
        wrong = _wrong_kind(values.dropna(), column.kind)
        problems = []
        if blank:
            problems.append(f"{blank} blank")
        if wrong:
            problems.append(f"{wrong} not a {column.kind}")
        if column.kind in (WHOLE, NUMBER):
            numbers = pd.to_numeric(values, errors="coerce")
            if column.minimum is not None and (numbers < column.minimum).any():
                problems.append(f"{int((numbers < column.minimum).sum())} below {column.minimum:g}")
            if column.maximum is not None and (numbers > column.maximum).any():
                problems.append(f"{int((numbers > column.maximum).sum())} above {column.maximum:g}")
        checks.append(Check(
            f"{prefix}_{column.name}", FAIL if problems else PASS,
            f"{column.name}: " + ", ".join(problems) + "." if problems
            else f"{column.name}: every value is a valid {column.kind}.",
        ))
    if not key:
        return checks
    repeated = int(table.duplicated(list(contract.key)).sum())
    checks.append(Check(
        f"{prefix}_key_unique", FAIL if repeated else PASS,
        f"{repeated} rows repeat the key {list(contract.key)}." if repeated
        else f"One row per {' and '.join(contract.key)}.",
    ))
    return checks


def describe(contract: Contract) -> str:
    """The contract as a Markdown table, for documentation."""
    lines = [f"### {contract.name}", "", contract.purpose, "",
             "| Column | Holds | Meaning |", "|---|---|---|"]
    for column in contract.columns:
        limits = []
        if column.minimum is not None:
            limits.append(f"at least {column.minimum:g}")
        if column.maximum is not None:
            limits.append(f"at most {column.maximum:g}")
        holds = column.kind + (f" ({', '.join(limits)})" if limits else "")
        lines.append(f"| `{column.name}` | {holds} | {column.meaning} |")
    lines += ["", f"One row per {' and '.join(contract.key)}.", ""]
    return "\n".join(lines)
