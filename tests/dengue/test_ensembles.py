"""Tests for the ensemble strategy registry."""

import pandas as pd
import pytest

from pipelines.dengue.lib.ensembles import (
    BaseEnsemble,
    _ENSEMBLE_REGISTRY,
    get_ensemble,
)


def _two_pred_dfs():
    df_a = pd.DataFrame(
        {
            "region": ["r0", "r1"],
            "recordDate": pd.to_datetime(["2022-05-25", "2022-05-25"]),
            "prediction": [10.0, 20.0],
            "model": ["a", "a"],
        }
    )
    df_b = pd.DataFrame(
        {
            "region": ["r0", "r1"],
            "recordDate": pd.to_datetime(["2022-05-25", "2022-05-25"]),
            "prediction": [20.0, 40.0],
            "model": ["b", "b"],
        }
    )
    return [df_a, df_b]


def test_mean_in_registry():
    assert "mean" in _ENSEMBLE_REGISTRY


def test_mean_satisfies_protocol():
    assert isinstance(get_ensemble("mean"), BaseEnsemble)


def test_get_ensemble_unknown_raises():
    with pytest.raises(KeyError, match="weighted"):
        get_ensemble("weighted")


def test_mean_takes_arithmetic_mean():
    e = get_ensemble("mean")
    out = e.combine(_two_pred_dfs(), spatial_col="region")
    out = out.sort_values("region").reset_index(drop=True)
    assert out["prediction"].tolist() == [15.0, 30.0]
    assert (out["model"] == "ensembleModel").all()
