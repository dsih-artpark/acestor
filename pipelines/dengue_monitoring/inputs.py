"""Adapters for acestor forecast archives and existing pipeline backtests."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from acestor import PipelineConfig

NATIVE_BUILDER = "pipelines.dengue.pipeline:build_pipeline"
MODEL_ALIASES = {
    "randomForestRegression": "rf",
    "xgboostRegression": "xgb",
    "timesFoundationModel": "timesfm",
    "negativeBinomialRegression": "nbr",
    "negativeBinomialRegressionRegularised": "nbr_regularised",
    "timeSeriesExtrapolation": "tse",
    "naivePersistence": "naive",
    "ensembleModel": "ensemble",
}


def load_prediction_file(scope: dict, path: Path, regions: list[str]) -> pd.DataFrame:
    """Read multiple forecast origins from a single native-column CSV.

    Consolidated files retain explicit issue_date and lead alongside acestor's
    count columns. Lead must never be inferred by numbering a filtered period.
    """
    data = pd.read_csv(path, dtype={"regionID": str})
    required = [
        "regionID",
        "model",
        "startDatePredictedWeek",
        "predictionRaw",
        "issue_date",
        "lead",
    ]
    if not set(required) <= set(data):
        raise ValueError(f"Consolidated predictions require columns {required}")
    data["model"] = data.model.replace(
        {**MODEL_ALIASES, **scope.get("model_aliases", {})}
    )
    data = data[data.model.isin(scope["models"])].copy()
    if data.empty or data[required].isna().any().any():
        raise ValueError(
            "Consolidated predictions contain no selected models or null required fields"
        )
    for column in ("startDatePredictedWeek", "issue_date"):
        data[column] = pd.to_datetime(data[column], errors="raise")
        if (
            data[column].dt.tz is not None
            or not data[column].eq(data[column].dt.normalize()).all()
        ):
            raise ValueError("Prediction dates must be timezone-free calendar dates")
    data["lead"] = pd.to_numeric(data.lead, errors="raise")
    if (
        not np.isfinite(data.lead).all()
        or (data.lead < 1).any()
        or (data.lead % 1 != 0).any()
    ):
        raise ValueError("Consolidated lead values must be positive integers")
    data["lead"] = data.lead.astype(int)
    data["predictionRaw"] = pd.to_numeric(data.predictionRaw, errors="raise")
    if not np.isfinite(data.predictionRaw).all():
        raise ValueError("Raw predictions must be finite")
    data["target_end"] = data.startDatePredictedWeek
    if scope.get("target_label", "end") == "start":
        data["target_end"] += pd.Timedelta(days=6)
    if (data.issue_date > data.target_end).any():
        raise ValueError("Forecast issue date is after its target window")
    anchor = data.target_end - pd.to_timedelta((data.lead - 1) * 7, unit="D")
    if (
        data.assign(anchor=anchor)
        .groupby(["issue_date", "model"])
        .anchor.nunique()
        .gt(1)
        .any()
    ):
        raise ValueError("Inconsistent lead/target windows within a forecast origin")
    keys = ["regionID", "model", "issue_date", "lead", "target_end"]
    if data.groupby(keys).predictionRaw.nunique().gt(1).any():
        raise ValueError("Conflicting duplicate consolidated predictions")
    data = data.drop_duplicates(keys)
    data = data[data.lead.isin(scope.get("leads", [1, 2, 3, 4]))]
    if (not scope.get("region_ids") or scope.get("reject_unknown_regions")) and set(
        data.regionID
    ) - set(regions):
        raise ValueError(
            f"Forecast regions outside evaluation cohort: {sorted(set(data.regionID) - set(regions))}; explicitly select region_ids or rebuild the baseline"
        )
    # Retain missing predictions for known origin/target combinations.
    windows = data[["issue_date", "lead", "target_end"]].drop_duplicates()
    cohort = pd.MultiIndex.from_product(
        [regions, scope["models"]], names=["regionID", "model"]
    ).to_frame(index=False)
    out = windows.merge(cohort, how="cross").merge(
        data[keys + ["predictionRaw"]], on=keys, how="left", validate="one_to_one"
    )
    out = out.rename(
        columns={"regionID": "region_id", "predictionRaw": "predicted_cases"}
    )
    out["target_start"] = out.target_end - pd.Timedelta(days=6)
    out["scope"] = scope["name"]
    out["spatial_scale"] = scope["spatial_scale"]
    out["derivation"] = scope.get("derivation", "native")
    out["prediction_file"] = str(path.resolve())
    return out


@lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """Include working-tree code and locked dependencies in backtest cache keys."""
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root / "acestor").rglob("*.py")) + sorted(
        (root / "pipelines").rglob("*.py")
    )
    paths += [root / "uv.lock"]
    return hashlib.sha256(
        "".join(
            str(p.relative_to(root)) + digest(p) for p in paths if p.is_file()
        ).encode()
    ).hexdigest()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def merge(base: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def invoke(builder: str, raw: dict, run_id: str) -> None:
    """Isolate pipeline globals and reuse the public CLI; never send emails."""
    raw = copy.deepcopy(raw)
    raw["email"] = {"enabled": False}
    with tempfile.TemporaryDirectory(prefix="acestor-monitoring-") as temp:
        config = Path(temp) / "config.yaml"
        config.write_text(yaml.safe_dump(raw), encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "acestor.run",
                "--pipeline",
                builder,
                "--config",
                str(config),
                "--run-id",
                run_id,
            ],
            check=True,
        )


def refresh_cases(prep_config: str, artifact_root: Path) -> set[str]:
    from pipelines.dengue_prep.configs import (
        PrepCaseDownloadConfig,
        PrepCaseParseConfig,
        PrepOutputConfig,
    )

    raw = dict(PipelineConfig.from_yaml(prep_config).raw)
    data = raw.get("data", {})
    if not PrepCaseDownloadConfig.from_raw(data.get("case_download", {})).enabled:
        raise ValueError(
            "Requested actuals refresh but data.case_download.enabled is false"
        )
    parse = dict(data.get("case_parse", {}))
    if data.get("region_type") and not parse.get("region_types"):
        parse["region_types"] = [data["region_type"]]
    scales = PrepCaseParseConfig.from_raw(parse).region_types
    base = Path(PrepOutputConfig.from_raw(data.get("prepared_data", {})).base_dir)
    paths = {scale: base / scale / "cases_daily.csv" for scale in scales}
    before = {
        scale: path.stat() if path.exists() else None for scale, path in paths.items()
    }
    raw["storages"] = {
        "artifacts": {
            "kind": "filesystem",
            "filesystem": {"base_path": str(artifact_root)},
        }
    }
    invoke(
        "pipelines.dengue_monitoring.pipeline:build_case_refresh_pipeline",
        raw,
        "case-refresh-" + uuid.uuid4().hex,
    )

    for scale, path in paths.items():
        after = path.stat() if path.is_file() else None
        previous = before[scale]
        if after is None or (
            previous is not None
            and (after.st_mtime_ns, after.st_ino, after.st_size)
            == (previous.st_mtime_ns, previous.st_ino, previous.st_size)
        ):
            raise ValueError(
                f"Case refresh did not rewrite prepared actuals for {scale}: {path}"
            )
    return set(paths)


def resolve_historical_run(
    scope: dict, spec: dict, raw: dict, cache: Path, reuse: bool
) -> Path:
    selected = Path(spec["run_dir"]) if spec.get("run_dir") else None
    if reuse and selected is not None and selected.is_dir():
        return selected
    issue_date = str(pd.Timestamp(spec["issue_date"]).date())
    builder = scope.get("forecast_builder", NATIVE_BUILDER)
    cfg = merge(copy.deepcopy(raw), spec.get("overrides", {}))
    cfg.setdefault("run", {})["run_date"] = issue_date
    source_dir = None
    derived_cases = None
    derived_section = None
    if builder != NATIVE_BUILDER:
        source = cfg.get("run", {}).get("source_run_id")
        if not source or source == "latest":
            raise ValueError(
                "Derived backtests require an explicit historical run.source_run_id; never use latest"
            )
        derived_section = (
            "rollup"
            if "dengue_rollup" in builder
            else "downscale" if "dengue_downscale" in builder else None
        )
        if derived_section is None:
            raise ValueError(
                "Supported backtest builders are dengue, dengue_rollup and dengue_downscale"
            )
        storage = cfg.get("storages", {}).get("artifacts", {})
        if storage.get("kind", "filesystem") != "filesystem":
            raise ValueError(
                "Derived backtest sources currently require filesystem storage"
            )
        source_base = Path(storage.get("filesystem", {}).get("base_path", "artifacts"))
        if derived_section == "rollup":
            source_base = Path(
                cfg["run"].get("source_artifacts_dir") or source_base.parent
            )
        source_dir = (source_base / source).resolve()
        # Preserve the source when the derived builder's output base is isolated.
        cfg["run"]["source_run_id"] = str(source_dir)
        derived_cases = Path(cfg[derived_section]["cases_csv"])
    # Keep scientific settings unchanged. Isolate report/map artifacts and disable delivery.
    cfg["email"] = {"enabled": False}
    cfg.setdefault("maps", {})["enable"] = False
    prepared = cfg.get("data", {}).get("prepared_data", {})
    inputs = {}
    geojson_base = cfg.get("data", {}).get("geojson", {}).get("base_path")
    if derived_section:
        geojson_base = cfg[derived_section].get("geojson_base_path", geojson_base)
    if geojson_base:
        for path in sorted(Path(geojson_base).rglob("*.geojson")):
            inputs[str(path.resolve())] = digest(path)
    if source_dir is not None:
        source_prediction = source_dir / "outputs/predictions.csv"
        inputs[str(source_prediction)] = digest(source_prediction)
        inputs[str(derived_cases.resolve())] = digest(derived_cases)
    for name in ("cases_daily.csv", "weather_daily.csv"):
        path = (
            Path(prepared.get("base_dir", "prepared_data"))
            / prepared.get("region_type", scope["spatial_scale"])
            / name
        )
        if path.exists():
            inputs[str(path.resolve())] = digest(path)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "builder": builder,
                "config": cfg,
                "inputs": inputs,
                "code": code_fingerprint(),
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    run_id = "backtest-" + issue_date + "-" + fingerprint[:16]
    directory = cache / run_id
    marker = directory / "monitoring_success.json"
    if (
        reuse
        and marker.exists()
        and json.loads(marker.read_text()).get("fingerprint") == fingerprint
    ):
        return directory
    # Never overwrite an earlier forecast, even when regenerating or retrying.
    if directory.exists() or not reuse:
        run_id += "-" + uuid.uuid4().hex[:8]
        directory = cache / run_id
    cfg.setdefault("storages", {})["artifacts"] = {
        "kind": "filesystem",
        "filesystem": {"base_path": str(cache)},
    }
    cfg["storages"].pop("runs", None)
    cfg.setdefault("report", {})["output_dir"] = str(directory / "report")
    if derived_cases is not None:
        # Existing derived pipelines use all supplied cases for spatial weights
        # and thresholds. Supply an isolated, origin-clamped copy for backtests.
        cases = pd.read_csv(derived_cases, dtype={"region_id": str})
        cases = cases[pd.to_datetime(cases.date) <= pd.Timestamp(issue_date)]
        if cases.empty:
            raise ValueError("No derived case history at the historical issue date")
        directory.mkdir(parents=True, exist_ok=True)
        cutoff_cases = directory / "historical_cases_daily.csv"
        cases.to_csv(cutoff_cases, index=False)
        cfg[derived_section]["cases_csv"] = str(cutoff_cases)
    invoke(builder, cfg, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    if source_dir is not None:
        for name in ("sampling_day.json", "cutoffs.json"):
            if (source_dir / name).exists():
                (directory / name).write_text(
                    (source_dir / name).read_text(), encoding="utf-8"
                )
    (directory / "monitoring_success.json").write_text(
        json.dumps(
            {"fingerprint": fingerprint, "issue_date": issue_date, "builder": builder}
        ),
        encoding="utf-8",
    )
    return directory


def load_forecasts(
    scope: dict, spec: dict, directory: Path, regions: list[str]
) -> pd.DataFrame:
    """Read selected acestor outputs; lead is position in saved prediction_dates.

    recordDate represents a trailing seven-day total in the native pipeline,
    despite the legacy CSV name startDatePredictedWeek. Derived runs must supply
    metadata_dir pointing to their parent forecast, or explicit prediction_dates.
    """
    metadata = Path(spec.get("metadata_dir", directory))
    run_status = directory / "run.json"
    if (
        run_status.exists()
        and json.loads(run_status.read_text()).get("status") != "success"
    ):
        raise ValueError(f"Selected forecast run did not succeed: {directory}")
    cutoffs_path = metadata / "cutoffs.json"
    cuts = json.loads(cutoffs_path.read_text()) if cutoffs_path.exists() else {}
    dates = spec.get("prediction_dates", cuts.get("prediction_dates"))
    if not dates:
        raise ValueError(
            f"No prediction_dates for {directory}; supply source metadata_dir or explicit dates"
        )
    dates = [pd.Timestamp(d).normalize() for d in dates]
    if any(pd.isna(d) for d in dates):
        raise ValueError("Invalid prediction_dates")
    if (
        len(set(dates)) != len(dates)
        or dates != sorted(dates)
        or any(b - a != pd.Timedelta(days=7) for a, b in zip(dates, dates[1:]))
    ):
        raise ValueError("prediction_dates must be ordered, unique, seven days apart")
    issue = pd.Timestamp(spec["issue_date"]).normalize()
    sampling = metadata / "sampling_day.json"
    if (
        sampling.exists()
        and pd.Timestamp(json.loads(sampling.read_text())["run_date"]) != issue
    ):
        raise ValueError(
            f"Selected run issue_date does not match sampling_day.json: {directory}"
        )
    if cuts.get("cutoff") and pd.Timestamp(cuts["cutoff"]) > issue:
        raise ValueError(
            "Historical forecast cutoff is after its configured issue date"
        )
    if cuts.get("cutoff") and any(d <= pd.Timestamp(cuts["cutoff"]) for d in dates):
        raise ValueError("Prediction targets must follow the training cutoff")
    explicit = spec.get("prediction_files", {})
    paths = [directory / "outputs" / "predictions.csv"]
    paths += sorted((directory / "outputs" / "per_model").glob("predictions_*.csv"))
    frames = []
    sources = (
        [(p, None) for p in paths if p.exists()]
        if not explicit
        else [(directory / p, model) for model, p in explicit.items()]
    )
    for path, supplied_model in sources:
        df = pd.read_csv(path, dtype={"regionID": str})
        required = {"regionID", "startDatePredictedWeek", "predictionRaw"}
        if not required <= set(df.columns):
            raise ValueError(
                f"{path}: required columns {sorted(required)}; raw predictions are required"
            )
        if supplied_model is not None:
            df["model"] = supplied_model
        elif "model" not in df:
            raise ValueError(
                f"{path}: model column missing; provide prediction_files mapping"
            )
        df["model"] = df.model.replace(
            {**MODEL_ALIASES, **scope.get("model_aliases", {})}
        )
        df = df[df.model.isin(scope["models"])].copy()
        if df.empty:
            continue
        if (
            df[["regionID", "startDatePredictedWeek", "predictionRaw"]]
            .isna()
            .any()
            .any()
        ):
            raise ValueError(f"Null forecast identifiers/counts in {path}")
        df["target_label"] = pd.to_datetime(df.startDatePredictedWeek).dt.normalize()
        if not df.target_label.isin(dates).all():
            raise ValueError(f"Forecast dates disagree with metadata in {path}")
        df["predictionRaw"] = pd.to_numeric(df.predictionRaw, errors="raise")
        if not np.isfinite(df.predictionRaw).all():
            raise ValueError(f"Invalid predicted case counts in {path}")
        frames.append(df[["regionID", "model", "target_label", "predictionRaw"]])
    if not frames:
        raise ValueError(f"No selected model forecasts found in {directory}")
    predictions = pd.concat(frames, ignore_index=True)
    keys = ["regionID", "model", "target_label"]
    if (predictions.groupby(keys).predictionRaw.nunique() > 1).any():
        raise ValueError(
            "Conflicting repeated predictions across files/threshold methods"
        )
    predictions = predictions.drop_duplicates(keys)
    missing_models = set(scope["models"]) - set(predictions.model)
    if missing_models:
        raise ValueError(f"Missing requested model outputs: {sorted(missing_models)}")
    if not regions:
        regions = sorted(predictions.regionID.unique())
    if (not scope.get("region_ids") or scope.get("reject_unknown_regions")) and set(
        predictions.regionID
    ) - set(regions):
        raise ValueError(
            f"Forecast regions outside evaluation cohort: {sorted(set(predictions.regionID) - set(regions))}; explicitly select region_ids or rebuild the baseline"
        )
    grid = pd.MultiIndex.from_product(
        [regions, scope["models"], dates], names=keys
    ).to_frame(index=False)
    out = grid.merge(predictions, on=keys, how="left", validate="one_to_one")
    out = out.rename(
        columns={"regionID": "region_id", "predictionRaw": "predicted_cases"}
    )
    out["lead"] = out.target_label.map({date: i + 1 for i, date in enumerate(dates)})
    out = out[out.lead.isin(scope.get("leads", [1, 2, 3, 4]))].copy()
    if out.empty:
        raise ValueError("No configured leads available in selected forecast")
    out["target_end"] = out.target_label
    if scope.get("target_label", "end") == "start":
        out["target_end"] += pd.Timedelta(days=6)
    out["target_start"] = out.target_end - pd.Timedelta(days=6)
    out["issue_date"] = issue
    out["run_dir"] = str(directory.resolve())
    out["scope"] = scope["name"]
    out["spatial_scale"] = scope["spatial_scale"]
    out["derivation"] = scope.get("derivation", "native")
    return out.drop(columns="target_label")
