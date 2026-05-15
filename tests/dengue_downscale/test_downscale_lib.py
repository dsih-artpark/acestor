"""Unit tests for the dengue_downscale core library."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from pipelines.dengue_downscale.configs import DownscaleConfig, DownscaleRunConfig
from pipelines.dengue_downscale.lib.downscale import (
    _PREDICTION_COLS,
    build_parent_child_mapping,
    compute_shares,
    downscale_predictions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_geojson(region_id: str, parent: str) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"region_id": region_id, "parent": parent},
                "geometry": None,
            }
        ],
    }


def _write_geojsons(tmp_dir: Path, entries: list[tuple[str, str]]) -> Path:
    for child_id, parent_id in entries:
        path = tmp_dir / f"{child_id}.geojson"
        path.write_text(json.dumps(_make_geojson(child_id, parent_id)))
    return tmp_dir


def _make_cases_df(
    region_ids: list[str],
    weeks: int = 8,
    start: str = "2026-01-05",
    case_count: int = 10,
) -> pd.DataFrame:
    rows = []
    for date in pd.date_range(start, periods=weeks, freq="7D"):
        for rid in region_ids:
            rows.append({"region_id": rid, "date": date, "case_count": case_count})
    return pd.DataFrame(rows)


def _make_parent_preds(parent_ids: list[str], n_weeks: int = 4) -> pd.DataFrame:
    rows = []
    for pid in parent_ids:
        for i in range(n_weeks):
            rows.append(
                {
                    "dateOfComputingPrediction": "2026-03-01",
                    "startDatePredictedWeek": f"2026-03-{(i + 1) * 7:02d}",
                    "regionID": pid,
                    "prediction": 100.0,
                    "thresholdMethod": "historical",
                    "predictionZone": 2.0,
                    "model": "ensembleModel",
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# build_parent_child_mapping
# ---------------------------------------------------------------------------


def test_build_mapping_returns_correct_pairs(tmp_path):
    _write_geojsons(
        tmp_path, [("mandal_01", "district_A"), ("mandal_02", "district_A")]
    )
    mapping = build_parent_child_mapping(tmp_path)
    assert mapping == {"mandal_01": "district_A", "mandal_02": "district_A"}


def test_build_mapping_skips_missing_parent(tmp_path):
    (tmp_path / "no_parent.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "x"},
                        "geometry": None,
                    }
                ],
            }
        )
    )
    mapping = build_parent_child_mapping(tmp_path)
    assert "x" not in mapping


def test_build_mapping_skips_missing_region_id(tmp_path):
    (tmp_path / "no_region_id.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"parent": "d1"},
                        "geometry": None,
                    }
                ],
            }
        )
    )
    mapping = build_parent_child_mapping(tmp_path)
    assert mapping == {}


def test_build_mapping_empty_dir(tmp_path):
    assert build_parent_child_mapping(tmp_path) == {}


# ---------------------------------------------------------------------------
# compute_shares
# ---------------------------------------------------------------------------


def test_shares_sum_to_one():
    cases = _make_cases_df(["m1", "m2", "m3"])
    as_of = pd.Timestamp("2026-03-01")
    shares = compute_shares(
        cases, "district_A", ["m1", "m2", "m3"], as_of, window_weeks=4
    )
    assert pytest.approx(sum(shares.values()), abs=1e-10) == 1.0


def test_shares_proportional_to_case_counts():
    rows = []
    for date in pd.date_range("2026-01-05", periods=4, freq="7D"):
        rows.append({"region_id": "m1", "date": date, "case_count": 30})
        rows.append({"region_id": "m2", "date": date, "case_count": 70})
    cases = pd.DataFrame(rows)
    as_of = pd.Timestamp("2026-02-02")
    shares = compute_shares(cases, "district_A", ["m1", "m2"], as_of, window_weeks=4)
    assert pytest.approx(shares["m1"], abs=1e-6) == 0.3
    assert pytest.approx(shares["m2"], abs=1e-6) == 0.7


def test_shares_uniform_when_zero_cases():
    cases = _make_cases_df(["m1", "m2"], case_count=0)
    as_of = pd.Timestamp("2026-03-01")
    shares = compute_shares(cases, "district_A", ["m1", "m2"], as_of, window_weeks=4)
    assert pytest.approx(shares["m1"], abs=1e-10) == 0.5
    assert pytest.approx(shares["m2"], abs=1e-10) == 0.5


def test_shares_uses_window_not_all_history():
    rows = []
    # Old cases (outside window): m1 gets all
    for date in pd.date_range("2025-01-01", periods=4, freq="7D"):
        rows.append({"region_id": "m1", "date": date, "case_count": 100})
        rows.append({"region_id": "m2", "date": date, "case_count": 0})
    # Recent cases (inside window): m2 gets all
    for date in pd.date_range("2026-02-02", periods=4, freq="7D"):
        rows.append({"region_id": "m1", "date": date, "case_count": 0})
        rows.append({"region_id": "m2", "date": date, "case_count": 100})
    cases = pd.DataFrame(rows)
    as_of = pd.Timestamp("2026-03-02")
    shares = compute_shares(cases, "district_A", ["m1", "m2"], as_of, window_weeks=4)
    assert pytest.approx(shares["m2"], abs=1e-6) == 1.0


# ---------------------------------------------------------------------------
# downscale_predictions
# ---------------------------------------------------------------------------


def test_downscale_output_schema():
    with tempfile.TemporaryDirectory() as tmp:
        geojson_dir = Path(tmp)
        _write_geojsons(geojson_dir, [("m1", "d1"), ("m2", "d1")])
        mapping = build_parent_child_mapping(geojson_dir)
        cases = _make_cases_df(["m1", "m2"])
        preds = _make_parent_preds(["d1"])
        result = downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), window_weeks=4
        )
    assert list(result.columns) == _PREDICTION_COLS


def test_downscale_child_predictions_sum_to_parent():
    with tempfile.TemporaryDirectory() as tmp:
        geojson_dir = Path(tmp)
        _write_geojsons(geojson_dir, [("m1", "d1"), ("m2", "d1")])
        mapping = build_parent_child_mapping(geojson_dir)
        cases = _make_cases_df(["m1", "m2"])
        preds = _make_parent_preds(["d1"], n_weeks=1)
        result = downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), window_weeks=4
        )
    # child predictions for one parent week must sum to parent prediction (100.0)
    assert pytest.approx(result["prediction"].sum(), abs=1e-8) == 100.0


def test_downscale_regionids_are_child_ids():
    with tempfile.TemporaryDirectory() as tmp:
        geojson_dir = Path(tmp)
        _write_geojsons(geojson_dir, [("m1", "d1"), ("m2", "d1")])
        mapping = build_parent_child_mapping(geojson_dir)
        cases = _make_cases_df(["m1", "m2"])
        preds = _make_parent_preds(["d1"])
        result = downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), window_weeks=4
        )
    assert set(result["regionID"].unique()) == {"m1", "m2"}


def test_downscale_unknown_parent_rows_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        geojson_dir = Path(tmp)
        _write_geojsons(geojson_dir, [("m1", "d1")])
        mapping = build_parent_child_mapping(geojson_dir)
        cases = _make_cases_df(["m1"])
        # d2 is in predictions but has no children in geojson mapping
        preds = _make_parent_preds(["d1", "d2"], n_weeks=1)
        result = downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), window_weeks=4
        )
    # Only d1's children appear; d2 rows are dropped
    assert "d2" not in result["regionID"].values
    assert "m1" in result["regionID"].values


def test_downscale_preserves_metadata_columns():
    with tempfile.TemporaryDirectory() as tmp:
        geojson_dir = Path(tmp)
        _write_geojsons(geojson_dir, [("m1", "d1")])
        mapping = build_parent_child_mapping(geojson_dir)
        cases = _make_cases_df(["m1"])
        preds = _make_parent_preds(["d1"], n_weeks=1)
        result = downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), window_weeks=4
        )
    assert result["thresholdMethod"].iloc[0] == "historical"
    assert result["model"].iloc[0] == "ensembleModel"
    assert result["dateOfComputingPrediction"].iloc[0] == "2026-03-01"


def test_downscale_empty_parent_preds_returns_empty_df():
    with tempfile.TemporaryDirectory() as tmp:
        geojson_dir = Path(tmp)
        _write_geojsons(geojson_dir, [("m1", "d1")])
        mapping = build_parent_child_mapping(geojson_dir)
        cases = _make_cases_df(["m1"])
        empty_preds = pd.DataFrame(
            columns=[
                "dateOfComputingPrediction",
                "startDatePredictedWeek",
                "regionID",
                "prediction",
                "thresholdMethod",
                "predictionZone",
                "model",
            ]
        )
        result = downscale_predictions(
            empty_preds, mapping, cases, pd.Timestamp("2026-03-01"), window_weeks=4
        )
    assert list(result.columns) == _PREDICTION_COLS
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


def test_downscale_run_config_requires_source_run_id():
    with pytest.raises(ValueError, match="source_run_id"):
        DownscaleRunConfig.from_raw({})


def test_downscale_run_config_reads_source_run_id():
    cfg = DownscaleRunConfig.from_raw({"source_run_id": "march-01-run"})
    assert cfg.source_run_id == "march-01-run"


def test_downscale_config_requires_parent_level():
    with pytest.raises(ValueError, match="parent_level"):
        DownscaleConfig.from_raw({"child_level": "mandals"})


def test_downscale_config_requires_child_level():
    with pytest.raises(ValueError, match="child_level"):
        DownscaleConfig.from_raw({"parent_level": "districts"})


def test_downscale_config_defaults():
    cfg = DownscaleConfig.from_raw(
        {"parent_level": "districts", "child_level": "mandals"}
    )
    assert cfg.window_weeks == 4
    assert cfg.geojson_base_path == "ap_datasets/geojsons/geojsons_AP"


def test_downscale_config_custom_values():
    cfg = DownscaleConfig.from_raw(
        {
            "parent_level": "districts",
            "child_level": "mandals",
            "window_weeks": 8,
            "cases_csv": "prepared_data/mandal/cases_daily.csv",
            "geojson_base_path": "custom/path",
        }
    )
    assert cfg.window_weeks == 8
    assert cfg.cases_csv == "prepared_data/mandal/cases_daily.csv"
    assert cfg.geojson_base_path == "custom/path"


def test_downscale_config_rejects_zero_window_weeks():
    with pytest.raises(ValueError, match="window_weeks"):
        DownscaleConfig.from_raw(
            {"parent_level": "districts", "child_level": "mandals", "window_weeks": 0}
        )


def test_downscale_config_rejects_negative_window_weeks():
    with pytest.raises(ValueError, match="window_weeks"):
        DownscaleConfig.from_raw(
            {"parent_level": "districts", "child_level": "mandals", "window_weeks": -1}
        )


def test_downscale_config_rejects_same_parent_and_child_level():
    with pytest.raises(ValueError, match="differ"):
        DownscaleConfig.from_raw(
            {"parent_level": "districts", "child_level": "districts"}
        )
