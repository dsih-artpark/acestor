"""Quality checks on case data: what a schema contract cannot express.

A contract says each row is well formed. These checks ask whether the file
means what the pipeline will assume it means: how dates should be read,
whether rows are cases or counts, whether locations are where patients live,
whether records are regular, and whether any unit goes silent. Nothing here
changes the data.
"""

from __future__ import annotations

import re

import pandas as pd

from report import FAIL, PASS, WARN, Check

MIN_WEEKS_FOR_FORECAST = 52
RECOMMENDED_YEARS = 3
LONG_GAP_WEEKS = 8
OUTLIER_FACTOR = 10
YEAR_FACTOR = 1.5
PLAUSIBLE_FACTOR = 2
MIN_RECENT_CASES = 3
MAX_GROUP_LABELS = 50
DATASET_GAP_DAYS = 14
AGGREGATED, LINE_LIST = "aggregated", "line_list"
DAY_FIRST_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y")
MONTH_FIRST_FORMATS = ("%m-%d-%Y", "%m/%d/%Y")
ZONED_TIME = r"\d{2}:\d{2}.*(?:Z|[+-]\d{2}:?\d{2})$"
PERSON_HINTS = ("age", "sex", "gender", "patient", "result", "onset", "sample",
                "symptom", "outcome")
FACILITY_HINTS = ("facility", "hospital", "clinic", "lab", "phc", "reporting",
                  "institution", "health cent", "testing")
CATEGORY_HINTS = ("result", "disease", "diagnosis", "test", "status", "pathogen",
                  "classification")
TOTAL_LABELS = {"total", "all", "all ages", "overall", "both", "both sexes"}
CUMULATIVE_SHARE = 0.95
CONCENTRATION_SHARE = 0.6
FEW_POINTS_SHARE = 0.5


def region_id(region_type: str, code: object) -> str:
    """acestor identifies a unit as '<level>_<code>', e.g. district_344."""
    text = str(code).strip()
    prefix = f"{region_type}_"
    return text if text.startswith(prefix) else prefix + text


def _has_hint(name: str, hints: tuple[str, ...]) -> bool:
    """True when the column's own name starts a word with a hint.

    Only the last part of a dotted name counts ('result' in
    'event.test.test1.result'), so a shared prefix does not match every column,
    and 'lab' matches 'Lab Name' but not 'Available'.
    """
    own = name.lower().rsplit(".", 1)[-1]
    return re.search(r"(?<![a-z])(" + "|".join(hints) + ")", own) is not None


def check_rows_have_units(values: pd.Series) -> Check:
    """A row without a unit cannot be placed on the map, so it is left out and counted."""
    blank = int(is_blank(values).sum())
    return Check("rows_have_units", WARN if blank else PASS,
                 f"{blank} of {len(values)} rows have no unit and are left out."
                 if blank else "Every row has a unit.", {"rows_without_unit": blank})


# --- dates -----------------------------------------------------------------

def _parse_with(text: pd.Series, formats: tuple[str, ...]) -> pd.Series:
    parsed = pd.Series(pd.NaT, index=text.index)
    for date_format in formats:
        parsed = parsed.fillna(pd.to_datetime(text, format=date_format, errors="coerce"))
    return parsed


def is_blank(values: pd.Series) -> pd.Series:
    return values.isna() | values.astype(str).str.strip().isin(["", "nan", "NaT", "None"])


def check_rows_have_dates(values: pd.Series) -> Check:
    """A row without a date cannot be placed in time, so it is left out and counted."""
    blank = int(is_blank(values).sum())
    return Check("rows_have_dates", WARN if blank else PASS,
                 f"{blank} of {len(values)} rows have no date and are left out."
                 if blank else "Every row has a date.", {"rows_without_date": blank})


