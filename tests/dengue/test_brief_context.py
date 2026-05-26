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
        "risk_progression",
        "run_date",
        "footer_meta",
    }
    missing = required - set(ctx)
    assert not missing, f"missing keys: {missing}"


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


def test_risk_progression_zones_aligned_to_weeks():
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 4,
            "startDatePredictedWeek": [
                "2026-06-01",
                "2026-06-08",
                "2026-06-01",
                "2026-06-08",
            ],
            "regionID": ["r1", "r1", "r2", "r2"],
            "prediction": [3.0, 2.0, 0.5, 1.0],
            "predictionZone": [3, 2, 1, 1],
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
            "ISOWeek": [22, 23, 22, 23],
        }
    )
    ctx = build_brief_context(
        predictions=df,
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="t",
    )
    prog = {r["regionID"]: r["zones"] for r in ctx["risk_progression"]}
    assert prog["r1"] == [3, 2]
    assert prog["r2"] == [1, 1]
