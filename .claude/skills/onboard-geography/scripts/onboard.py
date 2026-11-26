"""Onboard a new geography to the acestor pipelines: command-line entry point.

    contracts         print the schema contracts as Markdown
    columns           list the columns of a case or boundary file
    inspect           quality checks on a case file
    boundaries        check a boundary file and write acestor's per-unit layout
    cases             aggregated counts -> prepared_data/<level>/cases_daily.csv
    configs           write the prep and forecast configs
    verify-prep       check prepared cases and weather after the prep pipeline
    verify-forecast   check the predictions after the forecast pipeline

Each stage prints the checks that did not pass, saves the full report under
<workdir>/onboarding/, and exits 1 when any check fails. Add --full to print
every check. Run from the repository root with `uv run python`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

import boundaries
import cases
import configs
import contracts
import qc
import verify
from report import FAIL, PASS, Check, failed, summarise, write_audit

EXPECTED_CASES = "expected_cases.csv"


def _workdir(args: argparse.Namespace) -> Path | None:
    return Path(args.workdir).resolve() if getattr(args, "workdir", None) else None


def _boundary_units(args: argparse.Namespace) -> set[str] | None:
    work = _workdir(args)
    return (boundaries.unit_ids(work / "geojsons", args.region_type) or None) if work else None


def _finish(args: argparse.Namespace, stage: str, info: dict, checks: list[Check],
            inputs: list[Path]) -> dict:
    report = {**info, **summarise(checks)}
    if _workdir(args):
        report["audit_file"] = str(write_audit(_workdir(args), stage, report, inputs))
    return report


def run_contracts(args: argparse.Namespace) -> dict:
    print("\n".join(contracts.describe(c) for c in contracts.ALL))
    return {}


def run_columns(args: argparse.Namespace) -> dict:
    """Column names with how many distinct values each holds. No values are
    shown, because a line list's values are personal data."""
    path = Path(args.file)
    if cases.source_files(path):
        table = cases.read_table(path)
    else:
        table = boundaries.read_boundaries(path).drop(columns="geometry")
    distinct = {column: int(table[column].nunique()) for column in table.columns}
    return {"file": str(path), "rows": len(table), "distinct_values_per_column": distinct}


def run_inspect(args: argparse.Namespace) -> dict:
    info, checks, expected = cases.inspect(
        Path(args.cases), args.date_col, args.unit_col, args.count_col, args.region_type,
        _boundary_units(args), pd.Timestamp.today().normalize(),
        cases.parse_filters(args.filter), args.blank_as_zero)
    if expected is not None and _workdir(args):
        folder = _workdir(args) / "onboarding"
        folder.mkdir(parents=True, exist_ok=True)
        expected.to_csv(folder / EXPECTED_CASES, index=False)
    return _finish(args, "inspect", info, checks, cases.source_files(Path(args.cases)))


def run_boundaries(args: argparse.Namespace) -> dict:
    info, checks = boundaries.prepare(
        Path(args.input), args.code_col, args.name_col, args.region_type,
        args.parent_id, args.parent_name, _workdir(args) / "geojsons")
    return _finish(args, "boundaries", info, checks, [Path(args.input)])


def run_cases(args: argparse.Namespace) -> dict:
    info, checks = cases.standardise(
        Path(args.input), args.date_col, args.unit_col, args.count_col, args.region_type,
        _boundary_units(args) or set(), _workdir(args) / "prepared_data",
        args.sum_breakdowns, args.blank_as_zero)
    return _finish(args, "cases", info, checks, cases.source_files(Path(args.input)))


def run_configs(args: argparse.Namespace) -> dict:
    n_units = len(_boundary_units(args) or ())
    checks = [
        Check("level_supported",
              PASS if args.region_type in configs.SUPPORTED_LEVELS else FAIL,
              f"Level {args.region_type!r}; the forecast pipeline supports "
              f"{list(configs.SUPPORTED_LEVELS)}."),
        Check("boundaries_present", PASS if n_units else FAIL,
              f"{n_units} boundary units found for this level."),
    ]
    expected = _workdir(args) / "onboarding" / EXPECTED_CASES
    if expected.exists():
        last = pd.to_datetime(pd.read_csv(expected).date).max()
        checks.append(qc.check_run_date(pd.Timestamp(args.run_date), last))
    else:
        checks.append(Check("run_date_has_data", FAIL,
                            "No inspected case data in this work folder. Run "
                            "`inspect` with --workdir first."))
    if failed(checks):
        return _finish(args, "configs", {}, checks, [expected])
    line_list = None
    if args.linelist_dir:
        line_list = configs.LineList(
            Path(args.linelist_dir).resolve(), args.linelist_date_col,
            args.linelist_unit_col or "", args.linelist_code_col or "",
            tuple(cases.parse_filters(args.linelist_filter)))
    geo = configs.Geography(args.name, args.title, _workdir(args), args.region_type,
                            n_units, args.date_start, args.run_date, args.history_years,
                            args.models, line_list)
    info = {"configs": configs.write(geo), "units": n_units}
    return _finish(args, "configs", info, checks, [])


