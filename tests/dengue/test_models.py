"""Tests for pipelines.dengue.lib.models (NBR helpers and TSE)."""

import numpy as np
import pandas as pd
import pytest

from pipelines.dengue.lib.models.nbr import _lag, _filter_features, _one_hot, _rescale
from pipelines.dengue.lib.models.tse import _process_region, linear_extrapolation
from pipelines.dengue.lib.models import BaseModel, get_model, ModelContext, _REGISTRY
from pipelines.dengue.configs import ReportConfig, TrainPredictConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_merged_df(n_weeks=20, n_regions=3, start="2022-01-05"):
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
                    "t2m_mean": 25.0,
                    "tp_sum": 5.0,
                    "d2m_mean": 18.0,
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# NBR helpers
# ---------------------------------------------------------------------------


def test_lag_adds_temp_and_rf_columns():
    df = _make_merged_df()
    result = _lag(
        df.copy(),
        "location.admin2.ID",
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
    )
    assert "temp_lag_12" in result.columns
    assert "rainfall_lag_4" in result.columns
    assert "relative_humidity_lag_4" in result.columns


def test_lag_shifts_by_correct_amount():
    df = _make_merged_df(n_regions=1)
    original_temp = df["t2m_mean"].reset_index(drop=True)
    result = _lag(
        df.copy(),
        "location.admin2.ID",
        lag_temp=[1],
        lag_rainfall=[1],
        lag_humidity=[1],
    )
    result = result.reset_index(drop=True)
    # first row should be NaN after a lag-1 shift
    assert pd.isna(result["temp_lag_1"].iloc[0])
    # second row should equal the first row's original temperature
    assert result["temp_lag_1"].iloc[1] == pytest.approx(original_temp.iloc[0])


def test_filter_features_drops_rows_with_nan():
    df = _make_merged_df()
    df = _lag(
        df, "location.admin2.ID", lag_temp=[12], lag_rainfall=[4], lag_humidity=[4]
    )
    feature_cols = [
        "case",
        "recordYear",
        "recordMonth",
        "ISOWeek",
        "t2m_mean",
        "tp_sum",
        "d2m_mean",
    ]
    result = _filter_features(df, feature_cols, "location.admin2.ID", [], [])
    assert result.isna().sum().sum() == 0


def test_filter_features_excludes_years():
    df = _make_merged_df(n_weeks=52, start="2022-01-05")
    df = _lag(
        df, "location.admin2.ID", lag_temp=[12], lag_rainfall=[4], lag_humidity=[4]
    )
    feature_cols = ["case", "recordYear", "recordMonth", "ISOWeek"]
    result = _filter_features(
        df,
        feature_cols,
        "location.admin2.ID",
        years_to_exclude=[2022],
        years_to_include=[],
    )
    assert 2022 not in result["recordYear"].values


def test_filter_features_includes_years():
    df = _make_merged_df(n_weeks=104, start="2022-01-05")
    df = _lag(
        df, "location.admin2.ID", lag_temp=[12], lag_rainfall=[4], lag_humidity=[4]
    )
    feature_cols = ["case", "recordYear", "recordMonth", "ISOWeek"]
    result = _filter_features(
        df,
        feature_cols,
        "location.admin2.ID",
        years_to_exclude=[],
        years_to_include=[2022],
    )
    assert set(result["recordYear"].unique()).issubset({2022})


def test_one_hot_encodes_iso_week():
    df = pd.DataFrame({"ISOWeek": [1, 2, 3, 1]})
    result = _one_hot(df)
    assert result.shape[0] == 4
    assert result.shape[1] == 3  # 3 unique weeks
    # All values must be binary (0 or 1)
    assert set(result.values.flatten().tolist()).issubset({0, 1})


def test_rescale_returns_values_in_0_1():
    df = pd.DataFrame(
        {
            "rainfall_lag_4": [0.0, 5.0, 10.0],
            "relative_humidity_lag_4": [10.0, 15.0, 20.0],
            "temp_lag_12": [20.0, 25.0, 30.0],
        }
    )
    scaled, _ = _rescale(df)
    assert scaled.min().min() >= 0.0
    assert scaled.max().max() <= 1.0


def test_rescale_with_existing_scaler():
    df = pd.DataFrame(
        {
            "rainfall_lag_4": [0.0, 5.0, 10.0],
            "relative_humidity_lag_4": [10.0, 15.0, 20.0],
            "temp_lag_12": [20.0, 25.0, 30.0],
        }
    )
    scaled1, scaler = _rescale(df)
    scaled2, _ = _rescale(df, scaler=scaler)
    assert scaled2.shape == df.shape
    # Re-applying the fitted scaler must produce identical values (not a re-fit)
    np.testing.assert_array_almost_equal(scaled2.values, scaled1.values)


