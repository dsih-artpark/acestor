"""Guardrail + zone-rederivation tests for downscale (issues #37, #38, #39)."""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from pipelines.dengue.lib.thresholds import ThresholdContext
from pipelines.dengue_downscale.lib.downscale import (
    assign_child_zones,
    check_numeric_sanity,
    compute_shares,
    downscale_diagnostics,
    downscale_predictions,
    validate_cases_df,
)


def _zone_kwargs(
    methods=("historical", "prev_nweeks", "weighted_baseline"),
    classification="who",
    list_alpha=(1.0, 2.0),
):
    """Config-driven zone params, as the step supplies them from YAML."""
    return dict(
        list_alpha=list(list_alpha),
        classification_method=classification,
        ctx_by_method={m: ThresholdContext() for m in methods},
    )


def _parent_row(region_id, prediction, zone, method="prev_nweeks"):
    return {
        "dateOfComputingPrediction": "2026-03-01",
        "startDatePredictedWeek": "2026-03-08",
        "regionID": region_id,
        "prediction": prediction,
        "thresholdMethod": method,
        "predictionZone": zone,
        "model": "ensembleModel",
    }


def _flat_history(region_ids, per_week, weeks=14, end="2026-03-01"):
    """One row per (region, weekly date), each with `per_week` cases."""
    dates = pd.date_range(end=end, periods=weeks, freq="7D")
    rows = [
        {"region_id": rid, "date": d, "case_count": per_week}
        for d in dates
        for rid in region_ids
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# #37 — totals conserve / fail-fast on parents with no children
# ---------------------------------------------------------------------------


def test_missing_parent_raises_by_default():
    mapping = {"m1": "d1"}
    cases = _flat_history(["m1"], 1)
    preds = pd.DataFrame([_parent_row("d1", 100.0, 1), _parent_row("d2", 80.0, 1)])
    with pytest.raises(ValueError, match="d2"):
        downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), 4, **_zone_kwargs()
        )


def test_missing_parent_warn_mode_does_not_raise(caplog):
    mapping = {"m1": "d1"}
    cases = _flat_history(["m1"], 1)
    preds = pd.DataFrame([_parent_row("d1", 100.0, 1), _parent_row("d2", 80.0, 1)])
    with caplog.at_level(logging.WARNING):
        out = downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), 4,
            on_missing_parents="warn", **_zone_kwargs(),
        )
    assert "d2" in caplog.text
    assert set(out["regionID"]) == {"m1"}


def test_child_predictions_conserve_parent_total():
    mapping = {"m1": "d1", "m2": "d1"}
    cases = _flat_history(["m1", "m2"], 5)  # equal history → 50/50 split
    preds = pd.DataFrame([_parent_row("d1", 100.0, 1)])
    out = downscale_predictions(
        preds, mapping, cases, pd.Timestamp("2026-03-01"), 4, **_zone_kwargs()
    )
    assert out["prediction"].sum() == pytest.approx(100.0, abs=1e-9)


# ---------------------------------------------------------------------------
# #38 — child zone re-derived from its OWN threshold, not inherited
# ---------------------------------------------------------------------------


def test_zero_history_child_gets_zone_zero_not_parent_zone():
    # Parent labelled high (zone 4). m_active has cases, m_zero never did.
    mapping = {"m_active": "dX", "m_zero": "dX"}
    cases = pd.concat(
        [_flat_history(["m_active"], 5), _flat_history(["m_zero"], 0)],
        ignore_index=True,
    )
    preds = pd.DataFrame([_parent_row("dX", 100.0, 4)])
    out = downscale_predictions(
        preds, mapping, cases, pd.Timestamp("2026-03-01"), 4, **_zone_kwargs()
    )
    m_zero = out.loc[out["regionID"] == "m_zero", "predictionZone"].iloc[0]
    assert m_zero == 0  # degenerate threshold → no risk, NOT parent's 4