def run_verify_prep(args: argparse.Namespace) -> dict:
    work = _workdir(args)
    prepared = work / "prepared_data" / args.region_type
    known = _boundary_units(args) or set()
    info, checks = verify.prepared_cases(prepared / "cases_daily.csv", known,
                                         args.expect_cases,
                                         work / "onboarding" / EXPECTED_CASES,
                                         args.weather_end)
    checks += verify.prepared_weather(prepared / "weather_daily.csv", known,
                                      args.date_start, args.weather_end)
    return _finish(args, "verify_prep", info, checks,
                   [prepared / "cases_daily.csv", prepared / "weather_daily.csv"])


def run_verify_forecast(args: argparse.Namespace) -> dict:
    run_dir = Path(args.run_dir).resolve()
    path = run_dir / "outputs" / "predictions.csv"
    observed = run_dir / "inputs" / f"cases_{args.region_type}_sampled.csv"
    info, checks = verify.predictions(path, _boundary_units(args) or set(), observed)
    info["tuned_hyperparameters"] = verify.tuned_hyperparameters(run_dir)
    return _finish(args, "verify_forecast", info, checks, [path])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def stage(name: str, run, help_text: str, workdir_required: bool = True):
        p = commands.add_parser(name, help=help_text)
        p.add_argument("--workdir", required=workdir_required,
                       help="folder holding this geography's files")
        p.add_argument("--region-type", default="district",
                       help="administrative level, e.g. district, zone, ward")
        p.add_argument("--full", action="store_true", help="print every check")
        p.set_defaults(run=run)
        return p

    commands.add_parser("contracts", help="print the schema contracts").set_defaults(
        run=run_contracts)
    p = commands.add_parser("columns", help="list a file's columns")
    p.add_argument("--file", required=True)
    p.set_defaults(run=run_columns)

    p = stage("inspect", run_inspect, "quality checks on a case file",
              workdir_required=False)
    p.add_argument("--cases", required=True, help="a file, or a folder of files")
    p.add_argument("--blank-as-zero", action="store_true",
                   help="read blank counts as zero, once the user confirms that")
    p.add_argument("--date-col", required=True)
    p.add_argument("--unit-col", required=True)
    p.add_argument("--count-col", help="the count column; leave out for a line list")
    p.add_argument("--filter", action="append",
                   help="keep rows where a column has one of these values, "
                        "e.g. 'Test Result=Positive,Reactive'; repeatable")

    p = stage("boundaries", run_boundaries, "check and lay out a boundary file")
    p.add_argument("--input", required=True,
                   help="GeoJSON, shapefile or GeoPackage, or a folder of GeoJSON files")
    p.add_argument("--code-col", required=True)
    p.add_argument("--name-col", required=True)
    p.add_argument("--parent-id", default="", help="optional ID of the unit above")
    p.add_argument("--parent-name", default="", help="optional name of the unit above")

    p = stage("cases", run_cases, "aggregated counts to cases_daily.csv")
    p.add_argument("--input", required=True)
    p.add_argument("--date-col", required=True)
    p.add_argument("--unit-col", required=True)
    p.add_argument("--count-col", required=True)
    p.add_argument("--blank-as-zero", action="store_true",
                   help="read blank counts as zero, once the user confirms that")
    p.add_argument("--sum-breakdowns", action="store_true",
                   help="add together rows that split one unit and date into groups")

    p = stage("configs", run_configs, "write prep and forecast configs")
    p.add_argument("--name", required=True, help="short code, e.g. xy")
    p.add_argument("--title", required=True, help="display name of the geography")
    p.add_argument("--date-start", required=True)
    p.add_argument("--run-date", required=True)
    p.add_argument("--history-years", type=int, default=3,
                   help="calendar years of data available for thresholds")
    p.add_argument("--models", nargs="+", default=["rf"])
    p.add_argument("--linelist-dir")
    p.add_argument("--linelist-date-col")
    p.add_argument("--linelist-unit-col", help="column of full unit IDs, e.g. district_344")
    p.add_argument("--linelist-code-col", help="column of numeric unit codes, e.g. 344")
    p.add_argument("--linelist-filter", action="append",
                   help="same form as inspect --filter; repeatable")

    p = stage("verify-prep", run_verify_prep, "check prepared cases and weather")
    p.add_argument("--date-start", required=True)
    p.add_argument("--weather-end", required=True, help="last day weather is needed for")
    p.add_argument("--expect-cases", type=int)

    p = stage("verify-forecast", run_verify_forecast, "check a forecast run")
    p.add_argument("--run-dir", required=True)
    return parser


def _brief(report: dict) -> dict:
    """The header, the counts, and only the checks that need attention."""
    shown = [c for c in report.get("checks", []) if c["status"] != PASS]
    return {**{k: v for k, v in report.items() if k != "checks"}, "checks_not_passed": shown}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = args.run(args)
    if report:
        shown = report if getattr(args, "full", True) or "checks" not in report else _brief(report)
        json.dump(shown, sys.stdout, indent=2, default=str)
        print()
    return 0 if report.get("ready", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