def check_parser_reads_dates(values: pd.Series) -> Check:
    """Whether the prep pipeline's line-list parser can read the date column.

    A line list is parsed by the pipeline, not by this skill, so its dates
    must suit that parser. It stops on dates that carry a time zone, and when
    a column mixes layouts it keeps the rows in one layout and drops the rest.
    """
    text = values[~is_blank(values)].astype(str).str.strip()
    zoned = int(text.str.contains(ZONED_TIME).sum())
    layouts = text.str.replace(r"\d+", "9", regex=True).value_counts()
    if zoned:
        return Check(
            "parser_reads_dates", FAIL,
            f"{zoned} dates carry a time zone (for example a trailing 'Z'). The prep "
            "pipeline stops on these. Rewrite the date column as YYYY-MM-DD in a copy "
            "of the files and inspect the copy.", {"rows_with_time_zone": zoned})
    if len(layouts) > 1:
        return Check(
            "parser_reads_dates", FAIL,
            f"The date column mixes {len(layouts)} layouts. The prep pipeline would "
            "keep the rows in one layout and drop the others. Rewrite the date column "
            "as YYYY-MM-DD in a copy of the files and inspect the copy.",
            {"rows_per_layout": layouts.to_dict()})
    return Check("parser_reads_dates", PASS, "Dates are in one layout, without a time zone.")


def parse_dates(values: pd.Series) -> tuple[pd.Series, Check]:
    """Read dates without guessing silently between day-first and month-first.

    '03-04-2024' is 3 April in most of the world and 4 March in the US.
    Year-first dates are taken as they are. Otherwise both readings are tried:
    the one that reads every row is used, and when both do, the file is
    ambiguous and the check fails so that nobody forecasts on shifted dates.
    Blank rows are left as missing and judged by `check_rows_have_dates`.
    """
    parsed = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
    text = values[~is_blank(values)].astype(str).str.strip()
    if text.empty:
        return parsed, Check("dates_readable", FAIL, "The date column is empty.")
    if text.str.match(r"\d{4}-\d{2}-\d{2}").all():
        # A time or zone, if present, is dropped: the calendar date as written
        # is what counts.
        days = pd.to_datetime(text.str.slice(0, 10), format="%Y-%m-%d", errors="coerce")
        if days.notna().all():
            parsed.loc[text.index] = days
            return parsed, Check("dates_readable", PASS, "Dates are year-first (ISO 8601).")
    day_first = _parse_with(text, DAY_FIRST_FORMATS)
    month_first = _parse_with(text, MONTH_FIRST_FORMATS)
    day_ok, month_ok = bool(day_first.notna().all()), bool(month_first.notna().all())
    if day_ok and month_ok and not day_first.equals(month_first):
        return parsed, Check(
            "dates_readable", FAIL,
            "Every date can be read as day-first or as month-first, and the two "
            "readings differ. Convert the column to YYYY-MM-DD.",
            {"example": text.iloc[0]},
        )
    if day_ok or month_ok:
        parsed.loc[text.index] = day_first if day_ok else month_first
        return parsed, Check("dates_readable", PASS,
                             f"Dates read as {'day' if day_ok else 'month'}-first.")
    best = day_first if day_first.notna().sum() >= month_first.notna().sum() else month_first
    unreadable = text[best.isna()]
    return parsed, Check(
        "dates_readable", FAIL,
        f"{len(unreadable)} rows have a date that cannot be read.",
        {"examples": unreadable.head(3).tolist()},
    )


