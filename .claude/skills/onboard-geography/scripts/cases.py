"""Case data: inspect a user's file, and standardise aggregated counts.

A line list goes through the prep pipeline's parser, which counts one case
per row. Aggregated counts must not: they are standardised here into the
prepared-cases contract and written where the forecast pipeline reads them.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

import qc
from contracts import AGGREGATED_CASES, PREPARED_CASES, validate
from report import FAIL, PASS, WARN, Check, failed

Filters = list[tuple[str, list[str]]]


TABLE_TYPES = (".csv", ".xlsx", ".xls")


def source_files(path: Path) -> list[Path]:
    """A file, or every table file in a folder (data often arrives one file per year)."""
    if path.is_dir():
        return sorted(f for f in path.iterdir() if f.suffix.lower() in TABLE_TYPES)
    return [path]


def read_table(path: Path) -> pd.DataFrame:
    """Everything is read as text so codes keep leading zeros and dates stay as written."""
    def read(file: Path) -> pd.DataFrame:
        if file.suffix.lower() in (".xlsx", ".xls"):
            return pd.read_excel(file, dtype=str)
        return pd.read_csv(file, dtype=str)

    return pd.concat([read(file) for file in source_files(path)], ignore_index=True)


def check_files_have_columns(path: Path, columns: list[str]) -> Check:
    """Every file in a folder must carry the columns in use.

    Files exported in different years often differ. Put together, a file
    without the unit column just looks like rows with no unit, while the
    pipeline's parser stops on it.
    """
    lacking = {}
    for file in source_files(path):
        header = (pd.read_excel(file, nrows=0) if file.suffix.lower() in (".xlsx", ".xls")
                  else pd.read_csv(file, nrows=0)).columns
        missing = [c for c in columns if c not in header]
        if missing:
            lacking[file.name] = missing
    return Check("files_have_columns", FAIL if lacking else PASS,
                 f"{len(lacking)} files lack columns that are in use: {lacking}. "
                 "Leave those files out or ask for them in the same layout."
                 if lacking else "Every file has the columns in use.", {"files": lacking})


def blank_counts(counts: pd.Series, blank_as_zero: bool) -> tuple[pd.Series, list[Check]]:
    """A blank count is not a zero until someone says so."""
    blank = int(counts.isna().sum())
    if not blank:
        return counts, []
    if blank_as_zero:
        return counts.fillna("0"), [Check(
            "blank_counts", WARN, f"{blank} blank counts were read as zero, as instructed.")]
    return counts, [Check(
        "blank_counts", FAIL,
        f"{blank} rows have a blank count. Ask whether blank means zero cases or "
        "not reported. The pipeline cannot hold a missing count: a day left out is "
        "read as zero too. Rerun with --blank-as-zero if blank means zero, or if "
        "the user accepts the undercount. The other checks ran on the rows that "
        "have a count.")]


def parse_filters(items: list[str] | None) -> Filters:
    """'Test Result=Positive,Reactive' -> ('Test Result', ['Positive', 'Reactive'])."""
    filters = []
    for item in items or []:
        column, _, values = item.partition("=")
        filters.append((column.strip(), [v.strip() for v in values.split(",") if v.strip()]))
    return filters


def apply_filters(table: pd.DataFrame, filters: Filters) -> tuple[pd.DataFrame, list[Check]]:
    """Keep rows matching every filter, and say how many each one removed."""
    checks = []
    for column, values in filters:
        if column not in table.columns:
            return table, [Check("filters_applied", FAIL,
                                 f"Filter column {column!r} is not in the file.")]
        keep = table[column].astype(str).str.strip().isin(values)
        checks.append(Check(
            "filters_applied", PASS,
            f"Kept {int(keep.sum())} of {len(table)} rows where {column!r} is one "
            f"of {values}."))
        table = table[keep]
    return table.reset_index(drop=True), checks


def inspect(path: Path, date_col: str, unit_col: str, count_col: str | None,
            region_type: str, boundary_units: set[str] | None, today: pd.Timestamp,
            filters: Filters, blank_as_zero: bool = False,
            ) -> tuple[dict, list[Check], pd.DataFrame | None]:
    """Describe the file and run every check that applies to its kind.

    The caller names the columns: which column is the date, the unit or the
    count is a question about what the data means, and a guess from column
    names gets it wrong on real files. Also returns the cases per unit and
    date as read here, so the prepared data can be compared with it later.
    """
    table = read_table(path)
    cols = {"date": date_col, "unit": unit_col, "count": count_col}
    info = {"file": str(path), "files": len(source_files(path)), "rows": len(table),
            "columns_used": cols}
    absent = [c for c in cols.values() if c is not None and c not in table.columns]
    if absent:
        return info, [Check(
            "columns_present", FAIL,
            f"Columns {absent} are not in the file. Columns: {list(table.columns)}.")], None

    table, checks = apply_filters(table, filters)
    checks = [Check("columns_present", PASS, f"Using {cols}."),
              check_files_have_columns(path, [c for c in cols.values() if c]), *checks]
    if failed(checks):
        return info, checks, None
    kind, kind_checks = qc.detect_kind(table, cols["unit"], cols["date"], cols["count"])
    info["kind"] = kind
    dates, date_check = qc.parse_dates(table[cols["date"]])
    checks += [*kind_checks, date_check, qc.check_rows_have_dates(table[cols["date"]]),
               qc.check_rows_have_units(table[cols["unit"]]),
               *qc.check_location_basis(table, cols["unit"])]
    if failed([date_check, *kind_checks]):
        return info, checks, None
    placed = dates.notna() & ~qc.is_blank(table[cols["unit"]])
    table, dates = table[placed], dates[placed]

    units = table[cols["unit"]].astype(str).str.strip()
    if kind == qc.AGGREGATED:
        raw_counts, blank_checks = blank_counts(table[cols["count"]], blank_as_zero)
        rows = pd.DataFrame({"unit": units, "date": dates, "cases": raw_counts})
        # An unresolved blank fails, and the rest is still checked on the rows
        # that have a count, so every problem shows in one pass.
        rows = rows[rows.cases.notna()]
        # Values are checked row by row, before any adding up could hide a
        # non-number.
        value_checks = validate(rows, AGGREGATED_CASES, key=False)
        checks += blank_checks + value_checks
        if failed(value_checks):
            return info, checks, None
        series = (rows.assign(cases=pd.to_numeric(rows.cases))
                  .groupby(["unit", "date"], as_index=False).cases.sum())
        checks += [qc.check_cumulative(series.unit, series.date, series.cases),
                   qc.check_outliers(series.unit, series.date, series.cases)]
    else:
        series = (pd.DataFrame({"unit": units, "date": dates})
                  .value_counts().rename("cases").reset_index())
        checks += [qc.check_parser_reads_dates(table[cols["date"]]),
                   qc.check_exact_duplicates(table),
                   qc.check_mixed_categories(table, set(cols.values())),
                   qc.check_coordinates(table)]
    frequency, frequency_check = qc.check_frequency(series.unit, series.date, kind)
    info.update(frequency, date_start=str(dates.min().date()),
                date_end=str(dates.max().date()), units=int(units.nunique()),
                total_cases=int(series.cases.sum()))
    checks += [frequency_check, qc.check_history(dates),
               qc.check_year_levels(series.date, series.cases),
               *qc.check_recency(dates, today),
               *qc.check_unit_coverage(series.unit, series.date, kind),
               qc.check_concentration(series.unit, series.cases)]
    used = {qc.region_id(region_type, unit) for unit in units.unique()}
    checks += qc.check_unit_match(used, boundary_units, region_type)
    expected = series.assign(region_id=[qc.region_id(region_type, u) for u in series.unit],
                             date=series.date.dt.strftime("%Y-%m-%d"))
    # Counts with an unresolved blank are not a reference to verify against.
    unsettled = failed([c for c in checks if c.id == "blank_counts"])
    return info, checks, None if unsettled else expected[["region_id", "date", "cases"]]


def standardise(path: Path, date_col: str, unit_col: str, count_col: str,
                region_type: str, boundary_units: set[str], out_base: Path,
                sum_breakdowns: bool, blank_as_zero: bool = False,
                ) -> tuple[dict, list[Check]]:
    """Aggregated counts -> cases_daily.csv, with every case accounted for.

    Cases in the source equal cases written plus cases set aside, overall and
    per unit. Units without a boundary are set aside and listed, never dropped
    silently. Rows that split one unit and date into groups are added together
    only when the caller says so. Nothing is written unless every check passes.
    """
    table = read_table(path)
    dates, date_check = qc.parse_dates(table[date_col])
    units = table[unit_col].astype(str).str.strip()
    raw_counts, blank_checks = blank_counts(table[count_col], blank_as_zero)
    standard = pd.DataFrame({"unit": units, "date": dates, "cases": raw_counts})
    checks = [date_check, qc.check_rows_have_dates(table[date_col]), *blank_checks]
    if date_check.status == FAIL:
        return {}, checks
    undated_total = int(pd.to_numeric(standard.cases[dates.isna()], errors="coerce").sum())
    standard = standard[dates.notna()]
    if not failed(checks):
        checks += validate(standard, AGGREGATED_CASES, key=False)
    if failed(checks):
        return {}, checks
    standard["cases"] = pd.to_numeric(standard.cases)
    repeated = int(standard.duplicated(["unit", "date"]).sum())
    if repeated and not sum_breakdowns:
        checks.append(Check(
            "breakdowns_summed", FAIL,
            f"{repeated} rows repeat a unit and date. If they are separate groups of "
            "the same cases, confirm with the user and rerun with --sum-breakdowns."))
        return {}, checks
    if repeated:
        collapsed = standard.groupby(["unit", "date"], as_index=False).cases.sum()
        checks.append(Check(
            "breakdowns_summed", WARN,
            f"{len(standard)} rows added together into {len(collapsed)} unit/date totals."))
        standard = collapsed

    frame = pd.DataFrame({
        "region_id": [qc.region_id(region_type, unit) for unit in standard.unit],
        "date": standard.date,
        "case_count": pd.to_numeric(standard.cases).astype(int),
    })
    placed = frame.region_id.isin(boundary_units)
    set_aside = frame[~placed].groupby("region_id").case_count.agg(rows="size", cases="sum")
    # Reported zeros are kept. The pipeline fills absent days with zero only up
    # to a unit's last row, so a dropped zero at the end would cut its series short.
    kept = frame[placed].sort_values(["date", "region_id"])
    source_total = int(pd.to_numeric(raw_counts).sum())
    written_total, set_aside_total = int(kept.case_count.sum()), int(set_aside.cases.sum())
    per_unit_source = frame[placed].groupby("region_id").case_count.sum()
    per_unit_written = kept.groupby("region_id").case_count.sum().reindex(
        per_unit_source.index, fill_value=0)
    differing = sorted(per_unit_source.index[per_unit_source != per_unit_written])

    checks += [
        Check("cases_reconcile",
              PASS if source_total == written_total + set_aside_total + undated_total
              else FAIL,
              f"Source {source_total} = written {written_total} + set aside "
              f"{set_aside_total} + undated {undated_total}."),
        Check("unit_totals_reconcile", FAIL if differing else PASS,
              f"Totals differ for units: {differing}." if differing
              else f"Totals match for all {len(per_unit_source)} units."),
        Check("units_set_aside", WARN if len(set_aside) else PASS,
              f"{len(set_aside)} units have no boundary; their {set_aside_total} cases "
              "were not written." if len(set_aside) else "No units set aside.",
              {"units": set_aside.to_dict("index")}),
    ]
    output = kept.assign(date=kept.date.dt.strftime("%Y-%m-%d"))
    checks += validate(output, PREPARED_CASES)
    info = {"source_cases": source_total, "written_cases": written_total,
            "set_aside_cases": set_aside_total, "undated_cases": undated_total}
    if failed(checks):
        return info, checks

    target = out_base / region_type / "cases_daily.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(target, index=False)
    return {**info, "written": str(target), "rows": len(output),
            "date_start": output.date.min(), "date_end": output.date.max()}, checks
