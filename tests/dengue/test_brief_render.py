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
    assert "weekly-label" in html
    assert "risk_map_w1.png" in html
    assert "hero_forecast.png" in html


def test_render_with_downscale_diagnostics():
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 2,
            "startDatePredictedWeek": ["2026-06-01"] * 2,
            "regionID": ["mandal_05001", "mandal_05002"],
            "prediction": [0.0, 0.5],
            "predictionZone": [0, 1],
            "thresholdMethod": ["historical"] * 2,
            "model": ["ensembleModel"] * 2,
            "Mean": [1.0] * 2,
            "StdDev": [0.5] * 2,
            "Zero": [0.0] * 2,
            "Inf": [float("inf")] * 2,
            "T0.00": [1.0] * 2,
            "T1.00": [1.5] * 2,
            "T2.00": [2.0] * 2,
            "recordDate": ["2026-05-25"] * 2,
            "ISOWeek": [22] * 2,
        }
    )
    diag = {
        "conservation_max_abs_err": 2.2e-16,
        "n_parents_uniform": 2,
        "n_weeks_children_above_parent": 1,
        "n_weeks_children_below_parent": 174,
        "n_zone_zero": 2,
        "n_total": 4,
    }
    ctx = build_brief_context(
        predictions=df,
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=True,
        document_title="Mandal brief",
        downscale_diagnostics=diag,
    )
    html = render_brief(ctx)
    assert "Downscale diagnostics" in html
    assert "Parents split uniformly" in html
    # 2 should appear (n_parents_uniform) and "174" too
    assert "174" in html


def test_render_without_downscale_diagnostics_omits_section():
    """The downscale section must NOT render when is_downscale=False."""
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 2,
            "startDatePredictedWeek": ["2026-06-01"] * 2,
            "regionID": ["r1", "r2"],
            "prediction": [1.0, 2.0],
            "predictionZone": [1, 2],
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
        document_title="District brief",
    )
    html = render_brief(ctx)
    assert "Downscale diagnostics" not in html


def test_render_brief_table_has_predicted_and_range_columns():
    """Table headers must include Predicted and Range."""
    ctx = build_brief_context(
        predictions=_sample(),
        run_date="2026-05-21",
        charts_relpath="charts",
        is_downscale=False,
        document_title="Test brief",
    )
    html = render_brief(ctx)
    assert "<th>Predicted</th>" in html
    assert "<th>Range</th>" in html
