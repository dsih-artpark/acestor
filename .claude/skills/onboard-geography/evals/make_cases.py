"""Three made-up geographies for testing the onboarding skill, built by formula.

    weekly-aggregated   weekly counts, day-first dates, one unit with no boundary
    line-list           one row per case, numeric unit codes, boundaries as a shapefile
    flawed-data         weeks dated on mixed weekdays, a unit that stops reporting,
                        and under a year of history
    yearly-line-list    one line-list file per year, dates as timestamps ending in Z
    daily-blank-counts  daily counts in one file per year, some counts left blank

Usage: uv run python make_cases.py <output folder>
"""

import math
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

OUT = Path(sys.argv[1])
NAMES = ["Northfield", "Eastbank", "Riverside", "Hillview", "Lakeside", "Westgate"]
SCALE = [40, 30, 22, 15, 9, 4]
YEAR_FACTOR = {2022: 0.8, 2023: 1.0, 2024: 1.9, 2025: 1.1}


def districts(codes: list[str], code_col: str, name_col: str) -> gpd.GeoDataFrame:
    shapes = [box(77.2 + 0.25 * (i % 3), 12.8 + 0.25 * (i // 3),
                  77.45 + 0.25 * (i % 3), 13.05 + 0.25 * (i // 3)) for i in range(len(codes))]
    return gpd.GeoDataFrame({code_col: codes, name_col: NAMES[: len(codes)]},
                            geometry=shapes, crs="EPSG:4326")


def weekly(codes: list[str], start: str, end: str) -> pd.DataFrame:
    rows = []
    for i, code in enumerate(codes):
        for k, week in enumerate(pd.date_range(start, end, freq="7D")):
            season = 0.25 + math.exp(-0.5 * ((week.dayofyear - 215) / 45.0) ** 2)
            wiggle = 1 + 0.25 * math.sin(k * 1.7 + i)
            rows.append((week, code,
                         int(round(SCALE[i] * season * YEAR_FACTOR[week.year] * wiggle))))
    return pd.DataFrame(rows, columns=["date", "code", "cases"])


def weekly_aggregated() -> None:
    folder = OUT / "weekly-aggregated"
    folder.mkdir(parents=True, exist_ok=True)
    codes = [f"D0{i}" for i in range(1, 7)]
    districts(codes, "DIST_CODE", "DIST_NAME").to_file(folder / "districts.geojson",
                                                       driver="GeoJSON")
    table = weekly(codes, "2022-01-02", "2025-12-28")
    table = pd.concat([table, pd.DataFrame(
        {"date": [pd.Timestamp("2025-12-28")], "code": ["D99"], "cases": [3]})])
    table.assign(date=table.date.dt.strftime("%d-%m-%Y")).rename(columns={
        "date": "Week Ending", "code": "District Code", "cases": "Dengue Cases"}
    ).to_csv(folder / "weekly_dengue.csv", index=False)
    print("weekly-aggregated: cases in known units",
          int(table[table.code != "D99"].cases.sum()))


def line_list() -> None:
    folder = OUT / "line-list"
    (folder / "boundaries").mkdir(parents=True, exist_ok=True)
    (folder / "cases").mkdir(exist_ok=True)
    codes = [str(code) for code in range(501, 507)]
    districts(codes, "LGD_CODE", "DISTRICT").to_file(folder / "boundaries" / "districts.shp")
    table = weekly(codes, "2022-01-02", "2025-12-28")
    rows = table.loc[table.index.repeat(table.cases)]
    pd.DataFrame({
        "Sample Collected Date": rows.date.dt.strftime("%d/%m/%Y").values,
        "District Code": rows.code.values,
        "Test Result": "Positive",
    }).to_csv(folder / "cases" / "dengue_linelist.csv", index=False)
    print("line-list: rows", len(rows))


def flawed_data() -> None:
    folder = OUT / "flawed-data"
    folder.mkdir(parents=True, exist_ok=True)
    codes = [f"B{i}" for i in range(1, 7)]
    districts(codes, "CODE", "NAME").to_file(folder / "blocks.geojson", driver="GeoJSON")
    table = weekly(codes, "2025-03-02", "2025-12-28")  # 44 weeks
    table = table[~((table.code == "B4") & (table.date > "2025-08-01"))]  # stops reporting
    shifted = table.index % 6 == 0  # one week in six is dated on the Monday instead
    table.loc[shifted, "date"] = table.loc[shifted, "date"] + pd.Timedelta(days=1)
    table.assign(date=table.date.dt.strftime("%Y-%m-%d")).rename(columns={
        "date": "week_date", "code": "block_code", "cases": "cases"}
    ).to_csv(folder / "block_weekly_cases.csv", index=False)
    print("flawed-data: rows", len(table))


def yearly_line_list() -> None:
    folder = OUT / "yearly-line-list"
    (folder / "cases").mkdir(parents=True, exist_ok=True)
    codes = [str(code) for code in range(701, 707)]
    districts(codes, "LGD_CODE", "DISTRICT").to_file(folder / "districts.geojson",
                                                     driver="GeoJSON")
    table = weekly(codes, "2022-01-02", "2025-12-28")
    rows = table.loc[table.index.repeat(table.cases)].reset_index(drop=True)
    rows["date"] = rows.date - pd.to_timedelta(rows.index % 7, unit="D")  # real weekdays
    for year, part in rows.groupby(rows.date.dt.year):
        pd.DataFrame({
            "sample_date": part.date.dt.strftime("%Y-%m-%dT00:00:00Z").values,
            "district_code": part.code.values,
            "result": "Positive",
        }).to_csv(folder / "cases" / f"linelist_{year}.csv", index=False)
    print("yearly-line-list: rows", len(rows))


def daily_blank_counts() -> None:
    folder = OUT / "daily-blank-counts"
    (folder / "cases").mkdir(parents=True, exist_ok=True)
    codes = [f"T{i}" for i in range(1, 7)]
    districts(codes, "CODE", "NAME").to_file(folder / "districts.geojson", driver="GeoJSON")
    days = pd.date_range("2022-01-01", "2025-12-31", freq="D")
    rows = []
    for i, code in enumerate(codes):
        for k, day in enumerate(days):
            season = 0.25 + math.exp(-0.5 * ((day.dayofyear - 215) / 45.0) ** 2)
            rows.append((day, code, int(round(
                SCALE[i] / 7 * season * YEAR_FACTOR[day.year] * (1 + 0.25 * math.sin(k + i))))))
    table = pd.DataFrame(rows, columns=["date", "code", "cases"])
    blank = table.index % 37 == 0
    shown = table.cases.astype(str).mask(blank, "")
    for year, part in table.assign(cases=shown).groupby(table.date.dt.year):
        part.assign(date=part.date.dt.strftime("%Y-%m-%d")).rename(columns={
            "date": "report_date", "code": "district", "cases": "positives"}
        ).to_csv(folder / "cases" / f"daily_{year}.csv", index=False)
    print("daily-blank-counts: blank rows", int(blank.sum()),
          "cases with blanks as zero", int(table.cases[~blank].sum()))


if __name__ == "__main__":
    weekly_aggregated()
    line_list()
    flawed_data()
    yearly_line_list()
    daily_blank_counts()
