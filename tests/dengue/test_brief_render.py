from pipelines.dengue.lib.brief import build_brief_context, render_brief
import pandas as pd


def _sample():
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


def test_render_brief_emits_html():
    ctx = build_brief_context(
        predictions=_sample(),
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="Test brief",
    )
    html = render_brief(ctx)
    assert "<!doctype html>" in html.lower()
    assert "Test brief" in html


def test_render_brief_shows_weekly_blocks():
    ctx = build_brief_context(
        predictions=_sample(),
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="Test brief",
    )
    html = render_brief(ctx)
    assert "Week starting" in html
    assert "risk_map_w1.png" in html
    assert "hero_forecast.png" in html
