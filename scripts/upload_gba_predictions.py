"""Upload GBA corp/zone/ward predictions to the dashboard.

Shim until acestor emits `prediction_min`/`prediction_max` in snake_case for
all levels. Rewrites CSVs pre-upload so ingest picks up min/max at every
level (backend only reads snake_case; corp/ward pipelines don't emit min/max
at all today).

Usage:

  python scripts/upload_gba_predictions.py \
      --reference-date 2026-07-21 \
      --admin-email admin@dengue.local --admin-password admin123

By default auto-picks the latest run under artifacts/gba_corp,
artifacts/gba_zone (native, i.e. pipeline == acestor.dengue) and
artifacts/gba_zone (downscale, i.e. pipeline == acestor.dengue_downscale).
Override any with --corp-run / --zone-run / --ward-run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

ART = Path.home() / "Desktop/ARTPARK/acestor-v2/artifacts"


def _latest_run(subdir: str, pipeline_grep: str | None) -> str:
    root = ART / subdir
    candidates = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in candidates:
        log = p / "run.log"
        if not log.exists():
            continue
        if pipeline_grep is None or pipeline_grep in log.read_text(errors="replace"):
            return p.name
    raise SystemExit(f"no run with pipeline={pipeline_grep!r} under {root}")


def _load_historical(run_dir: Path) -> pd.DataFrame:
    df = pd.read_csv(run_dir / "outputs" / "predictions.csv")
    return df[df["thresholdMethod"] == "historical"].copy()


def _rename_zone_minmax(zone_df: pd.DataFrame) -> pd.DataFrame:
    out = zone_df.rename(
        columns={"predictionMin": "prediction_min", "predictionMax": "prediction_max"}
    )
    if "prediction_min" not in out.columns:
        raise SystemExit("zone CSV missing predictionMin/predictionMax columns")
    return out


def _corp_child_zones() -> dict[str, list[str]]:
    return {
        "corp_gba-1": ["zone_gba-n-1", "zone_gba-n-2"],
        "corp_gba-2": ["zone_gba-w-1", "zone_gba-w-2"],
        "corp_gba-3": ["zone_gba-e-1", "zone_gba-e-2"],
        "corp_gba-4": ["zone_gba-s-1", "zone_gba-s-2"],
        "corp_gba-5": ["zone_gba-c-1", "zone_gba-c-2"],
    }


def _augment_corp(corp_df: pd.DataFrame, zone_df: pd.DataFrame) -> pd.DataFrame:
    mapping = _corp_child_zones()
    zone_by_key = zone_df.set_index(["regionID", "startDatePredictedWeek"])[
        ["prediction_min", "prediction_max"]
    ]
    mins, maxs = [], []
    for _, r in corp_df.iterrows():
        child_zones = mapping.get(r["regionID"], [])
        pmin = pmax = 0.0
        ok = True
        for zid in child_zones:
            try:
                zr = zone_by_key.loc[(zid, r["startDatePredictedWeek"])]
                pmin += float(zr["prediction_min"])
                pmax += float(zr["prediction_max"])
            except KeyError:
                ok = False
                break
        mins.append(pmin if ok else None)
        maxs.append(pmax if ok else None)
    out = corp_df.copy()
    out["prediction_min"] = mins
    out["prediction_max"] = maxs
    return out


def _augment_ward(ward_df: pd.DataFrame, zone_df: pd.DataFrame) -> pd.DataFrame:
    zone_by_key = zone_df.set_index(["regionID", "startDatePredictedWeek"])[
        ["prediction_min", "prediction_max", "prediction"]
    ]
    zone_geojsons = ART.parent / "gba_datasets" / "geojsons" / "geojsons_GBA" / "wards"
    ward_to_zone: dict[str, str] = {}
    for f in zone_geojsons.glob("*.geojson"):
        g = json.loads(f.read_text())
        for feat in g.get("features", []):
            p = feat["properties"]
            wid = p.get("region_id") or f.stem
            parent = p.get("parent") or p.get("parent_id")
            if parent:
                ward_to_zone[wid] = parent
    mins, maxs = [], []
    for _, r in ward_df.iterrows():
        wid = r["regionID"]
        zid = ward_to_zone.get(wid)
        if zid is None:
            mins.append(None)
            maxs.append(None)
            continue
        try:
            zr = zone_by_key.loc[(zid, r["startDatePredictedWeek"])]
        except KeyError:
            mins.append(None)
            maxs.append(None)
            continue
        zpred = float(zr["prediction"]) or 0.0
        wpred = float(r["prediction"]) or 0.0
        if zpred <= 0:
            mins.append(0.0)
            maxs.append(0.0)
        else:
            share = wpred / zpred
            mins.append(float(zr["prediction_min"]) * share)
            maxs.append(float(zr["prediction_max"]) * share)
    out = ward_df.copy()
    out["prediction_min"] = mins
    out["prediction_max"] = maxs
    return out


def _login(base: str, email: str, password: str) -> str:
    r = subprocess.run(
        [
            "curl",
            "-s",
            "-X",
            "POST",
            f"{base}/api/auth/login",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"email": email, "password": password}),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(r.stdout)["access_token"]


def _upload(
    base: str, token: str, path: Path, scope: str, disease: str, reference_date: str
) -> dict:
    r = subprocess.run(
        [
            "curl",
            "-s",
            "-X",
            "POST",
            f"{base}/api/admin/predictions/upload",
            "-H",
            f"Authorization: Bearer {token}",
            "-F",
            f"file=@{path}",
            "-F",
            f"scope_id={scope}",
            "-F",
            f"disease={disease}",
            "-F",
            f"reference_date={reference_date}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(r.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--reference-date", required=True, help="YYYY-MM-DD, e.g. 2026-07-21"
    )
    ap.add_argument(
        "--corp-run", help="run_id under artifacts/gba_corp (default: latest)"
    )
    ap.add_argument(
        "--zone-run",
        help="run_id under artifacts/gba_zone with native model (default: latest acestor.dengue)",
    )
    ap.add_argument(
        "--ward-run",
        help="run_id under artifacts/gba_zone with downscale (default: latest acestor.dengue_downscale)",
    )
    ap.add_argument("--scope", default="gulb_gba")
    ap.add_argument("--disease", default="Dengue")
    ap.add_argument("--base-url", default="https://apps.artpark.ai/disease-dashboard")
    ap.add_argument("--admin-email", required=True)
    ap.add_argument("--admin-password", required=True)
    ap.add_argument("--out-dir", default="/tmp", help="where to write augmented CSVs")
    ap.add_argument(
        "--dry-run", action="store_true", help="augment + write CSVs, skip upload"
    )
    args = ap.parse_args()

    corp_id = args.corp_run or _latest_run("gba_corp", None)
    zone_id = args.zone_run or _latest_run("gba_zone", "acestor.dengue:")
    ward_id = args.ward_run or _latest_run("gba_zone", "acestor.dengue_downscale")

    print(f"corp run: {corp_id}")
    print(f"zone run: {zone_id}")
    print(f"ward run: {ward_id}")

    corp_df = _load_historical(ART / "gba_corp" / corp_id)
    zone_df = _rename_zone_minmax(_load_historical(ART / "gba_zone" / zone_id))
    ward_df = _load_historical(ART / "gba_zone" / ward_id)

    corp_aug = _augment_corp(corp_df, zone_df)
    ward_aug = _augment_ward(ward_df, zone_df)

    out_dir = Path(args.out_dir)
    corp_out = out_dir / "gba_corp_upload.csv"
    zone_out = out_dir / "gba_zone_upload.csv"
    ward_out = out_dir / "gba_ward_upload.csv"
    corp_aug.to_csv(corp_out, index=False)
    zone_df.to_csv(zone_out, index=False)
    ward_aug.to_csv(ward_out, index=False)

    for label, df in [("corp", corp_aug), ("zone", zone_df), ("ward", ward_aug)]:
        n_total = len(df)
        n_min = int(df["prediction_min"].notna().sum())
        n_max = int(df["prediction_max"].notna().sum())
        print(f"  {label}: {n_total} rows, {n_min} with min, {n_max} with max")

    if args.dry_run:
        print("dry-run: not uploading")
        return 0

    token = _login(args.base_url, args.admin_email, args.admin_password)
    for label, path in [("corp", corp_out), ("zone", zone_out), ("ward", ward_out)]:
        resp = _upload(
            args.base_url, token, path, args.scope, args.disease, args.reference_date
        )
        print(f"upload {label}: {resp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