def test_active_child_zone_reflects_its_own_threshold():
    # m_active: flat history of 1/week → Mean≈1, SD≈1 (inflated) → T2=3.
    # It takes essentially all the parent's 100 cases → far above T2 → zone 4.
    mapping = {"m_active": "dX", "m_zero": "dX"}
    cases = pd.concat(
        [_flat_history(["m_active"], 1), _flat_history(["m_zero"], 0)],
        ignore_index=True,
    )
    preds = pd.DataFrame([_parent_row("dX", 100.0, 4)])
    out = downscale_predictions(
        preds, mapping, cases, pd.Timestamp("2026-03-01"), 4, **_zone_kwargs()
    )
    m_active = out.loc[out["regionID"] == "m_active", "predictionZone"].iloc[0]
    assert m_active == 4


def test_assign_child_zones_is_per_region():
    # Two children, identical downscaled prediction, different history →
    # different zones (proves zone comes from the child, not a shared label).
    preds = pd.DataFrame(
        [
            {"regionID": "hi", "prediction": 50.0, "startDatePredictedWeek": "2026-03-08", "thresholdMethod": "prev_nweeks"},
            {"regionID": "lo", "prediction": 50.0, "startDatePredictedWeek": "2026-03-08", "thresholdMethod": "prev_nweeks"},
        ]
    )
    cases = pd.concat(
        [_flat_history(["hi"], 1), _flat_history(["lo"], 60)],
        ignore_index=True,
    )
    zones = assign_child_zones(
        preds, cases, pd.Timestamp("2026-03-01"), **_zone_kwargs()
    )
    # same prediction (50): well above hi's small baseline, around lo's large one
    assert zones.loc[preds["regionID"] == "hi"].iloc[0] > zones.loc[
        preds["regionID"] == "lo"
    ].iloc[0]


# ---------------------------------------------------------------------------
# #39 — explicit guardrails instead of silent fallbacks
# ---------------------------------------------------------------------------


def test_validate_cases_df_raises_on_empty():
    empty = pd.DataFrame(columns=["region_id", "date", "case_count"])
    with pytest.raises(ValueError, match="empty"):
        validate_cases_df(empty)


def test_validate_cases_df_raises_on_all_nat_dates():
    df = pd.DataFrame(
        {"region_id": ["m1"], "date": [pd.NaT], "case_count": [1]}
    )
    with pytest.raises(ValueError, match="date"):
        validate_cases_df(df)


def test_validate_cases_df_raises_on_some_unparseable_dates():
    df = pd.DataFrame(
        {
            "region_id": ["m1", "m2"],
            "date": ["2026-02-15", "not-a-date"],
            "case_count": [3, 4],
        }
    )
    with pytest.raises(ValueError, match="unparseable"):
        validate_cases_df(df)


def test_compute_shares_raises_on_nan_counts_in_window():
    cases = pd.DataFrame(
        [
            {"region_id": "m1", "date": pd.Timestamp("2026-02-15"), "case_count": float("nan")},
            {"region_id": "m2", "date": pd.Timestamp("2026-02-15"), "case_count": 5.0},
        ]
    )
    with pytest.raises(ValueError, match="[Nn]a[Nn]"):
        compute_shares(cases, "d1", ["m1", "m2"], pd.Timestamp("2026-03-01"), 4)


def test_compute_shares_warns_on_zero_window(caplog):
    cases = pd.DataFrame(
        [
            {"region_id": "m1", "date": pd.Timestamp("2026-02-15"), "case_count": 0},
            {"region_id": "m2", "date": pd.Timestamp("2026-02-15"), "case_count": 0},
        ]
    )
    with caplog.at_level(logging.WARNING):
        shares = compute_shares(cases, "d1", ["m1", "m2"], pd.Timestamp("2026-03-01"), 4)
    assert shares == {"m1": 0.5, "m2": 0.5}
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_compute_shares_warns_on_child_absent_from_cases(caplog):
    cases = pd.DataFrame(
        [{"region_id": "m1", "date": pd.Timestamp("2026-02-15"), "case_count": 10}]
    )  # m2 never appears at all
    with caplog.at_level(logging.WARNING):
        compute_shares(cases, "d1", ["m1", "m2"], pd.Timestamp("2026-03-01"), 4)
    assert "m2" in caplog.text