# ---------------------------------------------------------------------------
# TSE helpers
# ---------------------------------------------------------------------------


def test_process_region_adds_moving_avg():
    df = _make_merged_df(n_weeks=10, n_regions=1)
    region_df = df[df["location.admin2.ID"] == "r0"].copy()
    result = _process_region(
        region_df,
        spatial_col="location.admin2.ID",
        predict_upto_date=pd.Timestamp("2022-03-09"),
        years_to_exclude=[],
        years_to_include=[],
    )
    assert "4wMovingAvg" in result.columns
    assert "MaxCaseMonthlyHistorical" in result.columns


def test_process_region_filters_to_predict_upto():
    df = _make_merged_df(n_weeks=20, n_regions=1)
    cutoff = pd.Timestamp("2022-03-01")
    result = _process_region(
        df[df["location.admin2.ID"] == "r0"].copy(),
        spatial_col="location.admin2.ID",
        predict_upto_date=cutoff,
        years_to_exclude=[],
        years_to_include=[],
    )
    assert (result["recordDate"] <= cutoff).all()


def test_linear_extrapolation_returns_dataframe():
    df = _make_merged_df(n_weeks=20, n_regions=2)
    result = linear_extrapolation(
        df,
        spatial_col="location.admin2.ID",
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=pd.Timestamp("2022-05-25"),
    )
    assert isinstance(result, pd.DataFrame)


def test_linear_extrapolation_output_columns():
    df = _make_merged_df(n_weeks=20, n_regions=2)
    result = linear_extrapolation(
        df,
        spatial_col="location.admin2.ID",
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=pd.Timestamp("2022-05-25"),
    )
    assert "prediction" in result.columns
    assert "model" in result.columns
    assert len(result) > 0
    assert (result["model"] == "timeSeriesExtrapolation").all()


def test_linear_extrapolation_predictions_non_negative():
    df = _make_merged_df(n_weeks=20, n_regions=2)
    result = linear_extrapolation(
        df,
        spatial_col="location.admin2.ID",
        years_to_exclude=[],
        years_to_include=[],
        predict_upto_date=pd.Timestamp("2022-05-25"),
    )
    assert len(result) > 0
    assert (result["prediction"] >= 0).all()


# ---------------------------------------------------------------------------
# Registry infrastructure
# ---------------------------------------------------------------------------


def _make_ctx(
    pred_upto: pd.Timestamp = pd.Timestamp("2022-05-25"),
    cutoff_case: pd.Timestamp = pd.Timestamp("2022-04-27"),
) -> ModelContext:
    merged = _make_merged_df(n_weeks=20, n_regions=2)
    cfg = TrainPredictConfig(
        spatial_res="location.admin2.ID",
        data_features=["case", "recordDate", "recordYear", "recordMonth", "ISOWeek"],
        lag_temp=[12],
        lag_rainfall=[4],
        lag_humidity=[4],
        years_to_exclude=[],
        years_to_include=[],
        list_alpha=[1.0, 2.0],
        models=["nbr", "tse"],
        ensemble="mean",
        output="ensemble",
        tune=False,
        n_trials=50,
    )
    case_cols = [
        "location.admin2.ID",
        "recordDate",
        "recordYear",
        "recordMonth",
        "ISOWeek",
        "case",
    ]
    return ModelContext(
        merged_df=merged.copy(),
        case_df=merged[case_cols].copy(),
        cfg=cfg,
        pred_upto=pred_upto,
        cutoff_case=cutoff_case,
    )


def test_model_context_is_dataclass():
    ctx = _make_ctx()
    assert isinstance(ctx.merged_df, pd.DataFrame)
    assert isinstance(ctx.pred_upto, pd.Timestamp)
    assert isinstance(ctx.cutoff_case, pd.Timestamp)


def test_get_model_raises_for_unknown():
    with pytest.raises(KeyError, match="notamodel"):
        get_model("notamodel")


def test_train_predict_config_default_models():
    cfg = TrainPredictConfig.from_raw({"list_alpha": [2.0, 3.0]})
    assert cfg.models == ["nbr", "tse"]


def test_train_predict_config_custom_models():
    cfg = TrainPredictConfig.from_raw({"models": ["nbr"], "list_alpha": [2.0, 3.0]})
    assert cfg.models == ["nbr"]