def check_frequency(units: pd.Series, dates: pd.Series, kind: str) -> tuple[dict, Check]:
    """Daily or weekly, judged by weekdays and the usual gap between records.

    A line list whose dates all fall on one weekday has had its dates rounded
    to weeks, so it is weekly data whatever its shape.
    """
    weekdays = dates.dt.day_name().value_counts()
    weekday, share = weekdays.index[0], float(weekdays.iloc[0] / weekdays.sum())
    detail = {"records_per_weekday": weekdays.to_dict()}
    frame = pd.DataFrame({"unit": units, "date": dates}).drop_duplicates()
    gaps = frame.sort_values(["unit", "date"]).groupby("unit").date.diff().dt.days.dropna()
    usual = int(gaps.mode().iloc[0]) if len(gaps) else 0
    weekly = usual == 7 or (share == 1 and frame.date.nunique() >= 8)
    if weekly and share == 1:
        info = {"frequency": "weekly", "week_date_weekday": weekday}
        rounded = kind == LINE_LIST
        return info, Check(
            "frequency", WARN if rounded else PASS,
            (f"Every date falls on a {weekday}, so case dates have been rounded to "
             "weeks. Confirm whether that day starts or ends the week. "
             if rounded else f"Weekly records, all dated on a {weekday}. ")
            + f"Run forecasts on a {weekday}; on another weekday the latest week "
            "is left out.", detail)
    if weekly:
        return {"frequency": "weekly", "week_date_weekday": weekday}, Check(
            "frequency", FAIL,
            f"Weekly data, but only {share:.0%} of dates fall on a {weekday}. Weeks "
            "dated on different weekdays would land in the wrong 7-day windows. "
            "Ask the supplier which day a week is dated on; do not shift dates on "
            "a guess.", detail)
    if usual <= 1:
        return {"frequency": "daily"}, Check("frequency", PASS, "Daily records.", detail)
    return {"frequency": f"every {usual} days"}, Check(
        "frequency", FAIL,
        f"Records are usually {usual} days apart. The pipeline builds 7-day totals, "
        "so it needs daily or weekly data.", detail)


def check_history(dates: pd.Series) -> Check:
    years = (dates.max() - dates.min()).days / 365.25
    weeks = round(years * 52)
    detail = {"weeks": weeks, "minimum_weeks": MIN_WEEKS_FOR_FORECAST,
              "calendar_years": int(dates.dt.year.nunique())}
    if weeks < MIN_WEEKS_FOR_FORECAST:
        return Check("history_length", FAIL,
                     f"About {weeks} weeks of history; forecasts need at least "
                     f"{MIN_WEEKS_FOR_FORECAST}.", detail)
    if years < RECOMMENDED_YEARS:
        return Check("history_length", WARN,
                     f"{years:.1f} years of history. {RECOMMENDED_YEARS} or more, with "
                     "an outbreak season, make thresholds and evaluation more reliable.",
                     detail)
    return Check("history_length", PASS, f"{years:.1f} years of history.", detail)


def check_year_levels(dates: pd.Series, counts: pd.Series) -> Check:
    """A year far above or below the others: an outbreak, or a fault in that year's file.

    Only years the data covers from start to end are compared, each against
    the median year. A year's level is its median weekly total, so one
    extreme week (judged by `check_outliers`) does not mark the whole year.
    The data cannot say which cause it is, so this warns.
    """
    full = [year for year in sorted(dates.dt.year.unique())
            if dates.min() <= pd.Timestamp(year, 1, 7) and dates.max() >= pd.Timestamp(year, 12, 24)]
    weekly = counts.groupby(dates.dt.to_period("W").dt.end_time.dt.normalize()).sum()
    levels = weekly.groupby(weekly.index.year).median().reindex(full)
    odd = {}
    typical = levels.median()  # of all full years, so one odd year does not mark the rest
    for year in full if len(full) >= 3 else []:
        if typical > 0 and not 1 / YEAR_FACTOR <= levels[year] / typical <= YEAR_FACTOR:
            odd[int(year)] = round(float(levels[year] / typical), 2)
    return Check(
        "years_are_comparable", WARN if odd else PASS,
        f"Years {sorted(odd)} differ from the other years by more than {YEAR_FACTOR:g} "
        "times. Ask whether that was an outbreak or a change in the data (a repeated "
        "file, a wider case definition, more testing)." if odd
        else "No full year stands far from the others.",
        {"median_weekly_cases_per_full_year": {int(y): float(v) for y, v in levels.items()},
         "ratio_to_other_years": odd})


