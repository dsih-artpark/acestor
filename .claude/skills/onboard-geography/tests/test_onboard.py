"""Known-truth tests for the onboarding checks.

Every input is built by formula, so each expected value below is exact. Most
tests plant one fault and assert that its own check, and no other, objects.
Run from the repository root:

    uv run pytest .claude/skills/onboard-geography/tests -q --no-cov
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import boundaries  # noqa: E402
import cases  # noqa: E402
import configs  # noqa: E402
import contracts  # noqa: E402
import onboard  # noqa: E402
import qc  # noqa: E402
import verify  # noqa: E402
from report import FAIL, PASS, WARN, failed  # noqa: E402

TODAY = pd.Timestamp("2026-02-01")
UNITS = ("A1", "A2", "A3")
WEEKS = pd.date_range("2023-01-01", periods=160, freq="7D")  # Sundays, just over 3 years
KNOWN = {"district_A1", "district_A2", "district_A3"}


def status(checks, check_id):
    return next(c.status for c in checks if c.id == check_id)


def detail(checks, check_id):
    return next(c.detail for c in checks if c.id == check_id)


def objections(checks):
    return {c.id: c.status for c in checks if c.status != PASS}


def weekly_counts() -> pd.DataFrame:
    rows = [(week, unit, (3 * i + k) % 7) for i, unit in enumerate(UNITS)
            for k, week in enumerate(WEEKS)]
    return pd.DataFrame(rows, columns=["date", "unit", "cases"])


def line_list() -> pd.DataFrame:
    """One row per case, on real days of the week, from the same truth."""
    table = weekly_counts()
    rows = table.loc[table.index.repeat(table.cases), ["date", "unit"]].reset_index(drop=True)
    rows["date"] = rows.date - pd.to_timedelta(rows.index % 7, unit="D")
    return rows


def save(table: pd.DataFrame, path: Path, date_format: str = "%Y-%m-%d") -> Path:
    table.assign(date=pd.to_datetime(table.date).dt.strftime(date_format)).to_csv(
        path, index=False)
    return path


def run_inspect(path: Path, known=KNOWN, filters=(), **columns):
    return cases.inspect(path, columns.get("date", "date"), columns.get("unit", "unit"),
                         columns.get("count", "cases"), "district", known, TODAY,
                         list(filters))


@pytest.fixture
def layout(tmp_path) -> Path:
    """Boundaries for A1-A3 written through the real boundary stage."""
    features = [{
        "type": "Feature",
        "properties": {"CODE": unit, "NAME": f"Unit {unit}"},
        "geometry": {"type": "Polygon", "coordinates": [[
            [77 + i, 12], [78 + i, 12], [78 + i, 13], [77 + i, 13], [77 + i, 12]]]},
    } for i, unit in enumerate(UNITS)]
    source = tmp_path / "units.geojson"
    source.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    _, checks = boundaries.prepare(source, "CODE", "NAME", "district", "state_1",
                                   "Testland", tmp_path / "geojsons")
    assert not failed(checks)
    return tmp_path


# --- dates ---------------------------------------------------------------------

def test_iso_dates_are_read_as_written():
    parsed, check = qc.parse_dates(pd.Series(["2024-04-03", "2024-12-25"]))
    assert check.status == PASS
    assert list(parsed.dt.strftime("%d %b")) == ["03 Apr", "25 Dec"]


def test_day_first_is_chosen_when_only_it_reads_every_row():
    parsed, check = qc.parse_dates(pd.Series(["03-04-2024", "25-12-2024"]))
    assert check.status == PASS and "day-first" in check.message
    assert list(parsed.dt.strftime("%d %b")) == ["03 Apr", "25 Dec"]


def test_ambiguous_dates_fail_instead_of_guessing():
    # Every day is 12 or less, so 03-04 could be 3 April or 4 March.
    _, check = qc.parse_dates(pd.Series(["03-04-2024", "05-06-2024"]))
    assert check.status == FAIL


def test_iso_timestamps_are_read_as_their_calendar_date():
    parsed, check = qc.parse_dates(pd.Series(["2012-01-02T00:00:00Z", "2024-12-25T00:00:00Z"]))
    assert check.status == PASS
    assert list(parsed.dt.strftime("%Y-%m-%d")) == ["2012-01-02", "2024-12-25"]


def test_iso_timestamps_without_a_zone_and_blank_dates(tmp_path):
    values = pd.Series(["2024-07-06T00:00:00", None, "2024-07-08T00:00:00"])
    parsed, check = qc.parse_dates(values)
    assert check.status == PASS and parsed.notna().tolist() == [True, False, True]
    assert qc.check_rows_have_dates(values).detail == {"rows_without_date": 1}


@pytest.mark.parametrize("dates, expected, key", [
    (["2025-01-06", "2025-01-07"], PASS, None),
    (["6/1/2025", "17/01/2025"], PASS, None),
    (["2025-01-06T00:00:00", "2025-01-07T00:00:00"], PASS, None),
    (["2025-01-06T00:00:00Z", "2025-01-07T00:00:00Z"], FAIL, "rows_with_time_zone"),
    (["2025-01-06T00:00:00+05:30", "2025-01-07"], FAIL, "rows_with_time_zone"),
    (["2025-01-06", "2025-01-07T00:00:00"], FAIL, "rows_per_layout"),
    (["06-01-2025", "17/01/2025"], FAIL, "rows_per_layout"),
])
def test_dates_the_pipeline_parser_mishandles_fail(dates, expected, key):
    check = qc.check_parser_reads_dates(pd.Series(dates))
    assert check.status == expected
    assert key is None or key in check.detail


def test_zoned_line_list_fails_but_zoned_counts_do_not(tmp_path):
    zoned = "%Y-%m-%dT00:00:00Z"
    _, checks, _ = run_inspect(save(line_list(), tmp_path / "l.csv", zoned), count=None)
    assert status(checks, "parser_reads_dates") == FAIL
    _, checks, _ = run_inspect(save(weekly_counts(), tmp_path / "w.csv", zoned))
    assert "parser_reads_dates" not in {c.id for c in checks}


def test_rows_without_a_date_are_left_out_and_counted(tmp_path, layout):
    rows = line_list()
    path = tmp_path / "undated.csv"
    rows.assign(date=rows.date.dt.strftime("%Y-%m-%d").where(rows.index % 50 != 0)).to_csv(
        path, index=False)
    undated = int((rows.index % 50 == 0).sum())
    info, checks, _ = run_inspect(path, count=None)
    assert objections(checks) == {"rows_have_dates": WARN}
    assert info["total_cases"] == len(rows) - undated

    table = weekly_counts().astype({"date": object})
    table.loc[[2, 5], "date"] = None
    path = tmp_path / "undated_counts.csv"
    table.assign(date=pd.to_datetime(table.date).dt.strftime("%Y-%m-%d")).to_csv(path, index=False)
    info, checks = cases.standardise(path, "date", "unit", "cases", "district", KNOWN,
                                     layout / "prepared_data", sum_breakdowns=False)
    lost = int(weekly_counts().cases.iloc[[2, 5]].sum())
    assert info["undated_cases"] == lost and status(checks, "cases_reconcile") == PASS
    assert info["source_cases"] == info["written_cases"] + lost


def test_unreadable_dates_fail_with_examples():
    _, check = qc.parse_dates(pd.Series(["03-04-2024", "not a date"]))
    assert check.status == FAIL and check.detail["examples"] == ["not a date"]


# --- inspect: clean files ---------------------------------------------------------

def test_clean_weekly_file_raises_no_objection(tmp_path):
    info, checks, expected = run_inspect(save(weekly_counts(), tmp_path / "weekly.csv"))
    assert info["kind"] == qc.AGGREGATED
    assert info["frequency"] == "weekly" and info["week_date_weekday"] == "Sunday"
    assert info["total_cases"] == weekly_counts().cases.sum() == expected.cases.sum()
    assert objections(checks) == {}


def test_clean_line_list_raises_no_objection(tmp_path):
    info, checks, expected = run_inspect(save(line_list(), tmp_path / "list.csv"),
                                         count=None)
    assert info["kind"] == qc.LINE_LIST and info["frequency"] == "daily"
    assert info["total_cases"] == weekly_counts().cases.sum() == expected.cases.sum()
    assert objections(checks) == {}


# --- inspect: one planted fault each ------------------------------------------------

@pytest.mark.parametrize("edit, check_id, expected", [
    (lambda t: t.assign(cases=t.cases.where(t.index != 5, -2)), "aggregated_cases_cases", FAIL),
    (lambda t: t[t.date >= "2025-05-01"], "history_length", FAIL),
    (lambda t: t[t.date >= "2024-03-01"], "history_length", WARN),
    (lambda t: t.assign(date=t.date - pd.Timedelta(weeks=26)), "data_is_recent", WARN),
    (lambda t: t[~((t.unit == "A2") & (t.date > "2025-06-01"))],
     "units_report_to_the_end", WARN),
    (lambda t: t[~((t.unit == "A3") & t.date.between("2024-01-01", "2024-06-01"))],
     "no_long_silences", WARN),
    (lambda t: t.assign(cases=t.cases.where(t.index != 7, 900)), "no_extreme_counts", WARN),
    (lambda t: t.assign(date=t.date.where(t.index % 9 != 0, t.date + pd.Timedelta(days=1))),
     "frequency", FAIL),
    (lambda t: t.assign(cases=t.groupby(["unit", t.date.dt.year]).cases.cumsum()),
     "counts_are_new_cases", FAIL),
])
def test_each_fault_trips_only_its_own_check(tmp_path, edit, check_id, expected):
    _, checks, _ = run_inspect(save(edit(weekly_counts()), tmp_path / "faulty.csv"))
    assert objections(checks) == {check_id: expected}


def test_a_date_after_today_fails(tmp_path):
    table = weekly_counts()
    table.loc[0, "date"] = pd.Timestamp("2030-01-06")
    _, checks, _ = run_inspect(save(table, tmp_path / "future.csv"))
    assert status(checks, "no_future_dates") == FAIL


def test_mixed_weekdays_report_the_split(tmp_path):
    table = weekly_counts()
    table["date"] = table.date.where(table.index % 9 != 0, table.date + pd.Timedelta(days=1))
    _, checks, _ = run_inspect(save(table, tmp_path / "mixed.csv"))
    split = detail(checks, "frequency")["records_per_weekday"]
    assert split["Monday"] == (table.index % 9 == 0).sum() and set(split) == {"Sunday", "Monday"}


# --- cases or counts ---------------------------------------------------------------

def test_counts_split_by_age_group_are_recognised_as_aggregated(tmp_path, layout):
    table = weekly_counts()
    split = pd.concat([table.assign(age_group="under 15", cases=table.cases // 2),
                       table.assign(age_group="15 and over", cases=table.cases - table.cases // 2)])
    path = save(split, tmp_path / "by_age.csv")
    info, checks, _ = run_inspect(path)
    assert info["kind"] == qc.AGGREGATED and status(checks, "kind_clear") == WARN
    assert detail(checks, "kind_clear")["breakdown_columns"] == ["age_group"]
    assert info["total_cases"] == table.cases.sum()

    args = (path, "date", "unit", "cases", "district", KNOWN, layout / "prepared_data")
    _, refused = cases.standardise(*args, sum_breakdowns=False)
    assert status(refused, "breakdowns_summed") == FAIL
    assert not (layout / "prepared_data" / "district" / "cases_daily.csv").exists()
    info, checks = cases.standardise(*args, sum_breakdowns=True)
    assert not failed(checks) and info["written_cases"] == table.cases.sum()


def test_summary_rows_among_breakdowns_fail(tmp_path):
    table = weekly_counts()
    with_total = pd.concat([table.assign(sex="male", cases=table.cases // 2),
                            table.assign(sex="female", cases=table.cases - table.cases // 2),
                            table.assign(sex="Total")])
    _, checks, _ = run_inspect(save(with_total, tmp_path / "with_total.csv"))
    assert status(checks, "kind_clear") == FAIL


def test_count_like_column_in_a_line_list_fails_until_clarified(tmp_path):
    rows = line_list().assign(patient_age=31, number_of_tests=2)
    _, checks, _ = run_inspect(save(rows, tmp_path / "list.csv"), count="number_of_tests")
    assert status(checks, "kind_clear") == FAIL


def test_count_column_of_ones_is_a_line_list(tmp_path):
    rows = line_list().assign(cases=1)
    info, checks, _ = run_inspect(save(rows, tmp_path / "ones.csv"))
    assert info["kind"] == qc.LINE_LIST and status(checks, "kind_clear") == PASS
    assert info["total_cases"] == len(rows)


def test_line_list_with_dates_rounded_to_weeks_is_flagged(tmp_path):
    table = weekly_counts()
    rows = table.loc[table.index.repeat(table.cases), ["date", "unit"]]
    info, checks, _ = run_inspect(save(rows, tmp_path / "rounded.csv"), count=None)
    assert info["frequency"] == "weekly" and info["week_date_weekday"] == "Sunday"
    assert objections(checks) == {"frequency": WARN}


def test_mixed_test_results_warn_and_a_filter_resolves_them(tmp_path):
    rows = line_list()
    rows["Test Result"] = ["Negative" if i % 4 == 0 else "Positive" for i in range(len(rows))]
    path = save(rows, tmp_path / "results.csv")
    info, checks, _ = run_inspect(path, count=None)
    assert status(checks, "one_kind_of_case") == WARN and info["total_cases"] == len(rows)
    assert detail(checks, "one_kind_of_case")["values"]["Test Result"]["Negative"] == (
        rows["Test Result"] == "Negative").sum()
    info, checks, _ = run_inspect(path, count=None,
                                  filters=[("Test Result", ["Positive"])])
    assert info["total_cases"] == (rows["Test Result"] == "Positive").sum()
    assert status(checks, "one_kind_of_case") == PASS


def test_duplicate_rows_flagged_only_when_cases_are_identifiable():
    plain = pd.DataFrame({"date": ["2024-01-07"] * 4, "unit": ["A1"] * 4})
    assert "cannot be checked" in qc.check_exact_duplicates(plain).message
    with_ids = plain.assign(case_id=["c1", "c2", "c3", "c3"])
    assert qc.check_exact_duplicates(with_ids).status == WARN
    assert qc.check_exact_duplicates(with_ids.iloc[:3]).status == PASS


def test_blank_counts_fail_until_the_user_says_they_are_zero(tmp_path, layout):
    table = weekly_counts().astype({"cases": object})
    table.loc[[4, 9], "cases"] = None
    path = save(table, tmp_path / "blank.csv")
    _, checks, expected = run_inspect(path)
    assert objections(checks) == {"blank_counts": FAIL}  # the other checks still ran
    assert "history_length" in {c.id for c in checks} and expected is None
    info, checks, _ = cases.inspect(path, "date", "unit", "cases", "district", KNOWN, TODAY,
                                    [], blank_as_zero=True)
    assert objections(checks) == {"blank_counts": WARN}
    assert info["total_cases"] == pd.to_numeric(table.cases).sum()
    args = (path, "date", "unit", "cases", "district", KNOWN, layout / "prepared_data", False)
    assert failed(cases.standardise(*args)[1])
    assert not failed(cases.standardise(*args, blank_as_zero=True)[1])


def test_a_count_that_is_not_a_number_is_caught_before_adding_up(tmp_path):
    table = weekly_counts().astype({"cases": object})
    table.loc[11, "cases"] = "#REF!"
    _, checks, _ = run_inspect(save(table, tmp_path / "ref.csv"))
    assert status(checks, "aggregated_cases_cases") == FAIL


def test_breakdown_columns_exclude_numbers_and_identifiers(tmp_path):
    table = weekly_counts()
    split = pd.concat([table.assign(area_type="urban", cases=table.cases // 2),
                       table.assign(area_type="rural", cases=table.cases - table.cases // 2)],
                      ignore_index=True)
    split["record_id"] = [f"r{i}" for i in range(len(split))]
    split["tested"] = split.cases * 3
    _, checks, _ = run_inspect(save(split, tmp_path / "split.csv"))
    assert detail(checks, "kind_clear")["breakdown_columns"] == ["area_type"]


def test_daily_data_with_an_ordinary_outbreak_day_is_not_flagged(tmp_path):
    days = pd.date_range("2023-01-01", "2026-01-25")
    rows = [(day, unit, 1 if (k + i) % 2 == 0 else 0)
            for i, unit in enumerate(UNITS) for k, day in enumerate(days)]
    table = pd.DataFrame(rows, columns=["date", "unit", "cases"])
    table.loc[(table.unit == "A1") & (table.date == "2024-07-10"), "cases"] = 12
    _, checks, _ = run_inspect(save(table, tmp_path / "daily.csv"))
    assert status(checks, "no_extreme_counts") == PASS
    table.loc[(table.unit == "A1") & (table.date == "2024-07-10"), "cases"] = 400
    _, checks, _ = run_inspect(save(table, tmp_path / "daily.csv"))
    assert status(checks, "no_extreme_counts") == WARN


def test_a_gap_shared_by_every_unit_is_reported_once(tmp_path):
    table = weekly_counts()
    table = table[~table.date.between("2024-06-01", "2024-09-15")]
    _, checks, _ = run_inspect(save(table, tmp_path / "hole.csv"))
    assert objections(checks) == {"no_missing_periods": WARN}
    stretches = detail(checks, "no_missing_periods")["stretches"]
    assert len(stretches) == 1 and stretches[0]["from"] == "2024-05-26"


def test_a_folder_of_yearly_files_reads_as_one_table(tmp_path):
    table = weekly_counts()
    folder = tmp_path / "yearly"
    folder.mkdir()
    for year, part in table.groupby(table.date.dt.year):
        save(part, folder / f"{year}.csv")
    info, checks, _ = run_inspect(folder)
    assert info["files"] == table.date.dt.year.nunique()
    assert info["total_cases"] == table.cases.sum() and objections(checks) == {}


def test_a_file_with_a_different_layout_is_named(tmp_path):
    table = weekly_counts()
    folder = tmp_path / "yearly"
    folder.mkdir()
    for year, part in table.groupby(table.date.dt.year):
        part = part.rename(columns={"unit": "district"}) if year == 2025 else part
        save(part, folder / f"{year}.csv")
    _, checks, _ = run_inspect(folder)
    assert status(checks, "files_have_columns") == FAIL
    assert detail(checks, "files_have_columns")["files"] == {"2025.csv": ["unit"]}


# --- where the cases are -------------------------------------------------------------

def test_rows_without_a_unit_are_left_out_and_counted(tmp_path):
    rows = line_list().astype({"unit": object})
    rows.loc[rows.index % 40 == 0, "unit"] = None
    info, checks, _ = run_inspect(save(rows, tmp_path / "no_unit.csv"), count=None)
    assert objections(checks) == {"rows_have_units": WARN}
    assert info["total_cases"] == int((rows.index % 40 != 0).sum()) and info["units"] == 3


def test_dotted_column_names_are_judged_by_their_last_part(tmp_path):
    rows = line_list().rename(columns={"unit": "location.admin2.ID"})
    rows["event.test.sampleType"] = ["Serum" if i % 2 else "Blood" for i in range(len(rows))]
    rows["event.test.test1.result"] = ["Positive" if i % 3 else "Negative" for i in range(len(rows))]
    _, checks, _ = run_inspect(save(rows, tmp_path / "dotted.csv"),
                               unit="location.admin2.ID", count=None)
    assert sorted(detail(checks, "one_kind_of_case")["values"]) == ["event.test.test1.result"]


def test_blank_coordinates_are_not_mistaken_for_one_point():
    blank = pd.DataFrame({"latitude": [None] * 60, "longitude": [None] * 60})
    assert qc.check_coordinates(blank).status == PASS


def test_unit_column_named_for_the_facility_fails(tmp_path):
    table = weekly_counts().rename(columns={"unit": "Reporting Facility District"})
    _, checks, _ = run_inspect(save(table, tmp_path / "facility.csv"),
                               unit="Reporting Facility District")
    assert status(checks, "unit_is_patient_location") == FAIL


def test_a_second_facility_location_column_warns_with_the_disagreement(tmp_path):
    rows = line_list().rename(columns={"unit": "Patient District"})
    rows["Hospital District"] = ["A1" if i % 4 else unit
                                 for i, unit in enumerate(rows["Patient District"])]
    _, checks, _ = run_inspect(save(rows, tmp_path / "two.csv"),
                               unit="Patient District", count=None)
    assert status(checks, "unit_is_patient_location") == PASS
    assert status(checks, "one_location_column") == WARN
    expected = (rows["Hospital District"] != rows["Patient District"]).mean()
    differ = detail(checks, "one_location_column")["share_of_rows_where_they_differ"]
    assert differ["Hospital District"] == round(float(expected), 3)


def test_coordinates_on_a_few_points_warn():
    hospitals = pd.DataFrame({"Latitude": [12.9, 13.0, 13.1] * 40,
                              "Longitude": [77.5, 77.6, 77.7] * 40})
    assert qc.check_coordinates(hospitals).status == WARN
    homes = pd.DataFrame({"Latitude": [12.9 + i / 1000 for i in range(120)],
                          "Longitude": [77.5 + i / 1000 for i in range(120)]})
    assert qc.check_coordinates(homes).status == PASS
    assert qc.check_coordinates(pd.DataFrame({"unit": ["A1"]})).status == PASS


def test_cases_concentrated_in_one_unit_warn():
    units = pd.Series(["A1"] * 5 + ["A2", "A3", "A4", "A5", "A6"])
    heavy = qc.check_concentration(units, pd.Series([20] * 5 + [5, 5, 5, 5, 5]))
    assert heavy.status == WARN and heavy.detail == {"top_unit": "A1", "share": 0.8}
    even = qc.check_concentration(units, pd.Series([1] * 5 + [5, 5, 5, 5, 5]))
    assert even.status == PASS


def test_units_without_boundary_fail_and_near_matches_are_named(tmp_path):
    table = weekly_counts()
    table["unit"] = table.unit.replace({"A1": "a01"})
    _, checks, _ = run_inspect(save(table, tmp_path / "codes.csv"))
    assert status(checks, "case_units_have_boundaries") == FAIL
    assert detail(checks, "case_units_have_boundaries")["near_matches"] == {
        "district_a01": "district_A1"}


def test_boundary_match_that_was_not_run_is_a_warning_not_silence(tmp_path):
    _, checks, _ = run_inspect(save(weekly_counts(), tmp_path / "weekly.csv"), known=None)
    assert objections(checks) == {"case_units_have_boundaries": WARN}


# --- standardise -----------------------------------------------------------------------

def test_standardise_preserves_every_case(tmp_path, layout):
    truth = weekly_counts()
    path = save(truth, tmp_path / "weekly.csv", date_format="%d-%m-%Y")
    info, checks = cases.standardise(path, "date", "unit", "cases", "district", KNOWN,
                                     layout / "prepared_data", sum_breakdowns=False)
    assert not failed(checks)
    written = pd.read_csv(info["written"], parse_dates=["date"])
    assert written.case_count.sum() == truth.cases.sum() == info["written_cases"]
    expected = truth.assign(region_id="district_" + truth.unit)  # reported zeros included
    assert (truth.cases == 0).any()
    merged = expected.merge(written, on=["region_id", "date"], how="outer", indicator=True)
    assert (merged._merge == "both").all() and (merged.cases == merged.case_count).all()
    assert list(written.columns) == ["region_id", "date", "case_count"]


def test_standardise_sets_aside_unknown_units_and_reconciles(tmp_path, layout):
    table = pd.concat([weekly_counts(), pd.DataFrame(
        {"date": [WEEKS[0], WEEKS[1]], "unit": ["ZZ", "ZZ"], "cases": [4, 6]})])
    info, checks = cases.standardise(save(table, tmp_path / "extra.csv"), "date", "unit",
                                     "cases", "district", KNOWN,
                                     layout / "prepared_data", sum_breakdowns=False)
    assert info["set_aside_cases"] == 10
    assert info["source_cases"] == info["written_cases"] + 10
    assert status(checks, "cases_reconcile") == PASS
    assert status(checks, "units_set_aside") == WARN


def test_standardise_writes_nothing_when_a_check_fails(tmp_path, layout):
    table = weekly_counts()
    table.loc[3, "cases"] = -1
    _, checks = cases.standardise(save(table, tmp_path / "bad.csv"), "date", "unit",
                                  "cases", "district", KNOWN, layout / "prepared_data",
                                  sum_breakdowns=False)
    assert failed(checks)
    assert not (layout / "prepared_data" / "district" / "cases_daily.csv").exists()


# --- boundaries --------------------------------------------------------------------------

def test_layout_matches_what_acestor_reads(layout):
    import geopandas as gpd

    assert boundaries.unit_ids(layout / "geojsons", "district") == KNOWN
    feature = gpd.read_file(layout / "geojsons" / "districts" / "district_A2.geojson")
    assert feature.region_id.iloc[0] == "district_A2"
    assert feature.name.iloc[0] == "Unit A2" and feature.parent.iloc[0] == "state_1"
    assert feature.crs.to_string() == "EPSG:4326"


def test_a_folder_of_boundary_files_is_laid_out_like_a_single_file(layout, tmp_path):
    _, checks = boundaries.prepare(layout / "geojsons" / "districts", "region_id", "name",
                                   "district", "", "", tmp_path / "again")
    assert not failed(checks)
    assert boundaries.unit_ids(tmp_path / "again", "district") == KNOWN


def test_duplicate_boundary_codes_fail_and_write_nothing(tmp_path):
    square = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    features = [{"type": "Feature", "properties": {"CODE": "A1", "NAME": name},
                 "geometry": {"type": "Polygon", "coordinates": square}}
                for name in ("one", "two")]
    source = tmp_path / "dupe.geojson"
    source.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    _, checks = boundaries.prepare(source, "CODE", "NAME", "district", "", "",
                                   tmp_path / "geojsons")
    assert status(checks, "boundary_codes_unique") == FAIL
    assert not (tmp_path / "geojsons").exists()


# --- contracts and verification -------------------------------------------------------------

def test_prepared_cases_contract_rejects_negatives_and_repeats():
    good = pd.DataFrame({"region_id": ["district_A1"], "date": ["2024-01-07"],
                         "case_count": [3]})
    assert not failed(contracts.validate(good, contracts.PREPARED_CASES))
    assert not failed(contracts.validate(good.assign(case_count=0), contracts.PREPARED_CASES))
    assert failed(contracts.validate(good.assign(case_count=-1), contracts.PREPARED_CASES))
    assert failed(contracts.validate(pd.concat([good, good]), contracts.PREPARED_CASES))
    assert failed(contracts.validate(good.drop(columns="date"), contracts.PREPARED_CASES))


def test_units_whose_series_stop_before_the_run_date_are_listed():
    table = pd.DataFrame({"region_id": ["district_A1", "district_A2", "district_A2"],
                          "date": ["2025-12-28", "2025-12-21", "2025-12-26"],
                          "case_count": [0, 4, 1]})
    check = verify.series_reach_run_date(table, "2025-12-28")
    assert check.status == WARN and check.detail == {"days_short_per_unit": {"district_A2": 2}}
    assert verify.series_reach_run_date(table[:1], "2025-12-28").status == PASS


def test_swapped_day_and_month_is_caught_even_though_the_total_matches(tmp_path):
    expected = pd.DataFrame({"region_id": ["district_A1", "district_A1"],
                             "date": ["2024-03-04", "2024-05-06"], "cases": [3, 2]})
    expected.to_csv(tmp_path / "expected_cases.csv", index=False)
    swapped = pd.DataFrame({"region_id": ["district_A1", "district_A1"],
                            "date": ["2024-04-03", "2024-06-05"], "case_count": [3, 2]})
    swapped.to_csv(tmp_path / "cases_daily.csv", index=False)
    _, checks = verify.prepared_cases(tmp_path / "cases_daily.csv", {"district_A1"}, 5,
                                      tmp_path / "expected_cases.csv")
    assert status(checks, "prepared_total_as_expected") == PASS
    assert status(checks, "prepared_matches_inspected") == FAIL
    swapped.assign(date=expected.date).to_csv(tmp_path / "cases_daily.csv", index=False)
    _, checks = verify.prepared_cases(tmp_path / "cases_daily.csv", {"district_A1"}, 5,
                                      tmp_path / "expected_cases.csv")
    assert status(checks, "prepared_matches_inspected") == PASS


def test_weather_in_celsius_is_caught(tmp_path):
    days = pd.date_range("2024-01-01", "2024-01-10").strftime("%Y-%m-%d")
    table = pd.DataFrame({"region_id": "district_A1", "date": days,
                          "2mTemperature": 24.5, "2mDewpointTemperature": 18.0,
                          "totalPrecipitation": 0.002})
    path = tmp_path / "weather_daily.csv"
    table.to_csv(path, index=False)
    checks = verify.prepared_weather(path, {"district_A1"}, "2024-01-01", "2024-01-10")
    assert status(checks, "prepared_weather_2mTemperature") == FAIL
    table.assign(**{"2mTemperature": 297.6, "2mDewpointTemperature": 291.1}).to_csv(
        path, index=False)
    assert not failed(verify.prepared_weather(path, {"district_A1"}, "2024-01-01",
                                              "2024-01-10"))


def test_weather_missing_days_and_units_are_caught(tmp_path):
    days = pd.date_range("2024-01-01", "2024-01-09").strftime("%Y-%m-%d")
    table = pd.DataFrame({"region_id": "district_A1", "date": days,
                          "2mTemperature": 297.6, "2mDewpointTemperature": 291.1,
                          "totalPrecipitation": 0.0})
    path = tmp_path / "weather_daily.csv"
    table.to_csv(path, index=False)
    checks = verify.prepared_weather(path, {"district_A1", "district_A2"},
                                     "2024-01-01", "2024-01-10")
    assert status(checks, "weather_all_units") == FAIL
    assert status(checks, "weather_all_days") == FAIL


def test_predictions_missing_a_unit_warns(tmp_path):
    weeks = ["2026-01-04", "2026-01-11", "2026-01-18", "2026-01-25"]
    table = pd.DataFrame({"regionID": "district_A1", "startDatePredictedWeek": weeks,
                          "dateOfComputingPrediction": "2025-12-28", "model": "rf",
                          "thresholdMethod": "historical", "predictionRaw": 3.2,
                          "prediction": 3})
    path = tmp_path / "predictions.csv"
    table.to_csv(path, index=False)
    info, checks = verify.predictions(path, {"district_A1", "district_A2"})
    assert status(checks, "forecast_all_units") == WARN
    assert status(checks, "forecast_all_weeks") == PASS
    assert info["forecast_weeks"] == weeks


def test_forecast_far_from_recent_counts_warns(tmp_path):
    weeks = pd.date_range("2025-11-30", periods=5, freq="7D").strftime("%Y-%m-%d")
    observed = pd.DataFrame({
        "location.admin2.ID": ["district_A1"] * 5 + ["district_A2"] * 5 + ["district_A3"] * 5,
        "metadata.primaryDate": list(weeks) * 3,
        "case": [90, 12, 10, 12, 14] + [8, 8, 8, 8, 8] + [1, 0, 1, 2, 1]})
    observed.to_csv(tmp_path / "observed.csv", index=False)
    table = pd.DataFrame({
        "regionID": ["district_A1", "district_A2", "district_A3"] * 2,
        "startDatePredictedWeek": ["2026-01-04"] * 3 + ["2026-01-11"] * 3,
        "predictionRaw": [4.0, 7.5, 0.0, 12.0, 8.0, 0.0]})
    check = verify.forecast_near_recent(table, tmp_path / "observed.csv")
    assert check.status == WARN  # A1 at 4 against 12; A3 is too small to judge
    assert check.detail == {"units": {"district_A1": {"recent": 12.0, "forecast": 4.0}}}
    table.loc[0, "predictionRaw"] = 11.0
    assert verify.forecast_near_recent(table, tmp_path / "observed.csv").status == PASS


def test_a_year_at_twice_the_level_of_the_others_warns():
    dates = pd.Series(pd.date_range("2021-12-27", "2025-12-29", freq="7D"))
    counts = pd.Series(10, index=dates.index).mask(dates.dt.year == 2024, 19)
    check = qc.check_year_levels(dates, counts)
    assert check.status == WARN and check.detail["ratio_to_other_years"] == {2024: 1.9}
    assert list(check.detail["median_weekly_cases_per_full_year"]) == [2022, 2023, 2024, 2025]
    assert qc.check_year_levels(dates, pd.Series(10, index=dates.index)).status == PASS


def test_a_rerun_keeps_the_earlier_report(tmp_path):
    from report import write_audit
    write_audit(tmp_path, "inspect", {"ready": False}, [])
    write_audit(tmp_path, "inspect", {"ready": True}, [])
    kept = json.loads((tmp_path / "onboarding" / "inspect.run1.json").read_text())
    latest = json.loads((tmp_path / "onboarding" / "inspect.json").read_text())
    assert (kept["ready"], latest["ready"]) == (False, True)


def test_tuned_hyperparameters_are_read_from_beside_the_run(tmp_path):
    (tmp_path / "hp").mkdir()
    (tmp_path / "hp" / "rf_district_best_params.json").write_text(
        json.dumps({"best_rmse": 5.18, "params": {"max_depth": 11}}))
    tuned = verify.tuned_hyperparameters(tmp_path / "fc-2025-12-28")
    assert tuned == {"rf_district": {"best_rmse": 5.18, "params": {"max_depth": 11}}}


# --- configs ----------------------------------------------------------------------------------

@pytest.mark.parametrize("run_date, expected, days", [
    ("2025-12-28", PASS, 0), ("2025-12-21", PASS, 0), ("2026-01-04", FAIL, 7)])
def test_run_date_after_the_last_record_fails(run_date, expected, days):
    check = qc.check_run_date(pd.Timestamp(run_date), pd.Timestamp("2025-12-28"))
    assert check.status == expected and check.detail == {"days_after_last_record": days}


def test_configs_stage_refuses_a_run_date_past_the_data(layout):
    (layout / "onboarding").mkdir(exist_ok=True)
    pd.DataFrame({"region_id": ["district_A1"], "date": ["2025-12-28"], "cases": [3]}).to_csv(
        layout / "onboarding" / "expected_cases.csv", index=False)
    args = dict(workdir=str(layout), region_type="district", name="tl", title="Testland",
                date_start="2023-01-01", history_years=3, models=["rf"], linelist_dir=None)
    late = onboard.run_configs(argparse.Namespace(run_date="2026-01-04", **args))
    assert {c["id"] for c in late["checks"] if c["status"] == FAIL} == {"run_date_has_data"}
    assert not list(layout.glob("*.yaml"))
    assert onboard.run_configs(argparse.Namespace(run_date="2025-12-28", **args))["configs"]


def test_columns_reads_a_folder_of_boundary_files(layout):
    listing = onboard.run_columns(argparse.Namespace(file=str(layout / "geojsons" / "districts")))
    assert listing["rows"] == 3 and "geometry" not in listing["distinct_values_per_column"]


def test_configs_point_at_the_work_folder_and_are_bounded_by_the_run_date(layout):
    geo = configs.Geography("tl", "Testland", layout, "district", 3, "2023-01-01",
                            "2025-12-28", 3)
    prep, forecast = configs.prep_config(geo), configs.forecast_config(geo)
    assert prep["data"]["case_download"] == {"enabled": False}
    assert prep["data"]["geojson"]["base_path"] == str(layout / "geojsons")
    assert prep["run"]["run_date"] == "2025-12-28"
    assert prep["data"]["date_range"] == {"start": "2023-01-01", "end": "2025-12-28"}
    assert forecast["data"]["prepared_data"]["base_dir"] == str(layout / "prepared_data")
    assert forecast["run"]["run_date"] == "2025-12-28"
    assert forecast["assess"] == {"total_district_regions": 3}
    assert forecast["report"]["primary"] == "rf"


def test_line_list_config_turns_the_parser_on_with_its_filters(layout):
    line_list_options = configs.LineList(
        layout / "raw", "Test Date", code_column="Area Code",
        filters=(("Test Result", ["Positive"]),))
    geo = configs.Geography("tl", "Testland", layout, "district", 3, "2023-01-01",
                            "2025-12-28", 3, line_list=line_list_options)
    parse = configs.prep_config(geo)["data"]["case_parse"]
    assert configs.prep_config(geo)["data"]["case_download"]["enabled"] is True
    assert parse["lgd_code_column"] == "Area Code" and "region_id_column" not in parse
    assert parse["filters"] == [{"column": "Test Result", "values": ["Positive"]}]


def test_block_level_is_not_among_the_supported_levels():
    assert "block" not in configs.SUPPORTED_LEVELS and "district" in configs.SUPPORTED_LEVELS
