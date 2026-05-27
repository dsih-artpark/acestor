from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from pipelines.dengue.lib.brief import (
    build_brief_context,
    compute_parent_lookup,
    load_child_geojson_combined,
)


def _sample_predictions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 4,
            "startDatePredictedWeek": [
                "2026-06-01",
                "2026-06-08",
                "2026-06-15",
                "2026-06-22",
            ],
            "regionID": ["district_511"] * 4,
            "prediction": [3.2, 2.7, 1.1, 0.8],
            "predictionZone": [2, 1, 1, 1],
            "thresholdMethod": ["historical"] * 4,
            "model": ["ensembleModel"] * 4,
            "Mean": [2.5] * 4,
            "StdDev": [1.1] * 4,
            "Zero": [0.0] * 4,
            "Inf": [float("inf")] * 4,
            "T0.00": [2.5] * 4,
            "T1.00": [3.6] * 4,
            "T2.00": [4.7] * 4,
            "recordDate": ["2026-05-25"] * 4,
            "ISOWeek": [22, 23, 24, 25],
        }
    )


def test_build_brief_context_returns_required_keys():
    ctx = build_brief_context(
        predictions=_sample_predictions(),
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="Test brief",
    )
    required = {
        "document_title",
        "is_downscale",
        "hero_chart_relpath",
        "weekly_blocks",
        "action_matrix",
        "run_date",
        "footer_meta",
    }
    missing = required - set(ctx)
    assert not missing, f"missing keys: {missing}"


def test_build_brief_context_no_risk_progression_key():
    ctx = build_brief_context(
        predictions=_sample_predictions(),
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="Test brief",
    )
    assert "risk_progression" not in ctx


def test_weekly_blocks_filters_medium_plus_zones():
    """Weekly blocks should list only regions with predictionZone >= 2."""
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 4,
            "startDatePredictedWeek": ["2026-06-01"] * 4,
            "regionID": ["r_high", "r_mid", "r_low", "r_zero"],
            "prediction": [10.0, 5.0, 1.0, 0.0],
            "predictionZone": [3, 2, 1, 0],
            "thresholdMethod": ["historical"] * 4,
            "model": ["ensembleModel"] * 4,
            "Mean": [1.0] * 4,
            "StdDev": [1.0] * 4,
            "Zero": [0.0] * 4,
            "Inf": [float("inf")] * 4,
            "T0.00": [1.0] * 4,
            "T1.00": [2.0] * 4,
            "T2.00": [3.0] * 4,
            "recordDate": ["2026-05-25"] * 4,
            "ISOWeek": [22] * 4,
        }
    )
    ctx = build_brief_context(
        predictions=df,
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="t",
    )
    blocks = ctx["weekly_blocks"]
    assert len(blocks) == 1
    # zone_groups: list of {label, band_class, regions[], region_entries[]} — only zone >= 2 grouped
    region_ids = set()
    for g in blocks[0]["zone_groups"]:
        region_ids.update(g["regions"])
    assert region_ids == {"r_high", "r_mid"}
    # region_entries carries id, name, parent
    entry_ids = set()
    for g in blocks[0]["zone_groups"]:
        for e in g["region_entries"]:
            assert "id" in e and "name" in e and "parent" in e
            entry_ids.add(e["id"])
    assert entry_ids == {"r_high", "r_mid"}


def test_weekly_blocks_grouped_by_band_high_first():
    """zone_groups ordered Very High → High → Medium; each group has label + band_class + regions."""
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 3,
            "startDatePredictedWeek": ["2026-06-01"] * 3,
            "regionID": ["r_vhigh", "r_high", "r_mid"],
            "prediction": [12.0, 10.0, 5.0],
            "predictionZone": [4, 3, 2],
            "thresholdMethod": ["historical"] * 3,
            "model": ["ensembleModel"] * 3,
        }
    )
    ctx = build_brief_context(
        predictions=df,
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="t",
    )
    groups = ctx["weekly_blocks"][0]["zone_groups"]
    assert [g["label"] for g in groups] == ["Very High", "High", "Medium"]
    assert [g["band_class"] for g in groups] == ["vhigh", "high", "med"]
    assert groups[0]["regions"] == ["r_vhigh"]
    assert groups[1]["regions"] == ["r_high"]
    assert groups[2]["regions"] == ["r_mid"]
    # region_entries mirrors regions
    assert groups[0]["region_entries"][0]["id"] == "r_vhigh"
    assert groups[1]["region_entries"][0]["id"] == "r_high"
    assert groups[2]["region_entries"][0]["id"] == "r_mid"


def test_region_names_lookup():
    """Passing region_names should populate regionName with title-cased name."""
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"],
            "startDatePredictedWeek": ["2026-06-01"],
            "regionID": ["district_511"],
            "prediction": [5.0],
            "predictionZone": [2],
            "thresholdMethod": ["historical"],
            "model": ["ensembleModel"],
            "Mean": [2.5],
            "StdDev": [1.1],
            "Zero": [0.0],
            "Inf": [float("inf")],
            "T0.00": [2.5],
            "T1.00": [3.6],
            "T2.00": [4.7],
            "recordDate": ["2026-05-25"],
            "ISOWeek": [22],
        }
    )
    ctx = build_brief_context(
        predictions=df,
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="t",
        region_names={"district_511": "Kurnool"},
    )
    groups = ctx["weekly_blocks"][0]["zone_groups"]
    # The single Medium-zone region appears under its zone group, looked up to display name.
    all_regions = [r for g in groups for r in g["regions"]]
    assert "Kurnool" in all_regions
    assert "district_511" not in all_regions
    # region_entries also shows resolved name
    all_entry_names = [e["name"] for g in groups for e in g["region_entries"]]
    assert "Kurnool" in all_entry_names