def check_recency(dates: pd.Series, today: pd.Timestamp) -> list[Check]:
    future = int((dates > today).sum())
    weeks_old = int((today - dates.max()).days // 7)
    return [
        Check("no_future_dates", FAIL if future else PASS,
              f"{future} rows are dated after today." if future
              else "No dates in the future."),
        Check("data_is_recent", WARN if weeks_old > LONG_GAP_WEEKS else PASS,
              f"The data ends {weeks_old} weeks before today, so a forecast from it "
              "covers a period already past." if weeks_old > LONG_GAP_WEEKS
              else f"The data ends {weeks_old} weeks before today.",
              {"last_date": str(dates.max().date()), "weeks_old": weeks_old}),
    ]


# --- what the rows are -------------------------------------------------------

def check_run_date(run_date: pd.Timestamp, last_case_date: pd.Timestamp) -> Check:
    """The run date must not be later than the data.

    The forecast pipeline does not carry the series forward to the run date:
    the period between the last record and the run date enters the model as
    absent, and the forecasts come out too low without any error.
    """
    gap = (run_date - last_case_date).days
    return Check(
        "run_date_has_data", FAIL if gap > 0 else PASS,
        f"The run date is {gap} days after the last case record "
        f"({last_case_date.date()}). The pipeline would forecast from a gap and "
        f"come out too low. Use {last_case_date.date()} as the run date, or get "
        "data up to the date wanted." if gap > 0
        else "The case data reaches the run date.", {"days_after_last_record": max(gap, 0)})


def _is_grouping(values: pd.Series) -> bool:
    """A column that could split counts into groups: a few labels, not numbers or IDs."""
    present = values.dropna()
    numeric = pd.to_numeric(present, errors="coerce").notna().mean() > 0.9 if len(present) else True
    return not numeric and present.nunique() <= MAX_GROUP_LABELS


def detect_kind(table: pd.DataFrame, unit_col: str, date_col: str,
                count_col: str | None) -> tuple[str, list[Check]]:
    """Cases or counts? Getting this wrong multiplies or divides every number.

    A line list has one row per case. Aggregated data has a count per unit and
    date, sometimes split into several rows by age group, sex or facility.
    """
    counts = pd.to_numeric(table[count_col], errors="coerce") if count_col else None
    if counts is None or counts.notna().mean() < 0.95 or counts.max() <= 1:
        note = (f"Column {count_col!r} is always 1, so each row is one case."
                if counts is not None and counts.notna().mean() >= 0.95
                else "No count column, so each row is one case.")
        return LINE_LIST, [Check("kind_clear", PASS, note)]

    others = [c for c in table.columns if c not in (unit_col, date_col, count_col)]
    repeats = float(table.duplicated([unit_col, date_col]).mean())
    strata = [c for c in others if _is_grouping(table[c])
              and table.groupby([unit_col, date_col])[c].nunique().max() > 1]
    # A person-level column that does not split the counts into groups means
    # the rows may be people after all.
    personal = [c for c in others if _has_hint(c, PERSON_HINTS) and c not in strata]
    if personal:
        return LINE_LIST, [Check(
            "kind_clear", FAIL,
            f"The file has person-level columns {personal} and also {count_col!r} "
            "with values above 1. Ask whether each row is one case or a count.")]
    if repeats == 0:
        return AGGREGATED, [Check(
            "kind_clear", PASS, f"One count per unit and date in {count_col!r}.")]
    totals = sorted({str(v) for c in strata for v in table[c].dropna().unique()
                     if str(v).strip().lower() in TOTAL_LABELS})
    if totals:
        return AGGREGATED, [Check(
            "kind_clear", FAIL,
            f"Several rows per unit and date, including summary rows {totals}. "
            "Adding these to the rows they summarise would double count. Remove "
            "the summary rows or keep only them.", {"breakdown_columns": strata})]
    return AGGREGATED, [Check(
        "kind_clear", WARN,
        f"Several rows per unit and date, split by {strata}. Counts will be added "
        "across these rows. Confirm they are separate groups of the same cases and "
        "do not overlap.",
        {"breakdown_columns": strata, "share_of_repeated_rows": round(repeats, 3)})]


def check_exact_duplicates(table: pd.DataFrame) -> Check:
    """In a line list every row is a case, so a copied row is a case counted twice.

    Two cases in the same unit on the same day look identical unless some
    column tells cases apart, so the check only means something when one does.
    """
    identifying = any(table[c].nunique() > 0.5 * len(table) for c in table.columns)
    if not identifying:
        return Check("no_duplicate_rows", PASS,
                     "Duplicates cannot be checked: no column tells one case from "
                     "another, as expected in a de-identified line list.")
    repeated = int(table.duplicated().sum())
    return Check(
        "no_duplicate_rows", WARN if repeated else PASS,
        f"{repeated} rows are exact copies of another row; each would be counted as "
        "a separate case." if repeated else "No exact duplicate rows.",
    )


def check_mixed_categories(table: pd.DataFrame, used: set[str]) -> Check:
    """A line list that mixes results or diseases counts every row as a dengue case."""
    mixed = {}
    for column in table.columns:
        if column in used or not _has_hint(column, CATEGORY_HINTS):
            continue
        values = table[column].astype(str).str.strip().value_counts()
        if 1 < len(values) <= 20:
            mixed[column] = values.to_dict()
    return Check(
        "one_kind_of_case", WARN if mixed else PASS,
        f"Columns {sorted(mixed)} hold more than one value. Every row is counted "
        "as a case unless a filter keeps only the dengue-positive rows." if mixed
        else "No result or disease column holds mixed values.",
        {"values": mixed})


def check_cumulative(units: pd.Series, dates: pd.Series, counts: pd.Series) -> Check:
    """Running totals within a year, used as new cases, inflate everything.

    New-case counts go up and down from week to week. A series that almost
    never falls within a calendar year is a running total.
    """
    frame = pd.DataFrame({"unit": units, "date": dates, "cases": counts}).sort_values(
        ["unit", "date"])
    frame["year"] = frame.date.dt.year
    changes = frame.groupby(["unit", "year"]).cases.diff().dropna()
    if len(changes) < 20:
        return Check("counts_are_new_cases", PASS, "Too few records to assess.")
    rising = float((changes >= 0).mean())
    return Check(
        "counts_are_new_cases", FAIL if rising >= CUMULATIVE_SHARE else PASS,
        f"Counts rise or hold in {rising:.0%} of week-to-week steps within a year, "
        "which is the pattern of running totals. The pipeline needs new cases per "
        "period." if rising >= CUMULATIVE_SHARE
        else f"Counts rise in {rising:.0%} of steps and fall in the rest, as new-case "
        "counts do.", {"share_of_non_decreasing_steps": round(rising, 3)})


def check_outliers(units: pd.Series, dates: pd.Series, counts: pd.Series) -> Check:
    """A weekly total far above a unit's own usual level is often a typing error.

    Judged on weekly totals, because single days in sparse daily data swing
    widely for ordinary reasons.
    """
    frame = pd.DataFrame({"unit": units, "week": dates.dt.to_period("W").dt.end_time.dt.normalize(),
                          "cases": counts})
    weekly = frame.groupby(["unit", "week"], as_index=False).cases.sum()
    usual = weekly.groupby("unit").cases.transform(lambda s: s.quantile(0.95)).clip(lower=1)
    extreme = weekly[weekly.cases > OUTLIER_FACTOR * usual]
    return Check(
        "no_extreme_counts", WARN if len(extreme) else PASS,
        f"{len(extreme)} unit-weeks are more than {OUTLIER_FACTOR} times their unit's "
        "95th percentile week; check them against the source." if len(extreme)
        else "No weekly total far outside a unit's usual range.",
        {"unit_weeks": extreme.assign(week=extreme.week.dt.strftime("%Y-%m-%d"))
         .head(10).to_dict("records")},
    )


# --- where the cases are -------------------------------------------------------

def check_location_basis(table: pd.DataFrame, unit_col: str) -> list[Check]:
    """Patient residence, or the facility that reported the case?

    Cases placed at the reporting facility pile up in the districts that have
    hospitals. The forecast would then predict where people are treated.
    """
    facility_cols = [c for c in table.columns if _has_hint(c, FACILITY_HINTS)]
    checks = [Check(
        "unit_is_patient_location", FAIL if unit_col in facility_cols else PASS,
        f"The unit column {unit_col!r} reads as the reporting facility's location, "
        "not where the patient lives. Confirm with the supplier before using it."
        if unit_col in facility_cols
        else f"The unit column {unit_col!r} does not read as a facility location.")]
    rivals = [c for c in facility_cols if c != unit_col
              and table[c].nunique() <= max(table[unit_col].nunique() * 3, 20)]
    if rivals:
        differ = {c: round(float((table[c].astype(str).str.strip()
                                  != table[unit_col].astype(str).str.strip()).mean()), 3)
                  for c in rivals}
        checks.append(Check(
            "one_location_column", WARN,
            f"The file also has facility location columns {rivals}. Make sure "
            f"{unit_col!r} is the patient's residence.",
            {"share_of_rows_where_they_differ": differ}))
    return checks


def check_coordinates(table: pd.DataFrame) -> Check:
    """Many cases sharing a few exact points suggests facility coordinates."""
    lat = next((c for c in table.columns if re.search(r"\blat", c.lower())), None)
    lon = next((c for c in table.columns if re.search(r"\blon|\blng", c.lower())), None)
    if not lat or not lon:
        return Check("coordinates_are_patient_locations", PASS, "No coordinate columns.")
    located = table[~is_blank(table[lat]) & ~is_blank(table[lon])]
    if located.empty:
        return Check("coordinates_are_patient_locations", PASS, "No coordinates given.")
    points = (located[lat].astype(str) + "," + located[lon].astype(str)).value_counts()
    top_share = float(points.head(5).sum() / points.sum())
    clustered = top_share > FEW_POINTS_SHARE and len(located) >= 50
    return Check(
        "coordinates_are_patient_locations", WARN if clustered else PASS,
        f"{top_share:.0%} of rows sit on just 5 exact points ({len(points)} distinct "
        "points in all). That is the pattern of facility coordinates, not homes."
        if clustered else f"{len(points)} distinct points; no few points dominate.",
        {"distinct_points": int(len(points)), "share_on_top_5_points": round(top_share, 3)})


def check_concentration(units: pd.Series, weights: pd.Series) -> Check:
    """One unit holding most cases may be a referral centre's district."""
    totals = weights.groupby(units).sum().sort_values(ascending=False)
    share = float(totals.iloc[0] / totals.sum()) if totals.sum() else 0.0
    concentrated = share > CONCENTRATION_SHARE and len(totals) >= 5
    return Check(
        "cases_spread_across_units", WARN if concentrated else PASS,
        f"Unit {totals.index[0]} holds {share:.0%} of all cases across {len(totals)} "
        "units. Check that cases are placed where patients live and not where they "
        "were tested." if concentrated
        else f"The largest unit holds {share:.0%} of cases.",
        {"top_unit": str(totals.index[0]), "share": round(share, 3)})


def check_unit_coverage(units: pd.Series, dates: pd.Series, kind: str) -> list[Check]:
    """Periods with no data: for everyone, or for one unit?

    The pipeline treats a missing record as zero cases. A stretch with no
    record for any unit means reports are missing, and those weeks would be
    read as weeks without dengue. A unit that skips many of the dates on which
    others report may have stopped reporting.
    """
    frame = pd.DataFrame({"unit": units, "date": dates}).drop_duplicates()
    days = pd.Series(sorted(frame.date.unique()))
    steps = days.diff().dt.days
    holes = [{"from": str(days[i - 1].date()), "to": str(days[i].date()), "days": int(g)}
             for i, g in steps.items() if g > DATASET_GAP_DAYS]
    missing = Check(
        "no_missing_periods", WARN if holes else PASS,
        f"{len(holes)} stretches of more than {DATASET_GAP_DAYS} days have no record "
        f"for any unit, the longest {int(steps.max())} days. These would be read as "
        "periods with zero cases." if holes else "No long stretch without any record.",
        {"stretches": holes})
    if kind == LINE_LIST:
        # In a line list a unit with no rows simply had no cases, so only a
        # hole across the whole file says anything about reporting.
        return [missing]
    # A unit's silence is counted in reporting dates it skipped, so a hole in
    # the whole dataset is not blamed on every unit in turn.
    position = {day: i for i, day in enumerate(days)}
    usual_step = max(int(steps.median()), 1) if len(days) > 1 else 1
    limit = max(LONG_GAP_WEEKS * 7 // usual_step, 2)
    frame = frame.sort_values(["unit", "date"])
    frame["position"] = frame.date.map(position)
    skipped = frame.groupby("unit").position.diff().groupby(frame.unit).max()
    silent = skipped[skipped > limit]
    last = frame.groupby("unit").position.max()
    stale = last[(len(days) - 1 - last) > limit]
    return [
        missing,
        Check("units_report_to_the_end", WARN if len(stale) else PASS,
              f"{len(stale)} units have no record on the last {limit} dates on which "
              "others reported. Confirm they were still reporting." if len(stale)
              else "Every unit has records up to the end of the file.",
              {"last_record": {u: str(days[int(i)].date()) for u, i in stale.items()}}),
        Check("no_long_silences", WARN if len(silent) else PASS,
              f"{len(silent)} units skipped more than {limit} dates in a row on which "
              "other units reported. Confirm whether these were zero cases or periods "
              "without reporting." if len(silent)
              else "No unit is silent for long while others report.",
              {"reporting_dates_skipped": {u: int(d) for u, d in silent.items()}}),
    ]


def _loose(code: str) -> str:
    """A code with case, spaces, punctuation and zero padding removed."""
    plain = re.sub(r"[^a-z0-9]", "", code.lower())
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", plain)


def check_unit_match(case_units: set[str], boundary_units: set[str] | None,
                     region_type: str) -> list[Check]:
    if boundary_units is None:
        return [Check("case_units_have_boundaries", WARN,
                      "Not checked: no boundaries found for this level. Run the "
                      "boundaries stage first and pass the same --region-type.")]
    missing = sorted(case_units - boundary_units)
    unused = sorted(boundary_units - case_units)
    prefix = f"{region_type}_"
    by_loose = {_loose(u.removeprefix(prefix)): u for u in unused}
    near = {u: by_loose[_loose(u.removeprefix(prefix))] for u in missing
            if _loose(u.removeprefix(prefix)) in by_loose}
    return [
        Check("case_units_have_boundaries", FAIL if missing else PASS,
              f"{len(missing)} units in the case data have no boundary. Ask whether "
              "they are miscoded or missing from the boundary file; do not rename "
              "codes on a guess."
              + (f" {len(near)} differ from a boundary code only by zeros, spaces "
                 "or letter case." if near else "") if missing
              else "Every unit in the case data has a boundary.",
              {"units": missing, "near_matches": near}),
        Check("boundaries_have_cases", WARN if unused else PASS,
              f"{len(unused)} boundary units never appear in the case data and will "
              "be treated as zero cases throughout." if unused
              else "Every boundary unit appears in the case data.",
              {"units": unused}),
    ]
