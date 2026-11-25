from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import yaml

from acestor import PipelineConfig, PipelineContext, PipelineRunner
from pipelines.dengue_monitoring.pipeline import build_pipeline


def observations(errors, dates=None, **extra):
    dates = (
        dates
        if dates is not None
        else pd.date_range("2025-01-07", periods=len(errors), freq="7D")
    )
    return pd.DataFrame(
        [
            dict(
                scope="test",
                spatial_scale="ward",
                derivation="native",
                model="rf",
                lead=1,
                region_id="001",
                target_end=pd.Timestamp(date),
                signed_error=error,
                actual_cases=10,
                status="evaluated",
                **extra,
            )
            for date, error in zip(dates, errors)
        ]
    )


def archive(
    path: Path,
    issue: str,
    targets: list[str],
    predicted=14,
    model="randomForestRegression",
):
    (path / "outputs").mkdir(parents=True)
    (path / "sampling_day.json").write_text(json.dumps({"run_date": issue}))
    (path / "cutoffs.json").write_text(
        json.dumps({"prediction_dates": targets, "cutoff": issue})
    )
    records = [
        dict(
            regionID="001",
            startDatePredictedWeek=date,
            model=model,
            predictionRaw=predicted,
            prediction=round(predicted),
            thresholdMethod=method,
        )
        for date in targets
        for method in ("historical", "weighted_baseline")
    ]
    pd.DataFrame(records).to_csv(path / "outputs/predictions.csv", index=False)


def fixture_config(tmp_path):
    prepared = tmp_path / "prepared/ward"
    prepared.mkdir(parents=True)
    pd.DataFrame(
        {
            "region_id": "001",
            "date": pd.date_range("2024-01-01", "2025-02-01"),
            "case_count": 1,
        }
    ).to_csv(prepared / "cases_daily.csv", index=False)
    fc = tmp_path / "forecast.yaml"
    fc.write_text(
        yaml.safe_dump(
            {
                "data": {
                    "prepared_data": {
                        "base_dir": str(prepared.parent),
                        "region_type": "ward",
                    }
                }
            }
        )
    )
    archive(tmp_path / "hist", "2024-01-01", ["2024-01-08"], predicted=7)
    archive(tmp_path / "now1", "2025-01-01", ["2025-01-08"])
    archive(tmp_path / "now2", "2025-01-08", ["2025-01-15"])
    return {
        "storages": {
            "artifacts": {
                "kind": "filesystem",
                "filesystem": {"base_path": str(tmp_path / "results")},
            }
        },
        "monitoring": {
            "as_of": "2025-02-01",
            "reuse_existing_runs": True,
            "refresh_actuals": False,
            "baseline": {"directory": str(tmp_path / "baselines"), "name": "test"},
            "scopes": [
                {
                    "name": "test",
                    "spatial_scale": "ward",
                    "models": ["rf"],
                    "forecast_config": str(fc),
                    "historical": {
                        "start": "2024-01-01",
                        "end": "2024-12-31",
                        "runs": [
                            {
                                "issue_date": "2024-01-01",
                                "run_dir": str(tmp_path / "hist"),
                            }
                        ],
                    },
                    "current_runs": [
                        {"issue_date": "2025-01-01", "run_dir": str(tmp_path / "now1")},
                        {"issue_date": "2025-01-08", "run_dir": str(tmp_path / "now2")},
                    ],
                }
            ],
        },
    }


def execute_test(raw, tmp_path, run_id):
    config = PipelineConfig(tmp_path / "monitor.yaml", raw)
    return PipelineRunner(
        dag=build_pipeline(config),
        context=PipelineContext.from_config(config, run_id=run_id),
    ).run()


def build_test_baseline(raw, tmp_path, run_id="calibration"):
    calibration = copy.deepcopy(raw)
    calibration["monitoring"]["mode"] = "build_baseline"
    result = execute_test(calibration, tmp_path, run_id)
    assert result.status == "success", result.failure_detail
    return json.loads(
        (tmp_path / "results" / run_id / "outputs/manifest.json").read_text()
    )["baseline_id"]
