from __future__ import annotations
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).parent
TARUN = ROOT / "dengue-model-pipeline-automation - GBA/src/dengue-model-pipeline"
ACESTOR = ROOT / "artifacts/test-sot-cols"

FILE_PAIRS = [
    (
        ACESTOR / "datasets/cases_zone_sampled.csv",
        TARUN / "datasets/cases_zone_sampled.csv",
        "cases_zone_sampled",
    ),
    (
        ACESTOR / "datasets/weather_zone_sampled.csv",
        TARUN / "datasets/weather_zone_sampled.csv",
        "weather_zone_sampled",
    ),
    (
        ACESTOR / "datasets/agg_daily/zone",
        TARUN / "datasets/agg_daily/zone",
        "agg_daily/zone",
    ),
    (
        ACESTOR / "datasets/agg_Ndays/zone",
        TARUN / "datasets/agg_Ndays/zone",
        "agg_Ndays/zone",
    ),
    (
        ACESTOR / "datasets/thresholds/zone_all_thresholds.csv",
        TARUN / "datasets/thresholds/zone_all_thresholds.csv",
        "thresholds/zone",
    ),
]

PASS, WARN, FAIL, MISS = "PASS ", "WARN ", "FAIL ", "MISS "
_C = {PASS: "\033[92m", WARN: "\033[93m", FAIL: "\033[91m", MISS: "\033[90m"}
_R = "\033[0m"


def compare_csvs(acestor: Path, tarun: Path) -> tuple[str, str]:
    try:
        df1 = pd.read_csv(acestor).loc[
            :, lambda d: ~d.columns.str.startswith("Unnamed")
        ]
        df2 = pd.read_csv(tarun).loc[:, lambda d: ~d.columns.str.startswith("Unnamed")]
    except Exception as e:
        return FAIL, f"read error: {e}"

    c1, c2 = set(df1.columns), set(df2.columns)
    issues = []
    if c1 != c2:
        issues.append(f"cols missing={sorted(c2-c1)} extra={sorted(c1-c2)}")
    if len(df1) != len(df2):
        issues.append(f"rows acestor={len(df1)} tarun={len(df2)}")
        return WARN, "; ".join(issues)

    shared = sorted(c1 & c2)
    df1, df2 = df1[shared].reset_index(drop=True), df2[shared].reset_index(drop=True)
    mismatches = []
    for col in shared:
        s1, s2 = df1[col], df2[col]
        if pd.api.types.is_numeric_dtype(s1) and pd.api.types.is_numeric_dtype(s2):
            if not ((s1 - s2).abs() < 1e-6).all():
                mismatches.append(f"{col}(max_diff={(s1-s2).abs().max():.2e})")
        elif not s1.astype(str).equals(s2.astype(str)):
            mismatches.append(
                f"{col}({(s1.astype(str) != s2.astype(str)).sum()} cells differ)"
            )

    if mismatches:
        issues.append(f"value mismatches: {mismatches}")
    if not issues:
        return PASS, f"{len(df1)} rows, {len(shared)} cols — exact match"
    return FAIL if mismatches else WARN, "; ".join(issues)


def compare_pair(acestor: Path, tarun: Path, label: str) -> list[tuple[str, str, str]]:
    if not acestor.exists():
        return [(label, FAIL, f"acestor path not found: {acestor}")]
    if not tarun.exists():
        return [(label, MISS, f"not found in tarun: {tarun}")]
    if acestor.is_dir():
        files = sorted(acestor.rglob("*.csv"))
        if not files:
            return [(label, WARN, "folder is empty")]
        return [
            (
                (f"{label}/{f.relative_to(acestor)}", MISS, "not in tarun")
                if not (tarun / f.relative_to(acestor)).exists()
                else (
                    f"{label}/{f.relative_to(acestor)}",
                    *compare_csvs(f, tarun / f.relative_to(acestor)),
                )
            )
            for f in files
        ]
    return [(label, *compare_csvs(acestor, tarun))]


def main() -> None:
    rows = [
        r
        for acestor, tarun, label in FILE_PAIRS
        for r in compare_pair(acestor, tarun, label)
    ]
    col_w = max(len(r[0]) for r in rows) + 2
    print(f"\n{'File':<{col_w}} Status  Detail\n{'-'*(col_w+60)}")
    counts: dict[str, int] = {}
    for label, status, detail in rows:
        print(f"{label:<{col_w}} {_C[status]}{status}{_R}  {detail}")
        counts[status] = counts.get(status, 0) + 1
    print(
        f"\nSummary — PASS:{counts.get(PASS,0)}  WARN:{counts.get(WARN,0)}  FAIL:{counts.get(FAIL,0)}  MISSING:{counts.get(MISS,0)}"
    )


if __name__ == "__main__":
    main()
