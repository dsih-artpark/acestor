"""Regression test for issue #101: threshold_to_date alignment across models.

Before this fix, RF/XGB/NBR computed ``ctx.pred_upto - pd.Timedelta(days=28)``
while TSE/TimesFM used ``ctx.cutoff_case``. Byte-identical only when the
pipeline horizon was exactly 28 days. When the horizon shortened (e.g. GBA
today with W-FRI sampling producing a 21-day horizon), the two formulas
diverged, causing per-model ensemble members to carry different threshold
snapshots and the ensemble to split into two "duplicate" rows.

These tests pin the invariant: **every model's ``threshold_to_date`` must
return ``ctx.cutoff_case``, regardless of the horizon.**
"""

from __future__ import annotations

import pandas as pd
import pytest

from pipelines.dengue.lib.models import ModelContext, get_model


def _ctx(*, cutoff_case: str, pred_upto: str) -> ModelContext:
    """Minimal ModelContext for threshold_to_date probing.

    threshold_to_date only reads ctx.pred_upto and ctx.cutoff_case; every other
    field is unused by that method, so empty defaults are fine.
    """
    return ModelContext(
        merged_df=pd.DataFrame(),
        case_df=pd.DataFrame(),
        cfg=None,
        pred_upto=pd.Timestamp(pred_upto),
        cutoff_case=pd.Timestamp(cutoff_case),
    )


_MODELS = ["rf", "xgb", "nbr", "tse", "timesfm"]


@pytest.mark.parametrize("model_name", _MODELS)
def test_threshold_to_date_equals_cutoff_case_for_4week_horizon(model_name: str):
    """Sanity: when horizon = 28d, the invariant already held pre-fix."""
    ctx = _ctx(cutoff_case="2026-07-17", pred_upto="2026-08-14")  # 28 days
    model = get_model(model_name)
    assert model.threshold_to_date(ctx) == pd.Timestamp("2026-07-17")


@pytest.mark.parametrize("model_name", _MODELS)
def test_threshold_to_date_equals_cutoff_case_for_3week_horizon(model_name: str):
    """The GBA 2026-07-24 failure mode: 3-week horizon due to W-FRI sampling.

    Pre-fix, RF/XGB/NBR would have returned 2026-07-10 (pred_upto - 28d);
    TSE/TimesFM would have returned 2026-07-17 (cutoff_case). Divergent
    threshold-merge dates → divergent Mean/StdDev/T0..T2 → duplicate rows
    downstream.
    """
    ctx = _ctx(cutoff_case="2026-07-17", pred_upto="2026-08-07")  # 21 days
    model = get_model(model_name)
    assert model.threshold_to_date(ctx) == pd.Timestamp(
        "2026-07-17"
    ), f"{model_name} returned wrong threshold_to_date — must be cutoff_case"


@pytest.mark.parametrize("model_name", _MODELS)
def test_threshold_to_date_equals_cutoff_case_for_short_horizon(model_name: str):
    """Extreme case: 7-day horizon still returns cutoff_case."""
    ctx = _ctx(cutoff_case="2026-07-17", pred_upto="2026-07-24")  # 7 days
    model = get_model(model_name)
    assert model.threshold_to_date(ctx) == pd.Timestamp("2026-07-17")


def test_all_models_agree_on_threshold_to_date_for_any_horizon():
    """Cross-model invariant: for the same (cutoff_case, pred_upto), every
    model returns the SAME threshold_to_date. This is what enables
    merge_predictions_thresholds to produce identical threshold rows across
    models, which in turn lets the ensemble step combine per-model
    predictions into a single ensemble row per (region, week, method)."""
    ctx = _ctx(cutoff_case="2026-07-17", pred_upto="2026-08-07")
    values = {name: get_model(name).threshold_to_date(ctx) for name in _MODELS}
    unique = set(values.values())
    assert len(unique) == 1, (
        f"Model threshold_to_date values diverged for horizon != 28d: {values}. "
        f"This is the class of bug tracked in issue #101 — see "
        f"pipelines/dengue/lib/models/rf.py."
    )
