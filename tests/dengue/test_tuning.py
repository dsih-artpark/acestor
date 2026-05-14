"""Tests for HP tuning cache logic."""

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from pipelines.dengue.configs import TrainPredictConfig
from pipelines.dengue.lib.models import ModelContext, get_model
from pipelines.dengue.lib.models._tuning import (
    hp_cache_path,
    load_cached_params,
    save_params,
    tune_rf,
    tune_xgb,
)


def _base_raw():
    return {
        "spatial_res": "district",
        "models": ["rf"],
        "lag": {"lag_temp": [12], "lag_rainfall": [4], "lag_humidity": [4]},
        "list_alpha": [2.0, 3.0],
    }


def test_tune_defaults_to_false():
    cfg = TrainPredictConfig.from_raw(_base_raw())
    assert cfg.tune is False


def test_n_trials_defaults_to_50():
    cfg = TrainPredictConfig.from_raw(_base_raw())
    assert cfg.n_trials == 50


def test_tune_can_be_set_true():
    cfg = TrainPredictConfig.from_raw({**_base_raw(), "tune": True})
    assert cfg.tune is True


def test_n_trials_can_be_overridden():
    cfg = TrainPredictConfig.from_raw({**_base_raw(), "n_trials": 100})
    assert cfg.n_trials == 100


def _make_train_data(n: int = 120):
    """Synthetic X_train, y_train for tuning tests (small, fast)."""
    rng = np.random.default_rng(42)
    X = rng.standard_normal((n, 4))
    y = np.maximum(0, X[:, 0] * 3 + rng.standard_normal(n))
    return X, y


def test_hp_cache_path_rf():
    assert hp_cache_path("rf") == "hp/rf_best_params.json"


def test_hp_cache_path_xgb():
    assert hp_cache_path("xgb") == "hp/xgb_best_params.json"


def test_load_cached_params_returns_none_on_missing():
    storage = MagicMock()
    storage.read_json.side_effect = FileNotFoundError
    assert load_cached_params(storage, "rf") is None


def test_load_cached_params_returns_dict_on_hit():
    storage = MagicMock()
    storage.read_json.return_value = {
        "tuned_at": "2026-04-17",
        "n_trials": 50,
        "best_rmse": 1.23,
        "params": {"n_estimators": 350},
    }
    result = load_cached_params(storage, "rf")
    assert result["params"]["n_estimators"] == 350


def test_save_params_writes_json():
    storage = MagicMock()
    save_params(
        storage,
        "rf",
        {"n_estimators": 300},
        rmse=1.5,
        n_trials=50,
        tuned_at="2026-05-11",
    )
    storage.write_text.assert_called_once()
    written_json = storage.write_text.call_args[0][0]
    data = json.loads(written_json)
    assert data["params"]["n_estimators"] == 300
    assert data["best_rmse"] == 1.5
    assert data["tuned_at"] == "2026-05-11"
    assert storage.write_text.call_args[0][1] == "hp/rf_best_params.json"


def test_tune_rf_returns_params_and_rmse():
    X, y = _make_train_data(120)
    params, rmse = tune_rf(X, y, n_trials=3)
    assert "n_estimators" in params
    assert "max_depth" in params
    assert rmse >= 0.0


def test_tune_xgb_returns_params_and_rmse():
    X, y = _make_train_data(120)
    params, rmse = tune_xgb(X, y, n_trials=3)
    assert "n_estimators" in params
    assert "learning_rate" in params
    assert rmse >= 0.0


def _make_ctx(tune: bool = False, n_trials: int = 3, artifacts=None):
    cfg = TrainPredictConfig.from_raw(
        {
            "spatial_res": "district",
            "models": ["rf"],
            "lag": {"lag_temp": [12], "lag_rainfall": [4], "lag_humidity": [4]},
            "years_to_exclude": [],
            "years_to_include": [],
            "tune": tune,
            "n_trials": n_trials,
            "list_alpha": [2.0, 3.0],
        }
    )
    dates = pd.date_range("2022-01-07", periods=80, freq="7D")
    rows = []
    for d in dates:
        rows.append(
            {
                "district": "r0",
                "recordDate": d,
                "recordYear": d.year,
                "recordMonth": d.month,
                "ISOWeek": d.isocalendar().week,
                "case": float(d.month),
                "t2m_mean": 25.0,
                "tp_sum": 5.0,
                "d2m_mean": 18.0,
            }
        )
    merged_df = pd.DataFrame(rows)
    return ModelContext(
        merged_df=merged_df,
        case_df=merged_df.copy(),
        cfg=cfg,
        pred_upto=dates[-1],
        cutoff_case=dates[-5],
        artifacts=artifacts,
    )


def test_rf_uses_cached_params_when_available():
    cached = {
        "tuned_at": "2026-01-01",
        "n_trials": 50,
        "best_rmse": 1.0,
        "params": {
            "n_estimators": 75,
            "max_depth": 4,
            "min_samples_leaf": 3,
            "max_features": "sqrt",
        },
    }
    storage = MagicMock()
    storage.read_json.return_value = cached
    ctx = _make_ctx(tune=False, artifacts=storage)
    model = get_model("rf")
    with patch("pipelines.dengue.lib.models._tuning.tune_rf") as mock_tune:
        model.predict(ctx)
    mock_tune.assert_not_called()


def test_rf_runs_tuning_when_no_cache():
    storage = MagicMock()
    storage.read_json.side_effect = FileNotFoundError
    ctx = _make_ctx(tune=False, n_trials=2, artifacts=storage)
    model = get_model("rf")
    with patch(
        "pipelines.dengue.lib.models._tuning.tune_rf",
        return_value=(
            {
                "n_estimators": 100,
                "max_depth": 5,
                "min_samples_leaf": 2,
                "max_features": "sqrt",
            },
            1.0,
        ),
    ) as mock_tune:
        model.predict(ctx)
    mock_tune.assert_called_once()
    storage.write_text.assert_called_once()


def test_rf_force_retune_ignores_cache():
    cached = {
        "tuned_at": "2026-01-01",
        "n_trials": 50,
        "best_rmse": 1.0,
        "params": {
            "n_estimators": 75,
            "max_depth": 4,
            "min_samples_leaf": 3,
            "max_features": "sqrt",
        },
    }
    storage = MagicMock()
    storage.read_json.return_value = cached
    ctx = _make_ctx(tune=True, n_trials=2, artifacts=storage)
    model = get_model("rf")
    with patch(
        "pipelines.dengue.lib.models._tuning.tune_rf",
        return_value=(
            {
                "n_estimators": 200,
                "max_depth": 6,
                "min_samples_leaf": 1,
                "max_features": "log2",
            },
            0.9,
        ),
    ) as mock_tune:
        model.predict(ctx)
    mock_tune.assert_called_once()