# ---------------------------------------------------------------------------
# NBRModel
# ---------------------------------------------------------------------------


def test_nbr_model_in_registry():
    assert "nbr" in _REGISTRY


def test_nbr_model_satisfies_protocol():
    assert isinstance(get_model("nbr"), BaseModel)


def test_nbr_threshold_to_date():
    ctx = _make_ctx(pred_upto=pd.Timestamp("2022-05-25"))
    model = get_model("nbr")
    # NBR: pred_upto - 28 days
    assert model.threshold_to_date(ctx) == pd.Timestamp("2022-04-27")


def test_nbr_model_predict_returns_dataframe():
    ctx = _make_ctx()
    model = get_model("nbr")
    result = model.predict(ctx)
    assert isinstance(result, pd.DataFrame)


# ---------------------------------------------------------------------------
# TSEModel
# ---------------------------------------------------------------------------


def test_tse_model_in_registry():
    assert "tse" in _REGISTRY


def test_tse_model_satisfies_protocol():
    assert isinstance(get_model("tse"), BaseModel)


def test_rf_in_registry():
    assert "rf" in _REGISTRY


def test_rf_satisfies_protocol():
    assert isinstance(get_model("rf"), BaseModel)


def test_xgb_in_registry():
    assert "xgb" in _REGISTRY


def test_xgb_satisfies_protocol():
    assert isinstance(get_model("xgb"), BaseModel)


def test_tse_threshold_to_date():
    ctx = _make_ctx(cutoff_case=pd.Timestamp("2022-05-11"))
    model = get_model("tse")
    # TSE: (cutoff_case + 14 days) - 14 days = cutoff_case
    assert model.threshold_to_date(ctx) == pd.Timestamp("2022-05-11")


def test_tse_model_predict_returns_dataframe():
    ctx = _make_ctx()
    model = get_model("tse")
    result = model.predict(ctx)
    assert isinstance(result, pd.DataFrame)


def test_tse_model_predict_uses_case_df_not_merged():
    """TSE must work when merged_df has no weather columns — it only needs case_df."""
    ctx = _make_ctx()
    # Replace merged_df with a stub that has no weather columns to confirm TSE ignores it
    import dataclasses

    case_only_ctx = dataclasses.replace(ctx, merged_df=pd.DataFrame())
    model = get_model("tse")
    result = model.predict(case_only_ctx)
    assert isinstance(result, pd.DataFrame)


# ---------------------------------------------------------------------------
# TrainPredictConfig — ensemble + output fields
# ---------------------------------------------------------------------------


def test_train_predict_config_default_ensemble_and_output():
    cfg = TrainPredictConfig.from_raw({"list_alpha": [2.0, 3.0]})
    assert cfg.ensemble == "mean"
    assert cfg.output == "ensemble"


def test_train_predict_config_custom_ensemble_and_output():
    cfg = TrainPredictConfig.from_raw(
        {"ensemble": "none", "output": "per_model", "list_alpha": [2.0, 3.0]}
    )
    assert cfg.ensemble == "none"
    assert cfg.output == "per_model"


def test_train_predict_config_invalid_output_value():
    with pytest.raises(ValueError, match="output"):
        TrainPredictConfig.from_raw({"output": "bogus", "list_alpha": [2.0, 3.0]})


def test_train_predict_config_ensemble_none_with_output_ensemble_raises():
    with pytest.raises(ValueError, match="ensemble.*none.*output.*ensemble"):
        TrainPredictConfig.from_raw(
            {"ensemble": "none", "output": "ensemble", "list_alpha": [2.0, 3.0]}
        )


# ---------------------------------------------------------------------------
# ReportConfig — primary field
# ---------------------------------------------------------------------------


def test_report_config_default_primary():
    cfg = ReportConfig.from_raw({})
    assert cfg.primary == "ensemble"


def test_report_config_custom_primary():
    cfg = ReportConfig.from_raw({"primary": "nbr"})
    assert cfg.primary == "nbr"


def test_lag_rainfall_and_humidity_are_independent():
    """lag_rainfall and lag_humidity can differ — they control separate feature columns."""
    cfg = TrainPredictConfig.from_raw(
        {
            "lag": {"lag_temp": [12], "lag_rainfall": [4], "lag_humidity": [2]},
            "list_alpha": [1.0, 2.0],
        }
    )
    assert cfg.lag_rainfall == [4]
    assert cfg.lag_humidity == [2]
