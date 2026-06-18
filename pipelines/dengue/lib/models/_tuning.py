"""Optuna hyperparameter tuning + JSON cache for RF and XGB models.

Fold logic and search spaces ported from vbd-modelbench tune_rf_cv / tune_xgb_cv.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import numpy as np
import optuna
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error
from xgboost import XGBRegressor

optuna.logging.set_verbosity(optuna.logging.WARNING)

log = logging.getLogger(__name__)


def _key(model_name: str, region_type: str) -> str:
    """Combine model + region into a single cache key segment.

    Cache files are keyed by ``(model, region_type)`` so a run that tuned
    against district-level data cannot silently feed those params back to a
    ward-level run (the training distribution is different, but nothing in
    the legacy ``model_name``-only key prevented the share).
    """
    return f"{model_name}_{region_type}" if region_type else model_name


def hp_cache_path(model_name: str, region_type: str = "") -> str:
    return f"hp/{_key(model_name, region_type)}_best_params.json"


def fingerprint_path(model_name: str, region_type: str = "") -> str:
    return f"hp/{_key(model_name, region_type)}_fingerprint.json"


def compute_fingerprint(cfg: Any, train_max_date: str) -> str:
    """Return a short hash of the inputs that would invalidate cached hyperparams.

    Covers lag windows, feature list, year filters, the training data cutoff,
    and the region type (so a district / ward swap invalidates the cache even
    if all other inputs match).
    """
    payload = {
        "lag_temp": sorted(cfg.lag_temp),
        "lag_rainfall": sorted(cfg.lag_rainfall),
        "lag_humidity": sorted(cfg.lag_humidity),
        "lag_cases": sorted(cfg.lag_cases),
        "data_features": sorted(cfg.data_features),
        "years_to_exclude": sorted(cfg.years_to_exclude),
        "years_to_include": sorted(cfg.years_to_include),
        "train_max_date": train_max_date,
        "region_type": getattr(cfg, "spatial_res", "") or "",
    }
    raw = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def save_fingerprint(
    storage: Any, model_name: str, region_type: str, fingerprint: str
) -> None:
    storage.write_text(
        json.dumps({"fingerprint": fingerprint}),
        fingerprint_path(model_name, region_type),
    )


def load_fingerprint(
    storage: Any, model_name: str, region_type: str = ""
) -> str | None:
    try:
        data = storage.read_json(fingerprint_path(model_name, region_type))
        return data.get("fingerprint")
    except (FileNotFoundError, KeyError):
        return None


def check_fingerprint(
    storage: Any,
    model_name: str,
    cfg: Any,
    train_max_date: str,
    _log: Any,
) -> bool:
    """Return True if cached params are valid for the current config.

    Returns False on mismatch or when the fingerprint file is missing — the
    caller must honor this (auto-retune or fail). Until 2026 this function's
    return was being discarded at every call site; the silent-stale-params
    path is the bug fixed by #66.
    """
    region_type = getattr(cfg, "spatial_res", "") or ""
    current = compute_fingerprint(cfg, train_max_date)
    saved = load_fingerprint(storage, model_name, region_type)
    if saved is None:
        _log.warning(
            "%s [%s]: no tuning fingerprint found — treating cached params as "
            "stale and retuning",
            model_name.upper(),
            region_type or "?",
        )
        return False
    if saved != current:
        _log.warning(
            "%s [%s]: tuning fingerprint mismatch (config changed since last "
            "tune) — retuning",
            model_name.upper(),
            region_type or "?",
        )
        return False
    return True


def load_cached_params(
    storage: Any, model_name: str, region_type: str = ""
) -> dict | None:
    try:
        return storage.read_json(hp_cache_path(model_name, region_type))
    except (FileNotFoundError, KeyError):
        return None


def save_params(
    storage: Any,
    model_name: str,
    region_type: str,
    params: dict,
    *,
    rmse: float,
    n_trials: int,
    tuned_at: str,
) -> None:
    payload = {
        "tuned_at": tuned_at,
        "n_trials": n_trials,
        "best_rmse": round(float(rmse), 4),
        "params": params,
    }
    storage.write_text(
        json.dumps(payload, indent=2), hp_cache_path(model_name, region_type)
    )


def _make_folds(n: int, n_folds: int = 5) -> list[dict]:
    """Expanding-window folds over n time steps (mirrors vbd-modelbench fold logic).

    Each fold trains on everything up to train_end and validates on the next block.
    train_end and val_end are 1-based indices into the sorted time axis.
    """
    fold_size = n // (n_folds + 1)
    folds = []
    for i in range(n_folds):
        train_end = fold_size * (i + 1)
        val_end = train_end + fold_size
        folds.append({"train_end": train_end, "val_end": min(val_end, n)})
    return folds


def tune_rf(
    X_train: np.ndarray, y_train: np.ndarray, *, n_trials: int
) -> tuple[dict, float]:
    """Optuna search for RF hyperparameters. Returns (best_params, best_rmse).

    Search space matches vbd-modelbench tune_rf_cv.
    """
    n = len(X_train)
    folds = _make_folds(n)

    def objective(trial):
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 100, 800, step=50),
            "max_depth": trial.suggest_int("max_depth", 5, 40),
            "min_samples_leaf": trial.suggest_int("min_samples_leaf", 1, 20),
            "max_features": trial.suggest_categorical(
                "max_features", ["sqrt", "log2", 0.3, 0.5, 0.7, 1.0]
            ),
        }
        fold_rmses = []
        for fold in folds:
            t_end, v_end = fold["train_end"], fold["val_end"]
            X_t, y_t = X_train[:t_end], y_train[:t_end]
            X_v, y_v = X_train[t_end:v_end], y_train[t_end:v_end]
            if len(X_t) == 0 or len(X_v) == 0:
                continue
            m = RandomForestRegressor(**params, random_state=42, n_jobs=-1)
            m.fit(X_t, y_t)
            preds = np.maximum(0.0, m.predict(X_v))
            fold_rmses.append(mean_squared_error(y_v, preds) ** 0.5)
        return float(np.mean(fold_rmses)) if fold_rmses else float("inf")

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    return study.best_params, study.best_value


def tune_xgb(
    X_train: np.ndarray, y_train: np.ndarray, *, n_trials: int
) -> tuple[dict, float]:
    """Optuna search for XGB hyperparameters. Returns (best_params, best_rmse).

    Search space matches vbd-modelbench tune_xgb_cv.
    """
    n = len(X_train)
    folds = _make_folds(n)

    def objective(trial):
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 400, 2000, step=200),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
            "max_depth": trial.suggest_int("max_depth", 3, 6),
            "min_child_weight": trial.suggest_int("min_child_weight", 5, 20),
            "subsample": trial.suggest_float("subsample", 0.6, 0.9),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.6, 0.9),
            "gamma": trial.suggest_float("gamma", 0.0, 2.0),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.0, 2.0),
            "reg_lambda": trial.suggest_float("reg_lambda", 2.0, 10.0),
        }
        fold_rmses = []
        for fold in folds:
            t_end, v_end = fold["train_end"], fold["val_end"]
            X_t, y_t = X_train[:t_end], y_train[:t_end]
            X_v, y_v = X_train[t_end:v_end], y_train[t_end:v_end]
            if len(X_t) == 0 or len(X_v) == 0:
                continue
            m = XGBRegressor(**params, random_state=42, n_jobs=-1, verbosity=0)
            m.fit(X_t, y_t)
            preds = np.maximum(0.0, m.predict(X_v))
            fold_rmses.append(mean_squared_error(y_v, preds) ** 0.5)
        return float(np.mean(fold_rmses)) if fold_rmses else float("inf")

    study = optuna.create_study(direction="minimize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    return study.best_params, study.best_value
