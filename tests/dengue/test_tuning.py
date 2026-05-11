"""Tests for HP tuning cache logic."""

from pipelines.dengue.configs import TrainPredictConfig


def _base_raw():
    return {
        "spatial_res": "district",
        "models": ["rf"],
        "lag": {"lag_temp": [12], "lag_rf": [4]},
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
