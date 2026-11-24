"""Explicit historical calibration and routine monitoring against frozen baselines."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from acestor import (
    BaseStep,
    NoInputs,
    PipelineConfig,
    PipelineContext,
    PipelineDAG,
    PipelineStep,
)
from . import baselines
from .inputs import (
    NATIVE_BUILDER,
    digest,
    load_forecasts,
    load_prediction_file,
    refresh_cases,
    resolve_historical_run,
)
from .metrics import SERIES, baseline, compare, score
from .actuals import load_actuals, validate_actuals_config
from .report import render


def actuals_settings(scope: dict, role: str) -> dict:
    return scope.get(role, {}).get("actuals", scope.get("actuals", {}))


def should_refresh(cfg: dict, scope: dict, role: str) -> bool:
    # Legacy shared refresh_actuals is interpreted as CURRENT-only.
    default = cfg.get("refresh_actuals", True) if role == "current" else False
    value = scope.get(role, {}).get("refresh_actuals", default)
    if not isinstance(value, bool):
        raise ValueError(f"{role}.refresh_actuals must be a YAML boolean")
    return value


def period_dates(period: dict, label: str) -> None:
    if any(pd.isna(pd.Timestamp(period.get(k))) for k in ("start", "end")):
        raise ValueError(f"{label} requires start/end dates")
    if pd.Timestamp(period["start"]) > pd.Timestamp(period["end"]):
        raise ValueError(f"{label} start must not exceed end")


def validate(raw: dict) -> dict:
    cfg = raw.get("monitoring", {})
    mode = cfg.get("mode", "monitor")
    if mode not in ("monitor", "build_baseline"):
        raise ValueError("monitoring.mode must be monitor or build_baseline")
    baselines.settings(cfg)
    if not cfg.get("scopes"):
        raise ValueError("monitoring.scopes must be a non-empty list")
    for key in ("reuse_existing_runs", "refresh_actuals"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise ValueError(f"{key} must be a YAML boolean")
    if pd.isna(pd.Timestamp(cfg.get("as_of"))):
        raise ValueError("as_of must be a date")
    names = set()
    for scope in cfg["scopes"]:
        name = scope["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in names:
            raise ValueError(
                "Scope names must be unique and contain only letters, numbers, _ or -"
            )
        names.add(name)
        if (
            not scope.get("spatial_scale")
            or not isinstance(scope.get("models"), list)
            or not scope["models"]
            or len(set(scope["models"])) != len(scope["models"])
        ):
            raise ValueError("Each scope needs a spatial_scale and unique model names")
        if scope.get("target_label", "end") not in ("start", "end"):
            raise ValueError("target_label must be start or end")
        leads = scope.get("leads", [1, 2, 3, 4])
        if (
            not leads
            or any(type(lead) is not int or lead < 1 for lead in leads)
            or len(set(leads)) != len(leads)
        ):
            raise ValueError("leads must be unique positive integers")
        # Routine monitoring deliberately does not validate/open historical
        # source paths, forecast settings or actuals: the bundle is authoritative.
        role = "historical" if mode == "build_baseline" else "current"
        period = scope.get(role, {})
        actuals = actuals_settings(scope, role)
        frequency = validate_actuals_config(actuals)
        if should_refresh(cfg, scope, role):
            if frequency != "daily" or actuals.get("path"):
                raise ValueError(
                    f"{role}.refresh_actuals refreshes prepared daily data only; set false for external actuals files"
                )
            if not period.get("prep_config", scope.get("prep_config")):
                raise ValueError(f"{role}.refresh_actuals requires prep_config")
        runs = (
            period.get("runs", [])
            if role == "historical"
            else scope.get("current_runs", [])
        )
        if role == "historical" or period:
            period_dates(period, role)
        if role == "historical" and pd.Timestamp(period["end"]) > pd.Timestamp(
            cfg["as_of"]
        ):
            raise ValueError("Historical reference must end on or before as_of")
        if period.get("predictions_file") and (runs or period.get("issue_start")):
            raise ValueError(f"Choose {role}.predictions_file or runs, not both")
        if period.get("predictions_file"):
            if not Path(period["predictions_file"]).is_file():
                raise FileNotFoundError(period["predictions_file"])
        elif not runs and not (
            role == "historical"
            and period.get("issue_start")
            and period.get("issue_end")
        ):
            raise ValueError(f"Supply {role} predictions_file or runs")
        if role == "current" and scope.get("historical") and period:
            historical = scope["historical"]
            if historical.get("end") and pd.Timestamp(period["start"]) <= pd.Timestamp(
                historical["end"]
            ):
                raise ValueError(
                    "Current period must start after historical period ends"
                )
        for spec in runs:
            if not spec.get("issue_date") or pd.isna(pd.Timestamp(spec["issue_date"])):
                raise ValueError("Every selected run requires an explicit issue_date")
            if role == "current" and (
                not spec.get("run_dir") or not Path(spec["run_dir"]).is_dir()
            ):
                raise FileNotFoundError(
                    f"Selected current run does not exist: {spec.get('run_dir')}"
                )
    return cfg


def scope_contract(scope: dict) -> dict:
    return {
        "spatial_scale": scope["spatial_scale"],
        "derivation": scope.get("derivation", "native"),
        "models": sorted(scope["models"]),
        "leads": sorted(scope.get("leads", [1, 2, 3, 4])),
        "target_label": scope.get("target_label", "end"),
        "model_aliases": scope.get("model_aliases", {}),
    }


def check_contract(cfg: dict, contract: dict) -> None:
    frozen = contract["scopes"]
    if set(frozen) != {scope["name"] for scope in cfg["scopes"]}:
        raise ValueError(
            "Configured scopes differ from frozen baseline; explicitly build a compatible baseline"
        )
    for scope in cfg["scopes"]:
        saved = frozen[scope["name"]]
        if any(saved[key] != value for key, value in scope_contract(scope).items()):
            raise ValueError(
                "Scope/model/lead/window settings differ from frozen baseline; rebuild explicitly"
            )
        if (
            scope.get("region_ids") is not None
            and sorted(map(str, scope["region_ids"])) != saved["region_ids"]
        ):
            raise ValueError(
                "Region cohort differs from frozen baseline; rebuild explicitly"
            )
        historical = scope.get("historical", {})
        for key in ("start", "end"):
            if (
                key in historical
                and str(pd.Timestamp(historical[key]).date())
                != saved["historical"][key]
            ):
                raise ValueError(
                    "Historical period differs from frozen baseline; rebuild explicitly"
                )
        current = scope.get("current")
        if current and pd.Timestamp(current["start"]) <= pd.Timestamp(
            saved["historical"]["end"]
        ):
            raise ValueError(
                "Current period must start after the frozen historical period"
            )


def read_archive(scope, spec, forecast, cache, cfg, regions, is_history):
    if (
        is_history
        and not forecast
        and (
            not cfg.get("reuse_existing_runs", True)
            or not spec.get("run_dir")
            or not Path(spec["run_dir"]).is_dir()
        )
    ):
        raise ValueError("Historical regeneration requires forecast_config")
    directory = (
        resolve_historical_run(
            scope, spec, forecast, cache, cfg.get("reuse_existing_runs", True)
        )
        if is_history
        else Path(spec["run_dir"])
    )
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    predictions = load_forecasts(scope, spec, directory, regions)
    paths = sorted((directory / "outputs").rglob("predictions*.csv"))
    if spec.get("prediction_files"):
        paths = [directory / path for path in spec["prediction_files"].values()]
    return directory, predictions, paths


def collect(context, cfg, role, frozen=None):
    parts, provenance, snapshots, contracts = [], [], {}, {}
    refreshed = {}
    cache = Path(cfg.get("backtest_dir", "artifacts/monitoring_backtests")).resolve()
    for original in cfg["scopes"]:
        scope = dict(original)
        if frozen is not None:
            scope["region_ids"] = frozen["scopes"][scope["name"]]["region_ids"]
            scope["reject_unknown_regions"] = original.get("region_ids") is None
        period = scope.get(role, {})
        actuals_config = actuals_settings(scope, role)
        refresh_actuals = should_refresh(cfg, scope, role)
        prep_path = period.get("prep_config", scope.get("prep_config"))
        forecast = {}
        # Only load forecasting settings when they are needed for preparation
        # path resolution or explicit historical backtest execution.
        if scope.get("forecast_config") and (
            (role == "historical" and not period.get("predictions_file"))
            or (not actuals_config.get("path") and not prep_path)
        ):
            forecast = dict(PipelineConfig.from_yaml(scope["forecast_config"]).raw)
            if scope.get("forecast_builder", NATIVE_BUILDER) == NATIVE_BUILDER:
                for scale in (
                    forecast.get("model", {}).get("spatial_res"),
                    forecast.get("data", {})
                    .get("prepared_data", {})
                    .get("region_type"),
                ):
                    if scale and scale != scope["spatial_scale"]:
                        raise ValueError(
                            "Monitoring spatial_scale differs from native forecast configuration"
                        )
        if refresh_actuals and prep_path not in refreshed:
            context.log.info(
                "monitoring: refreshing %s cases using %s", role, prep_path
            )
            refreshed[prep_path] = refresh_cases(prep_path, cache / "prep")
        if refresh_actuals and scope["spatial_scale"] not in refreshed[prep_path]:
            raise ValueError(
                f"Case refresh did not produce requested scale: {scope['spatial_scale']}"
            )
        data = forecast.get("data", {}).get("prepared_data", {})
        if prep_path and not actuals_config.get("path"):
            prep = PipelineConfig.from_yaml(prep_path).raw
            data = prep.get("data", {}).get("prepared_data", data)
        default_path = (
            Path(data.get("base_dir", "prepared_data"))
            / scope["spatial_scale"]
            / "cases_daily.csv"
        )
        actuals, cases_path, frequency = load_actuals(actuals_config, default_path)
        regions = sorted(
            str(region)
            for region in scope.get("region_ids", actuals.region_id.unique())
        )
        if not regions or len(set(regions)) != len(regions):
            raise ValueError("Region cohort must be nonempty and unique")
        snapshot = actuals.to_csv(index=False).encode()
        relative = f"inputs/{scope['name']}/{role}/cases_{frequency}.csv"
        context.artifacts.write(snapshot, context.artifact_path(relative))
        if role == "historical":
            snapshots[relative] = snapshot
            contracts[scope["name"]] = {
                **scope_contract(scope),
                "region_ids": regions,
                "historical": {
                    key: str(pd.Timestamp(period[key]).date())
                    for key in ("start", "end")
                },
            }
        if period.get("predictions_file"):
            specs = [{"predictions_file": period["predictions_file"]}]
        elif role == "historical":
            specs = period.get("runs") or [
                {"issue_date": str(day.date())}
                for day in pd.date_range(
                    period["issue_start"], period["issue_end"], freq="7D"
                )
            ]
        else:
            specs = scope["current_runs"]
        scope_parts = []
        for spec in specs:
            if spec.get("predictions_file"):
                directory = None
                paths = [Path(spec["predictions_file"])]
                predictions = load_prediction_file(scope, paths[0], regions)
            else:
                directory, predictions, paths = read_archive(
                    scope, spec, forecast, cache, cfg, regions, role == "historical"
                )
            if period:
                predictions = predictions[
                    predictions.target_end.between(
                        pd.Timestamp(period["start"]), pd.Timestamp(period["end"])
                    )
                ]
            if role == "current":
                historical_end = pd.Timestamp(
                    frozen["scopes"][scope["name"]]["historical"]["end"]
                )
                if (predictions.target_end <= historical_end).any():
                    raise ValueError(
                        "Current target windows overlap the frozen historical reference"
                    )
            if predictions.empty:
                continue
            scope_parts.append(
                score(predictions, actuals, str(cfg["as_of"]), frequency=frequency)
            )
            provenance.append(
                {
                    "scope": scope["name"],
                    "role": role,
                    "actuals_frequency": frequency,
                    "actuals_path": str(cases_path.resolve()),
                    "actuals_mapping": actuals_config,
                    "actuals_sha256": baselines.sha(snapshot),
                    "actuals_snapshot": relative,
                    "actuals_refreshed": refresh_actuals,
                    "period": (
                        {key: str(period[key]) for key in ("start", "end")}
                        if period
                        else None
                    ),
                    "run_dir": str(directory.resolve()) if directory else None,
                    "issue_date": (
                        str(spec["issue_date"]) if spec.get("issue_date") else None
                    ),
                    "prediction_files": {
                        str(path.resolve()): digest(path) for path in paths
                    },
                }
            )
        if not scope_parts:
            raise ValueError(
                f"{scope['name']}: no {role} observations in selected period"
            )
        parts.extend(scope_parts)
    table = (
        pd.concat(parts, ignore_index=True)
        .sort_values(SERIES + ["target_end"])
        .reset_index(drop=True)
    )
    if table.duplicated(SERIES + ["target_end"]).any():
        raise ValueError(
            "Duplicate selected region/model/lead/target observations; select one forecast"
        )
    return table, provenance, snapshots, {"method_version": 1, "scopes": contracts}


def read_table(content: bytes) -> pd.DataFrame:
    table = pd.read_csv(
        io.BytesIO(content), dtype={"region_id": str}, float_precision="round_trip"
    )
    for column in ("target_start", "target_end", "issue_date"):
        if column in table:
            table[column] = pd.to_datetime(table[column])
    return table


@dataclass(frozen=True)
class MonitoringResult:
    report_path: str
    baseline_id: str
    evaluated_rows: int
    alerts: int


class MonitoringStep(BaseStep[NoInputs, MonitoringResult]):
    input_type = NoInputs

    def preflight_run(self, config: PipelineConfig, run_id: str, clean: bool = False):
        baselines.preflight(dict(config.raw), run_id, clean)

    def run(self, context: PipelineContext, inputs: NoInputs) -> MonitoringResult:
        cfg = validate(dict(context.config))
        # Check/load baseline before fetching anything. Historical source paths
        # are never read on this route, even if their contents have changed.
        bundle = None
        if cfg.get("mode", "monitor") == "monitor":
            bundle = baselines.load(cfg)
            check_contract(cfg, bundle[1]["contract"])
        execution = baselines.claim(dict(context.config), context.run_id)
        context.write_artifact_json("execution.json", execution)
        if cfg.get("mode", "monitor") == "build_baseline":
            historical, provenance, snapshots, contract = collect(
                context, cfg, "historical"
            )
            reference = baseline(historical)
            files = {
                **snapshots,
                "historical_errors.csv": historical.to_csv(index=False).encode(),
                "baseline.csv": reference.to_csv(index=False).encode(),
            }
            baseline_id = baselines.save(cfg, contract, provenance, files)
            historical["baseline_id"] = baseline_id
            reference["baseline_id"] = baseline_id
            for name, table in (
                ("historical_errors", historical),
                ("baseline", reference),
            ):
                context.artifacts.write_text(
                    table.to_csv(index=False),
                    context.artifact_path(f"outputs/{name}.csv"),
                )
            manifest = {
                **execution,
                "baseline_id": baseline_id,
                "inputs": provenance,
                "as_of": str(cfg["as_of"]),
                "schema_version": 2,
            }
            manifest["run_sha256"] = baselines.sha(baselines.canonical(manifest))
            context.write_artifact_json("outputs/manifest.json", manifest)
            baselines.activate(cfg, baseline_id)
            return MonitoringResult("", baseline_id, len(historical), 0)
        baseline_id, payload, files = bundle
        reference = read_table(files["baseline.csv"])
        reference["baseline_id"] = baseline_id
        current, provenance, _, _ = collect(
            context, cfg, "current", payload["contract"]
        )
        return write_results(
            context,
            cfg,
            current,
            reference,
            baseline_id,
            provenance,
            execution,
            payload,
        )


def write_results(
    context,
    cfg,
    current,
    reference,
    baseline_id,
    provenance,
    execution,
    payload,
):
    evaluated = compare(current, reference)
    evaluated["baseline_id"] = baseline_id
    evaluated["run_id"] = context.run_id
    evaluated["execution_id"] = execution["execution_id"]
    tables = {
        "baseline": reference,
        "current_errors": evaluated,
        "flags": evaluated[evaluated.upper_flag | evaluated.underprediction_flag],
        "alerts": evaluated[evaluated.alert],
        "exclusions": evaluated.query("status != 'evaluated'").assign(period="current"),
    }
    for name, table in tables.items():
        context.artifacts.write_text(
            table.to_csv(index=False), context.artifact_path(f"outputs/{name}.csv")
        )
    manifest = {
        **execution,
        "schema_version": 2,
        "baseline_id": baseline_id,
        "baseline_directory": str(baselines.settings(cfg)[0] / baseline_id),
        "as_of": str(cfg["as_of"]),
        "inputs": provenance,
        "historical_inputs": payload["inputs"],
        "rules": {
            "upper": "signed_error > p75",
            "alert": "2+ consecutive target weeks above p75",
            "underprediction": "signed_error < 0; flag only",
            "undefined": "break sequence",
            "quantiles": "nearest rank",
            "sd_ddof": 1,
        },
        "limitations": [
            "Actuals maturity unverified. Daily inputs zero-fill internal absent days; weekly inputs match exact windows.",
            "Historical calibration is frozen; only explicit build_baseline runs can publish another version.",
        ],
    }
    manifest["evaluation_sha256"] = baselines.sha(
        baselines.canonical(
            {
                "baseline_id": baseline_id,
                "as_of": str(cfg["as_of"]),
                "inputs": provenance,
                "errors": current.to_csv(index=False),
            }
        )
    )
    manifest["run_sha256"] = baselines.sha(baselines.canonical(manifest))
    context.write_artifact_json("outputs/manifest.json", manifest)
    path = context.artifact_path("outputs/report.html")
    html = render(
        evaluated,
        reference,
        baseline_id,
        run_id=context.run_id,
        execution_id=execution["execution_id"],
    )
    context.artifacts.write_text(html, path)
    return MonitoringResult(
        path,
        baseline_id,
        int((evaluated.status == "evaluated").sum()),
        int(evaluated.alert.sum()),
    )


def build_pipeline(config: PipelineConfig) -> PipelineDAG:
    validate(dict(config.raw))
    return PipelineDAG.from_steps(
        [PipelineStep(name="monitor_forecasts", impl=MonitoringStep())]
    )


def build_case_refresh_pipeline(config: PipelineConfig) -> PipelineDAG:
    """Reuse existing case source/parser without unrelated weather downloads."""
    from pipelines.dengue_prep.steps.download_case_data import PrepDownloadCaseDataStep
    from pipelines.dengue_prep.steps.download_geojsons import PrepDownloadGeojsonsStep
    from pipelines.dengue_prep.steps.parse_case_data import PrepParseCaseDataStep

    geo = PipelineStep(name="download_geojsons", impl=PrepDownloadGeojsonsStep())
    cases = PipelineStep(name="download_case_data", impl=PrepDownloadCaseDataStep())
    parse = PipelineStep(name="parse_case_data", impl=PrepParseCaseDataStep())
    [geo, cases] >> parse
    return PipelineDAG.from_steps([geo, cases, parse])
