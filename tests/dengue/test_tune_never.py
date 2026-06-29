"""tune='never' mode — trust cache regardless of fingerprint, fail loud if missing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pipelines.dengue.configs import TrainPredictConfig, _parse_tune


def test_parse_tune_accepts_bool_true():
    assert _parse_tune(True) is True


def test_parse_tune_accepts_bool_false():
    assert _parse_tune(False) is False


def test_parse_tune_accepts_never_string():
    assert _parse_tune("never") == "never"


def test_parse_tune_accepts_truthy_strings():
    assert _parse_tune("true") is True
    assert _parse_tune("YES") is True


def test_parse_tune_accepts_falsy_strings():
    assert _parse_tune("false") is False
    assert _parse_tune("") is False


def test_parse_tune_rejects_unknown_string():
    with pytest.raises(ValueError, match="must be true, false, or 'never'"):
        _parse_tune("maybe")


def test_parse_tune_rejects_unknown_type():
    with pytest.raises(ValueError, match="must be true, false, or 'never'"):
        _parse_tune(42)


def test_train_predict_config_parses_tune_never():
    cfg = TrainPredictConfig.from_raw({"tune": "never"})
    assert cfg.tune == "never"


def test_rf_uses_cache_unconditionally_when_tune_never(tmp_path):
    """When tune='never', cached params are returned even when the fingerprint
    would otherwise be stale. No Optuna call should fire."""
    from acestor.io.storage import FileStorage
    from pipelines.dengue.lib.models import _tuning, rf as rf_module

    art = FileStorage(base_path=tmp_path)
    # Seed a cache + a STALE fingerprint (different region_type) so the
    # ordinary path would consider it invalid.
    _tuning.save_params(
        art,
        "rf",
        "corp",
        {
            "n_estimators": 99,
            "max_depth": 7,
            "min_samples_leaf": 3,
            "max_features": "sqrt",
        },
        rmse=11.11,
        n_trials=50,
        tuned_at="2026-06-19",
    )
    _tuning.save_fingerprint(art, "rf", "corp", "deliberately-wrong-hash")

    ctx = MagicMock()
    ctx.artifacts = art
    ctx.log = MagicMock()
    ctx.cfg.spatial_res = "corp"
    ctx.cfg.tune = "never"
    ctx.cfg.n_trials = 50

    # If tune='never' is honoured, _get_rf_params returns the cached params and
    # never calls _tuning.tune_rf. If it falls through to retuning, the test
    # would (a) try to run real Optuna and (b) overwrite the seeded params.
    out = rf_module._get_rf_params(
        ctx, X_train=None, y_train=None, train_max_date="2099-01-01"
    )
    assert out == {
        "n_estimators": 99,
        "max_depth": 7,
        "min_samples_leaf": 3,
        "max_features": "sqrt",
    }


def test_rf_tune_never_fails_loud_when_no_cache(tmp_path):
    from acestor.io.storage import FileStorage
    from pipelines.dengue.lib.models import rf as rf_module

    art = FileStorage(base_path=tmp_path)  # empty — no cache file

    ctx = MagicMock()
    ctx.artifacts = art
    ctx.log = MagicMock()
    ctx.cfg.spatial_res = "corp"
    ctx.cfg.tune = "never"
    ctx.cfg.n_trials = 50

    with pytest.raises(FileNotFoundError, match="tune='never' but no cached"):
        rf_module._get_rf_params(
            ctx, X_train=None, y_train=None, train_max_date="2099-01-01"
        )