def test_weekly_blocks_omit_predicted_and_range():
    """Predicted and Range columns intentionally removed — blocks only carry zone_groups now."""
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 2,
            "startDatePredictedWeek": ["2026-06-01"] * 2,
            "regionID": ["r_high", "r_mid"],
            "prediction": [10.4, 5.6],
            "predictionZone": [3, 2],
            "thresholdMethod": ["historical"] * 2,
            "model": ["ensembleModel"] * 2,
        }
    )
    ctx = build_brief_context(
        predictions=df,
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="t",
    )
    blk = ctx["weekly_blocks"][0]
    assert "rows" not in blk  # legacy field gone
    assert "zone_groups" in blk
    # Per-group dicts shouldn't carry prediction or range either.
    for g in blk["zone_groups"]:
        assert "prediction_int" not in g
        assert "range_low" not in g
        assert "range_high" not in g


def test_weekly_blocks_have_pretty_label():
    """week_label_pretty should be formatted as 'DD Mon – DD Mon YYYY'."""
    ctx = build_brief_context(
        predictions=_sample_predictions(),
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="t",
    )
    label = ctx["weekly_blocks"][0]["week_label_pretty"]
    # 2026-06-01 is Monday 01 Jun; end is 07 Jun 2026
    assert "Jun" in label
    assert "2026" in label
    assert "–" in label


def test_weekly_blocks_have_week_idx():
    """Each weekly block should carry a 1-based week_idx for JS addressing."""
    ctx = build_brief_context(
        predictions=_sample_predictions(),
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="t",
    )
    for i, blk in enumerate(ctx["weekly_blocks"], start=1):
        assert blk["week_idx"] == i


def _mini_geojson(region_id: str, parent: str, parent_name: str, coords: list) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "region_id": region_id,
                    "name": region_id.upper(),
                    "parent": parent,
                    "parent_name": parent_name,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coords],
                },
            }
        ],
    }


def test_load_child_geojson_combined_simplifies():
    """load_child_geojson_combined reads geojsons, simplifies, and combines into FeatureCollection."""
    coords_a = [[0.0, 0.0], [0.1, 0.0], [0.1, 0.1], [0.0, 0.1], [0.0, 0.0]]
    coords_b = [[1.0, 1.0], [1.1, 1.0], [1.1, 1.1], [1.0, 1.1], [1.0, 1.0]]
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "region_a.geojson").write_text(
            json.dumps(_mini_geojson("region_a", "parent_1", "Parent One", coords_a))
        )
        (tmpdir / "region_b.geojson").write_text(
            json.dumps(_mini_geojson("region_b", "parent_1", "Parent One", coords_b))
        )
        fc = load_child_geojson_combined(tmpdir)
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 2
    ids = {f["properties"]["region_id"] for f in fc["features"]}
    assert ids == {"region_a", "region_b"}
    for f in fc["features"]:
        # Only the four kept properties should be present
        assert set(f["properties"].keys()) == {
            "region_id",
            "name",
            "parent",
            "parent_name",
        }


def test_compute_parent_lookup_groups_by_parent():
    """compute_parent_lookup aggregates child_ids and computes a bbox per parent."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "region_id": "child_1",
                    "name": "Child One",
                    "parent": "parent_A",
                    "parent_name": "PARENT ALPHA",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
                    ],
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "region_id": "child_2",
                    "name": "Child Two",
                    "parent": "parent_A",
                    "parent_name": "PARENT ALPHA",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[2.0, 2.0], [3.0, 2.0], [3.0, 3.0], [2.0, 3.0], [2.0, 2.0]]
                    ],
                },
            },
            {
                "type": "Feature",
                "properties": {
                    "region_id": "child_3",
                    "name": "Child Three",
                    "parent": "parent_B",
                    "parent_name": "PARENT BETA",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[5.0, 5.0], [6.0, 5.0], [6.0, 6.0], [5.0, 6.0], [5.0, 5.0]]
                    ],
                },
            },
        ],
    }
    lookup = compute_parent_lookup(fc)
    assert set(lookup.keys()) == {"parent_A", "parent_B"}
    pa = lookup["parent_A"]
    assert set(pa["child_ids"]) == {"child_1", "child_2"}
    assert pa["name"] == "Parent Alpha"
    # bbox should span both children: minx=0, miny=0, maxx=3, maxy=3
    assert pa["bbox"][0] == pytest.approx(0.0)
    assert pa["bbox"][1] == pytest.approx(0.0)
    assert pa["bbox"][2] == pytest.approx(3.0)
    assert pa["bbox"][3] == pytest.approx(3.0)
    pb = lookup["parent_B"]
    assert pb["child_ids"] == ["child_3"]
