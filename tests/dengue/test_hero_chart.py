from pathlib import Path
import pandas as pd

from pipelines.dengue.lib.maps import render_hero_forecast


def test_render_hero_forecast_writes_png(tmp_path: Path):
    df = pd.DataFrame(
        {
            "startDatePredictedWeek": [
                "2026-06-01",
                "2026-06-08",
                "2026-06-15",
                "2026-06-22",
            ],
            "prediction": [3.2, 2.7, 1.1, 0.8],
            "regionID": ["district_511"] * 4,
            "model": ["ensembleModel"] * 4,
            "thresholdMethod": ["historical"] * 4,
            "StdDev": [1.0] * 4,
        }
    )
    out = tmp_path / "hero_forecast.png"
    render_hero_forecast(
        df, observed_df=None, run_date=pd.Timestamp("2026-05-21"), out_path=str(out)
    )
    assert out.exists()
    assert out.stat().st_size > 0


def test_render_hero_forecast_handles_multiple_models(tmp_path: Path):
    df = pd.DataFrame(
        {
            "startDatePredictedWeek": ["2026-06-01", "2026-06-08"] * 3,
            "prediction": [3.2, 2.7, 4.0, 3.5, 2.8, 2.1],
            "regionID": ["r1", "r1", "r1", "r1", "r1", "r1"],
            "model": ["ensembleModel", "ensembleModel", "nbr", "nbr", "xgb", "xgb"],
            "thresholdMethod": ["historical"] * 6,
            "StdDev": [1.0] * 6,
        }
    )
    out = tmp_path / "hero_multimodel.png"
    render_hero_forecast(
        df, observed_df=None, run_date=pd.Timestamp("2026-05-21"), out_path=str(out)
    )
    assert out.exists()
    assert out.stat().st_size > 0


def test_render_hero_forecast_no_observed(tmp_path: Path):
    df = pd.DataFrame(
        {
            "startDatePredictedWeek": ["2026-06-01"],
            "prediction": [3.0],
            "regionID": ["r1"],
            "model": ["ensembleModel"],
            "thresholdMethod": ["historical"],
            "StdDev": [1.0],
        }
    )
    out = tmp_path / "h.png"
    render_hero_forecast(
        df, observed_df=None, run_date=pd.Timestamp("2026-05-21"), out_path=str(out)
    )
    assert out.exists() and out.stat().st_size > 0


def test_render_hero_forecast_with_observed(tmp_path: Path):
    """Observed cases df enriches the chart without error."""
    predictions_df = pd.DataFrame(
        {
            "startDatePredictedWeek": ["2026-04-14", "2026-04-21"],
            "prediction": [20.0, 18.0],
            "regionID": ["district_511", "district_511"],
            "model": ["ensembleModel", "ensembleModel"],
            "thresholdMethod": ["historical", "historical"],
            "StdDev": [3.0, 3.0],
        }
    )
    dates = pd.date_range("2026-01-05", periods=14, freq="7D")
    observed_df = pd.DataFrame(
        {
            "date": list(dates) * 2,
            "region_id": ["district_511"] * 28,
            "case_count": [5, 7, 4, 8, 6, 9, 3, 5, 7, 4, 8, 6, 9, 3] * 2,
        }
    )
    out = tmp_path / "hero_with_obs.png"
    render_hero_forecast(
        predictions_df,
        observed_df=observed_df,
        run_date=pd.Timestamp("2026-04-08"),
        out_path=str(out),
    )
    assert out.exists() and out.stat().st_size > 0
