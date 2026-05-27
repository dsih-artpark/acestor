import pandas as pd

from pipelines.dengue.lib.brief import build_brief_context, render_brief


def test_downscale_diagnostics_block_renders_when_is_downscale_true():
    df = pd.DataFrame(
        {
            "dateOfComputingPrediction": ["2026-05-21"] * 4,
            "startDatePredictedWeek": ["2026-06-01"] * 4,
            "regionID": [f"mandal_05{i:03d}" for i in range(4)],
            "prediction": [0.0, 0.5, 1.2, 0.3],
            "predictionZone": [0, 1, 1, 0],
            "thresholdMethod": ["historical"] * 4,
            "model": ["ensembleModel"] * 4,
            "Mean": [1.0] * 4,
            "StdDev": [0.5] * 4,
            "Zero": [0.0] * 4,
            "Inf": [float("inf")] * 4,
            "T0.00": [1.0] * 4,
            "T1.00": [1.5] * 4,
            "T2.00": [2.0] * 4,
            "recordDate": ["2026-05-25"] * 4,
            "ISOWeek": [22] * 4,
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
    assert "174" in html
