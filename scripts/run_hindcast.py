"""
Hindcast runner — re-runs the pipeline for every Tuesday from start_date to end_date,
generating predictions and a report for each week.

Usage:
    uv run python scripts/run_hindcast.py \
        --config configs/ap_district.yaml \
        --start 2023-01-01 \
        --end 2026-03-01 \
        [--output-dir artifacts/ap_hindcast] \
        [--dry-run]

Each weekly run writes its artifacts under {output_dir}/{YYYYMMDD}/.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import yaml


def tuesdays_between(start: date, end: date) -> list[date]:
    """Return every Tuesday in [start, end]."""
    days = []
    d = start
    while d.weekday() != 1:  # 1 = Tuesday
        d += timedelta(days=1)
    while d <= end:
        days.append(d)
        d += timedelta(weeks=1)
    return days


def main() -> None:
    parser = argparse.ArgumentParser(description="Hindcast batch runner")
    parser.add_argument("--config", required=True, help="Base pipeline YAML config")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--output-dir",
        default="artifacts/hindcast",
        help="Base directory for all run artifacts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the run dates without executing",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    weeks = tuesdays_between(start, end)

    print(f"Hindcast: {len(weeks)} weekly runs from {weeks[0]} to {weeks[-1]}")

    base_config_path = Path(args.config)
    with open(base_config_path) as f:
        base_config = yaml.safe_load(f)

    failed = []

    for run_date in weeks:
        run_id = f"hindcast_{run_date.strftime('%Y%m%d')}"
        run_date_str = run_date.isoformat()

        if args.dry_run:
            print(f"  [dry-run] {run_id}  run_date={run_date_str}")
            continue

        # Build per-week config: override run_date, base_path, and skip intermediates
        cfg = yaml.safe_load(yaml.dump(base_config))  # deep copy
        cfg.setdefault("run", {})["run_date"] = run_date_str
        cfg.setdefault("storages", {}).setdefault("artifacts", {}).setdefault(
            "filesystem", {}
        )["base_path"] = str(Path(args.output_dir) / run_date.strftime("%Y%m%d"))
        cfg.setdefault("data", {}).setdefault("weather_parse", {}).update(
            {"write_agg_daily": False, "write_agg_ndays": False}
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, prefix=f"hindcast_{run_id}_"
        ) as tmp:
            yaml.dump(cfg, tmp)
            tmp_path = tmp.name

        print(f"  Running {run_id}  run_date={run_date_str} ... ", end="", flush=True)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "acestor.run",
                "--pipeline",
                "pipelines.dengue.pipeline:build_pipeline",
                "--config",
                tmp_path,
                "--run-id",
                run_id,
            ],
            capture_output=False,
        )
        Path(tmp_path).unlink(missing_ok=True)

        if result.returncode == 0:
            print("OK")
        else:
            print("FAILED")
            failed.append(run_id)

    if failed:
        print(f"\n{len(failed)} runs failed: {failed}")
        sys.exit(1)
    elif not args.dry_run:
        print(f"\nAll {len(weeks)} runs completed.")


if __name__ == "__main__":
    main()