# ---------------------------------------------------------------------------
# Numeric sanity + diagnostics (parent↔child conservation, heterogeneity, risk)
# ---------------------------------------------------------------------------


def _child_row(region_id, prediction, week="2026-03-08", zone=0):
    return {
        "dateOfComputingPrediction": "2026-03-01",
        "startDatePredictedWeek": week,
        "regionID": region_id,
        "prediction": prediction,
        "thresholdMethod": "prev_nweeks",
        "predictionZone": zone,
        "model": "ensembleModel",
    }


def test_numeric_sanity_passes_for_conserving_split():
    parent = pd.DataFrame([_parent_row("d1", 100.0, 2)])
    child = pd.DataFrame([_child_row("m1", 60.0), _child_row("m2", 40.0)])
    check_numeric_sanity(parent, child, {"m1": "d1", "m2": "d1"})  # no raise


def test_numeric_sanity_conservation_violation_raises():
    parent = pd.DataFrame([_parent_row("d1", 100.0, 2)])
    child = pd.DataFrame([_child_row("m1", 40.0), _child_row("m2", 50.0)])  # sums 90
    with pytest.raises(ValueError, match="conserv"):
        check_numeric_sanity(parent, child, {"m1": "d1", "m2": "d1"})


def test_downscale_raises_on_nonfinite_parent_prediction():
    mapping = {"m1": "d1", "m2": "d1"}
    cases = _flat_history(["m1", "m2"], 5)
    preds = pd.DataFrame([_parent_row("d1", float("inf"), 2)])
    with pytest.raises(ValueError, match="finite"):
        downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), 4, **_zone_kwargs()
        )


def test_downscale_raises_on_negative_parent_prediction():
    mapping = {"m1": "d1", "m2": "d1"}
    cases = _flat_history(["m1", "m2"], 5)
    preds = pd.DataFrame([_parent_row("d1", -5.0, 2)])
    with pytest.raises(ValueError, match="negative"):
        downscale_predictions(
            preds, mapping, cases, pd.Timestamp("2026-03-01"), 4, **_zone_kwargs()
        )


def test_diagnostics_flags_uniform_split():
    # equal child predictions within the week → uniform (not data-driven)
    parent = pd.DataFrame([_parent_row("d1", 100.0, 2)])
    child = pd.DataFrame([_child_row("m1", 50.0), _child_row("m2", 50.0)])
    diag = downscale_diagnostics(parent, child, {"m1": "d1", "m2": "d1"})
    assert diag["n_parents_uniform"] == 1


def test_diagnostics_reports_parent_vs_child_risk():
    # parent zone 4, both children zone 0 → children below parent risk
    parent = pd.DataFrame([_parent_row("d1", 100.0, 4)])
    child = pd.DataFrame(
        [_child_row("m1", 60.0, zone=0), _child_row("m2", 40.0, zone=0)]
    )
    diag = downscale_diagnostics(parent, child, {"m1": "d1", "m2": "d1"})
    assert diag["n_weeks_children_below_parent"] == 1
    assert diag["n_weeks_children_above_parent"] == 0


# ---------------------------------------------------------------------------
# AP schema: parent CSV has one row per thresholdMethod, so (regionID,
# startDatePredictedWeek) is NOT unique (PR #43 review blocker).
# ---------------------------------------------------------------------------


def test_downscale_handles_duplicate_thresholdmethod_rows():
    mapping = {"m1": "d1", "m2": "d1"}
    cases = _flat_history(["m1", "m2"], 5)  # equal -> 50/50 split
    preds = pd.DataFrame(
        [
            _parent_row("d1", 100.0, 2, method="historical"),
            _parent_row("d1", 100.0, 3, method="previousNweeks"),
        ]
    )  # same (regionID, week), two thresholdMethods
    out = downscale_predictions(
        preds, mapping, cases, pd.Timestamp("2026-03-01"), 4, **_zone_kwargs()
    )
    # children are emitted per method; each method's children conserve to the parent
    for meth in ("historical", "previousNweeks"):
        s = out.loc[out["thresholdMethod"] == meth, "prediction"].sum()
        assert s == pytest.approx(100.0, abs=1e-9), meth
