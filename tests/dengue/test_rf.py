"""Unit tests for the Random Forest regression model."""

import numpy as np
import pandas as pd

from pipelines.dengue.lib.models.rf import random_forest_regression


def _make_merged_df(
    n_weeks: int = 20, n_regions: int = 3, start: str = "2022-01-05"
) -> pd.DataFrame:
    np.random.seed(42)
    rows = []
    dates = pd.date_range(start, periods=n_weeks, freq="7D")
    for region in [f"r{i}" for i in range(n_regions)]:
        for d in dates:
            rows.append(
                {
                    "location.admin2.ID": region,
                    "recordDate": d,
                    "recordYear": d.year,
                    "recordMonth": d.month,
                    "ISOWeek": d.isocalendar().week,
                    "case": float(np.random.randint(0, 10)),
                    "t2m_mean": 25.0 + np.random.randn(),
                    "tp_sum": 5.0 + abs(np.random.randn()),
                    "d2m_mean": 18.0 + np.random.randn(),
                }
            )
    return pd.DataFrame(rows)


SPATIAL_COL = "location.admin2.ID"


def test_rf_returns_expected_columns():
    df = _make_merged_df()
    result = random_forest_regression(
        df,
        spatial_col=SPATIAL_COL,
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
        lag_cases=[],
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=df["recordDate"].max(),
    )
    assert isinstance(result, pd.DataFrame)
    for col in [SPATIAL_COL, "recordDate", "prediction", "model"]:
        assert col in result.columns, f"Missing column: {col}"


def test_rf_predictions_are_non_negative():
    df = _make_merged_df()
    result = random_forest_regression(
        df,
        spatial_col=SPATIAL_COL,
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
        lag_cases=[],
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=df["recordDate"].max(),
    )
    assert (result["prediction"] >= 0).all()


def test_rf_predicts_last_4_dates_only():
    df = _make_merged_df()
    pred_upto = df["recordDate"].max()
    result = random_forest_regression(
        df,
        spatial_col=SPATIAL_COL,
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
        lag_cases=[],
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=pred_upto,
    )
    expected_dates = set(
        pd.Timestamp(d).date() for d in sorted(df["recordDate"].unique())[-4:]
    )
    actual_dates = set(pd.to_datetime(result["recordDate"]).dt.date)
    assert actual_dates == expected_dates


def test_rf_model_label():
    df = _make_merged_df()
    result = random_forest_regression(
        df,
        spatial_col=SPATIAL_COL,
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
        lag_cases=[],
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=df["recordDate"].max(),
    )
    assert (result["model"] == "randomForestRegression").all()


def test_rf_returns_empty_when_no_valid_lag_features():
    # Only 3 weeks — after lag-12 shift, all training rows drop as NaN → empty
    df = _make_merged_df(n_weeks=3)
    result = random_forest_regression(
        df,
        spatial_col=SPATIAL_COL,
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
        lag_cases=[],
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=df["recordDate"].max(),
    )
    assert isinstance(result, pd.DataFrame)
    assert result.empty


def test_rf_skips_regions_with_nan_lag_features():
    df = _make_merged_df(n_weeks=20, n_regions=3)
    # NaN out ALL weather for r0 — ensures every lag feature for r0 is NaN,
    # including those derived for the last-4 test window.
    mask = df["location.admin2.ID"] == "r0"
    df.loc[mask, "t2m_mean"] = float("nan")
    df.loc[mask, "tp_sum"] = float("nan")
    df.loc[mask, "d2m_mean"] = float("nan")

    result = random_forest_regression(
        df,
        spatial_col=SPATIAL_COL,
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
        lag_cases=[],
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=df["recordDate"].max(),
    )
    assert isinstance(result, pd.DataFrame)
    # r0 should be absent (all its test-window lag features are NaN)
    assert "r0" not in result[SPATIAL_COL].values
    # r1 and r2 should still have predictions
    assert set(result[SPATIAL_COL].unique()) >= {"r1", "r2"}


def test_rf_skips_regions_with_nan_lag_features_in_test_window():
    # NaN out ALL weather for r2 — ensures lag features for its test window are NaN.
    df = _make_merged_df()
    mask = df["location.admin2.ID"] == "r2"
    df.loc[mask, ["t2m_mean", "tp_sum", "d2m_mean"]] = np.nan
    result = random_forest_regression(
        df,
        spatial_col=SPATIAL_COL,
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
        lag_cases=[],
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=df["recordDate"].max(),
    )
    # r2 should be absent; r0 and r1 should be present
    assert "r2" not in result[SPATIAL_COL].values
    assert "r0" in result[SPATIAL_COL].values
    assert "r1" in result[SPATIAL_COL].values
