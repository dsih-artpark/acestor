from __future__ import annotations

import pandas as pd

from pipelines.dengue.lib.brief import build_brief_context


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
    rows = blocks[0]["rows"]
    region_ids = {r["regionID"] for r in rows}
    assert region_ids == {"r_high", "r_mid"}  # only zone >= 2


def test_weekly_blocks_rows_have_band_fields():
    """Rows should have band_text and band_class, not prediction float."""
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 2,
            "startDatePredictedWeek": ["2026-06-01"] * 2,
            "regionID": ["r_high", "r_mid"],
            "prediction": [10.0, 5.0],
            "predictionZone": [3, 2],
            "thresholdMethod": ["historical"] * 2,
            "model": ["ensembleModel"] * 2,
            "Mean": [1.0] * 2,
            "StdDev": [1.0] * 2,
            "Zero": [0.0] * 2,
            "Inf": [float("inf")] * 2,
            "T0.00": [1.0] * 2,
            "T1.00": [2.0] * 2,
            "T2.00": [3.0] * 2,
            "recordDate": ["2026-05-25"] * 2,
            "ISOWeek": [22] * 2,
        }
    )
    ctx = build_brief_context(
        predictions=df,
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="t",
    )
    rows = ctx["weekly_blocks"][0]["rows"]
    by_region = {r["regionID"]: r for r in rows}
    assert by_region["r_high"]["band_text"] == "High"
    assert by_region["r_high"]["band_class"] == "high"
    assert by_region["r_mid"]["band_text"] == "Medium"
    assert by_region["r_mid"]["band_class"] == "med"
    # prediction float must NOT be present
    assert "prediction" not in by_region["r_high"]


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
    rows = ctx["weekly_blocks"][0]["rows"]
    assert rows[0]["regionName"] == "Kurnool"


def test_weekly_blocks_rows_have_prediction_int_field():
    """Rows include prediction_int (rounded). Range columns dropped — see PR feedback."""
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
    rows = ctx["weekly_blocks"][0]["rows"]
    by_region = {r["regionID"]: r for r in rows}
    assert by_region["r_high"]["prediction_int"] == 10
    assert by_region["r_mid"]["prediction_int"] == 6  # 5.6 → 6
    # Range columns intentionally removed; should not be present.
    assert "range_low" not in by_region["r_high"]
    assert "range_high" not in by_region["r_high"]


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
