"""
Pipeline runner with config overrides
======================================
A thin wrapper around the main pipeline that lets you pass config overrides
from the command line using dotted-key notation, without touching any YAML file.

QUICK START
-----------
A config file is always required:

    uv run python scripts/run.py --config configs/ap_district_v3.yaml

Override any config value using dotted keys:

    uv run python scripts/run.py --config configs/ap_district_v3.yaml --set run.run_date=2026-03-13
    uv run python scripts/run.py --config configs/ap_district_v3.yaml --set thresholds.method_configs.prev_nweeks.n_weeks=8
    uv run python scripts/run.py --config configs/ap_district_v3.yaml --set model.models="[nbr, xgb]"

Multiple overrides at once:

    uv run python scripts/run.py \\
        --config configs/ap_district_v3.yaml \\
        --run-id my-test-run \\
        --set run.run_date=2026-03-13 \\
        --set thresholds.method_configs.historical.historical_n_years=2 \\
        --set thresholds.method_configs.prev_nweeks.n_weeks=8 \\
        --set model.models="[nbr, tse]"

VALUE PARSING
-------------
Values are parsed as YAML scalars, so types are inferred automatically:
    4           → integer
    2.5         → float
    true        → boolean
    2026-03-13  → string (quoted by YAML as a date string)
    "[nbr,xgb]" → list
    "[]"        → empty list

Wrap values containing spaces or special characters in quotes.

DOTTED KEY NOTATION
-------------------
Keys follow the YAML structure with dots as separators:

    thresholds.method_configs.prev_nweeks.n_weeks=8
    └── thresholds
        └── method_configs
            └── prev_nweeks
                └── n_weeks: 8

If a key doesn't exist in the base config, it is created.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_PIPELINE = "pipelines.dengue.pipeline:build_pipeline"


def set_nested(d: dict, dotted_key: str, value) -> None:
    """Set d[k1][k2][...][kn] = value, creating intermediate dicts as needed."""
    keys = dotted_key.split(".")
    node = d
    for k in keys[:-1]:
        if k not in node or not isinstance(node[k], dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value


def parse_value(raw: str):
    """
    Parse a string value from the CLI into the appropriate Python type.
    Uses YAML's own parser so integers, floats, booleans, and lists all work naturally.

    Examples:
        "4"         → 4
        "2.5"       → 2.5
        "true"      → True
        "[nbr,xgb]" → ["nbr", "xgb"]
        "foo"       → "foo"
    """
    return yaml.safe_load(raw)


def apply_overrides(config: dict, overrides: list[str]) -> dict:
    """
    Apply a list of "dotted.key=value" overrides to the config dict in place.
    Returns the modified dict.
    """
    for override in overrides:
        if "=" not in override:
            print(f"  [error] Invalid override (missing '='): {override}")
            print("          Expected format: dotted.key=value")
            sys.exit(1)
        key, _, raw_value = override.partition("=")
        value = parse_value(raw_value)
        set_nested(config, key.strip(), value)
        print(f"  override  {key.strip()} = {value!r}")
    return config


def main():
    parser = argparse.ArgumentParser(
        description="Run the dengue pipeline with optional config overrides.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # Run with no overrides (--config is required)
  uv run python scripts/run.py --config configs/ap_district_v3.yaml

  # Change run date
  uv run python scripts/run.py --config configs/ap_district_v3.yaml --set run.run_date=2026-03-13

  # Change n_weeks and historical years
  uv run python scripts/run.py --config configs/ap_district_v3.yaml \\
      --set thresholds.method_configs.prev_nweeks.n_weeks=8 \\
      --set thresholds.method_configs.historical.historical_n_years=2

  # Run only NBR and XGB models
  uv run python scripts/run.py --config configs/ap_district_v3.yaml --set "model.models=[nbr, xgb]"

  # Full custom run
  uv run python scripts/run.py \\
      --config configs/ap_district_v3.yaml \\
      --run-id my-experiment \\
      --set run.run_date=2026-03-27 \\
      --set thresholds.method_configs.prev_nweeks.n_weeks=6 \\
      --set model.ensemble=none \\
      --set model.output=per_model
        """,
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="FILE",
        help="Base config YAML file (e.g. configs/ap_district_v3.yaml)",
    )
    parser.add_argument(
        "--pipeline",
        default=DEFAULT_PIPELINE,
        metavar="MODULE:FUNC",
        help=f"Pipeline entry point (default: {DEFAULT_PIPELINE})",
    )
    parser.add_argument(
        "--run-id",
        metavar="ID",
        help="Run ID for artifact storage (default: auto-generated timestamp)",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override a config value using dotted key notation. Can be repeated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the final config and command without actually running the pipeline.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: config file not found: {config_path}")
        sys.exit(1)

    # Load base config
    with open(config_path) as f:
        config = yaml.safe_load(f)

    print(f"\nBase config:  {config_path}")

    # Apply overrides
    if args.set:
        print("Overrides:")
        apply_overrides(config, args.set)
    else:
        print("No overrides — using base config as-is.")

    # Write to a temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="run_override_", delete=False
    ) as tmp:
        yaml.dump(config, tmp, default_flow_style=False, allow_unicode=True)
        tmp_path = tmp.name

    print(f"\nTemp config:  {tmp_path}")

    # Build the run command
    cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "acestor.run",
        "--pipeline",
        args.pipeline,
        "--config",
        tmp_path,
    ]
    if args.run_id:
        cmd += ["--run-id", args.run_id]

    print(f"Command:      {' '.join(cmd)}\n")

    if args.dry_run:
        print("── Dry run: final config ──────────────────────────────")
        print(yaml.dump(config, default_flow_style=False, allow_unicode=True))
        print("(pipeline not started — remove --dry-run to actually run)")
        return

    # Run
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
