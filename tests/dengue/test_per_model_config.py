"""Tests for per-model config override resolution."""

from pipelines.dengue.configs import TrainPredictConfig, resolve_model_config


def _base_cfg() -> TrainPredictConfig:
    return TrainPredictConfig.from_raw(
        {
            "spatial_res": "district",
            "models": ["nbr", "tse", "rf"],
            "ensemble": "mean",
            "output": "ensemble",
            "lag": {"lag_temp": [12], "lag_rf": [4]},
            "list_alpha": [2.0, 3.0],
            "data_features": [
                "case",
                "recordDate",
                "recordYear",
                "recordMonth",
                "ISOWeek",
                "t2m_mean",
                "tp_sum",
                "d2m_mean",
            ],
            "years_to_exclude": [],
            "years_to_include": [],
        }
    )


def test_no_overrides_returns_base_cfg():
    base = _base_cfg()
    result = resolve_model_config(base, {})
    assert result.lag_temp == base.lag_temp
    assert result.lag_rf == base.lag_rf
    assert result.list_alpha == base.list_alpha


def test_lag_temp_override():
    base = _base_cfg()
    result = resolve_model_config(base, {"lag": {"lag_temp": [8, 12], "lag_rf": [4]}})
    assert result.lag_temp == [8, 12]
    assert result.lag_rf == [4]  # unchanged


def test_lag_rf_override():
    base = _base_cfg()
    result = resolve_model_config(base, {"lag": {"lag_temp": [12], "lag_rf": [2, 4]}})
    assert result.lag_rf == [2, 4]
    assert result.lag_temp == [12]  # unchanged


def test_list_alpha_override():
    base = _base_cfg()
    result = resolve_model_config(base, {"list_alpha": [1.5, 2.0, 3.0]})
    assert result.list_alpha == [1.5, 2.0, 3.0]


def test_data_features_override():
    base = _base_cfg()
    tse_features = ["case", "recordDate", "recordYear", "recordMonth", "ISOWeek"]
    result = resolve_model_config(base, {"data_features": tse_features})
    assert result.data_features == tse_features


def test_years_to_exclude_override():
    base = _base_cfg()
    result = resolve_model_config(base, {"years_to_exclude": [2020]})
    assert result.years_to_exclude == [2020]


def test_override_does_not_mutate_base():
    base = _base_cfg()
    resolve_model_config(base, {"lag": {"lag_temp": [4], "lag_rf": [2]}})
    assert base.lag_temp == [12]  # base is unchanged (frozen dataclass)


def test_partial_lag_override_uses_base_for_missing_keys():
    # If only lag_temp is overridden, lag_rf should fall back to base value
    base = _base_cfg()
    result = resolve_model_config(base, {"lag": {"lag_temp": [8]}})
    assert result.lag_temp == [8]
    assert result.lag_rf == [4]  # falls back to base


def test_resolve_model_config_only_affects_target_model():
    """Smoke: two models with different overrides get different configs."""
    base = _base_cfg()
    nbr_cfg = resolve_model_config(base, {"lag": {"lag_temp": [8, 12], "lag_rf": [4]}})
    rf_cfg = resolve_model_config(
        base, {"lag": {"lag_temp": [4, 8, 12], "lag_rf": [2, 4]}}
    )
    assert nbr_cfg.lag_temp == [8, 12]
    assert rf_cfg.lag_temp == [4, 8, 12]
    assert rf_cfg.lag_rf == [2, 4]
    assert nbr_cfg.lag_rf == [4]  # nbr override didn't affect rf
    assert base.lag_temp == [12]  # base still untouched
