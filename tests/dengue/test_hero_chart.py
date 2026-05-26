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
        }
    )
    out = tmp_path / "hero_forecast.png"
    render_hero_forecast(df, out_path=str(out))
    assert out.exists()
    assert out.stat().st_size > 0


def test_render_hero_forecast_handles_multiple_models(tmp_path: Path):
    df = pd.DataFrame(
        {
            "startDatePredictedWeek": ["2026-06-01", "2026-06-08"] * 3,
            "prediction": [3.2, 2.7, 4.0, 3.5, 2.8, 2.1],
            "regionID": ["r1", "r1", "r1", "r1", "r1", "r1"],
            "model": ["ensembleModel", "ensembleModel", "nbr", "nbr", "xgb", "xgb"],
        }
    )
    out = tmp_path / "hero_multimodel.png"
    render_hero_forecast(df, out_path=str(out))
    assert out.exists()
    assert out.stat().st_size > 0
