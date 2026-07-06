"""Tests for pipelines.dengue_rollup.lib.rollup.

Invariants being enforced:
  1. sum(child.predictionRaw) == parent.predictionRaw   (exact float conservation)
  2. sum(child.prediction)     == parent.prediction     (integer conservation)
  3. Post-#89 CSVs (prediction=int + predictionRaw=float) round-trip through
     rollup so the parent's own prediction is the SUM of children's ints —
     not round_half_up of the summed raws. This is why the same-week integers
     add up across levels in the dashboard.
  4. Pre-#89 CSVs (prediction=float, no predictionRaw) still work — the
     `prediction` column is treated as the raw, and the child int is derived
     via round_half_up for aggregation.
  5. Missing-parent behaviour: `error` raises, `warn` drops orphaned rows.
"""

from __future__ import annotations

import json
import math

import pandas as pd
import pytest

from pipelines.dengue_rollup.lib.rollup import (
    build_child_parent_mapping,
    rollup_predictions,
)


def _child_row(*, region, week, method, model, raw, disp_int):
    """Post-#89 acestor CSV shape: `prediction`=int, `predictionRaw`=float."""
    return {
        "dateOfComputingPrediction": "2026-07-06",
        "startDatePredictedWeek": week,
        "regionID": region,
        "prediction": disp_int,
        "predictionRaw": raw,
        "predictionInt": disp_int,
        "thresholdMethod": method,
        "predictionZone": 0,
        "model": model,
    }


def _pre89_row(*, region, week, method, model, prediction):
    """Pre-#89 acestor CSV shape: `prediction`=float, no predictionRaw column."""
    return {
        "dateOfComputingPrediction": "2026-07-06",
        "startDatePredictedWeek": week,
        "regionID": region,
        "prediction": prediction,
        "thresholdMethod": method,
        "predictionZone": 0,
        "model": model,
    }


# ---------------------------------------------------------------------------
# build_child_parent_mapping
# ---------------------------------------------------------------------------


def test_build_child_parent_mapping_reads_region_id_and_parent(tmp_path):
    (tmp_path / "z1.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "z1", "parent": "c1"},
                        "geometry": None,
                    }
                ],
            }
        )
    )
    (tmp_path / "z2.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "z2", "parent": "c1"},
                        "geometry": None,
                    }
                ],
            }
        )
    )
    (tmp_path / "z3.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "z3", "parent": "c2"},
                        "geometry": None,
                    }
                ],
            }
        )
    )
    m = build_child_parent_mapping(tmp_path)
    assert m == {"z1": "c1", "z2": "c1", "z3": "c2"}


def test_build_child_parent_mapping_skips_broken_files(tmp_path, caplog):
    (tmp_path / "empty.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": []})
    )
    (tmp_path / "missing_parent.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "z1"},
                        "geometry": None,
                    }
                ],
            }
        )
    )
    (tmp_path / "ok.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"region_id": "z2", "parent": "c1"},
                        "geometry": None,
                    }
                ],
            }
        )
    )
    m = build_child_parent_mapping(tmp_path)
    assert m == {"z2": "c1"}


# ---------------------------------------------------------------------------
# rollup_predictions — post-#89 CSV shape
# ---------------------------------------------------------------------------


def test_sum_invariant_post89_two_children_one_parent():
    """Simplest case: parent_int == sum(child_int); parent_raw == sum(child_raw)."""
    rows = [
        _child_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=3.4,
            disp_int=3,
        ),
        _child_row(
            region="z2",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=1.6,
            disp_int=2,
        ),
    ]
    child_preds = pd.DataFrame(rows)
    mapping = {"z1": "c1", "z2": "c1"}
    out = rollup_predictions(child_preds, mapping)

    assert len(out) == 1
    parent = out.iloc[0]
    assert parent["regionID"] == "c1"
    assert parent["predictionRaw"] == pytest.approx(5.0, abs=1e-9)
    # Parent int = sum of children ints (3 + 2 = 5), NOT round_half_up(5.0)=5
    # (those happen to be the same here — see next test for the case they differ).
    assert parent["prediction"] == 5
    assert parent["predictionInt"] == 5


def test_parent_int_is_sum_of_child_ints_not_round_of_sum():
    """The bug we hit in real data: individual rounds accumulate.

    Child raws: 2.4 (→ int 2) + 4.04 (→ int 4). Sum of raws = 6.44.
    round_half_up(6.44) = 6 — but children's ints sum to 6, so it agrees here.
    Try 2.4 + 4.03 = 6.43 → also 6.
    But 3.5 + 3.5 = 7.0, ints = 4 + 4 = 8, round_half_up(7.0) = 7 ← disagreement.
    """
    rows = [
        _child_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=3.5,
            disp_int=4,  # 3.5 rounds up to 4
        ),
        _child_row(
            region="z2",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=3.5,
            disp_int=4,
        ),
    ]
    out = rollup_predictions(pd.DataFrame(rows), {"z1": "c1", "z2": "c1"})
    parent = out.iloc[0]
    assert parent["predictionRaw"] == pytest.approx(7.0, abs=1e-9)
    # sum-of-child-ints (8) — preserves invariant sum(child.prediction) == parent.prediction
    assert parent["prediction"] == 8
    # round_half_up(7.0) would give 7 — that's the WRONG answer for our use case.
    # Confirm we DID NOT produce that:
    assert parent["prediction"] != math.floor(parent["predictionRaw"] + 0.5)


def test_multiple_parents_grouped_correctly():
    rows = [
        _child_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=1.0,
            disp_int=1,
        ),
        _child_row(
            region="z2",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=2.0,
            disp_int=2,
        ),
        _child_row(
            region="z3",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=3.0,
            disp_int=3,
        ),
        _child_row(
            region="z4",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=4.0,
            disp_int=4,
        ),
    ]
    mapping = {"z1": "c1", "z2": "c1", "z3": "c2", "z4": "c2"}
    out = rollup_predictions(pd.DataFrame(rows), mapping).sort_values("regionID")

    c1 = out[out["regionID"] == "c1"].iloc[0]
    c2 = out[out["regionID"] == "c2"].iloc[0]
    assert c1["prediction"] == 3 and c1["predictionRaw"] == pytest.approx(3.0)
    assert c2["prediction"] == 7 and c2["predictionRaw"] == pytest.approx(7.0)


def test_grouping_respects_week_method_and_model():
    """Same parent + different week/method/model → separate output rows."""
    rows = [
        _child_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=1.0,
            disp_int=1,
        ),
        _child_row(
            region="z1",
            week="2026-07-13",
            method="historical",
            model="ensembleModel",
            raw=2.0,
            disp_int=2,
        ),
        _child_row(
            region="z1",
            week="2026-07-06",
            method="previousNweeks",
            model="ensembleModel",
            raw=3.0,
            disp_int=3,
        ),
        _child_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="rf",
            raw=4.0,
            disp_int=4,
        ),
    ]
    out = rollup_predictions(pd.DataFrame(rows), {"z1": "c1"})
    assert len(out) == 4  # each (week, method, model) triple is its own group


def test_empty_input_returns_empty_dataframe_with_schema():
    out = rollup_predictions(pd.DataFrame(columns=["regionID"]), {})
    assert out.empty
    # All expected columns present, in order.
    assert list(out.columns) == [
        "dateOfComputingPrediction",
        "startDatePredictedWeek",
        "regionID",
        "predictionRaw",
        "prediction",
        "predictionInt",
        "thresholdMethod",
        "predictionZone",
        "model",
    ]


# ---------------------------------------------------------------------------
# rollup_predictions — pre-#89 CSV shape (backward compat)
# ---------------------------------------------------------------------------


def test_pre89_csv_prediction_treated_as_raw_float():
    """Older parent CSVs only have `prediction` = float. It should be treated
    as the raw, and per-child ints derived via round_half_up before summing."""
    rows = [
        _pre89_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            prediction=3.5,  # round_half_up → 4
        ),
        _pre89_row(
            region="z2",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            prediction=3.5,  # round_half_up → 4
        ),
    ]
    out = rollup_predictions(pd.DataFrame(rows), {"z1": "c1", "z2": "c1"})
    parent = out.iloc[0]
    assert parent["predictionRaw"] == pytest.approx(7.0, abs=1e-9)
    assert parent["prediction"] == 8  # 4 + 4


# ---------------------------------------------------------------------------
# on_missing_parents
# ---------------------------------------------------------------------------


def test_error_mode_raises_on_orphaned_child():
    rows = [
        _child_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=1.0,
            disp_int=1,
        ),
        _child_row(
            region="z_orphan",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=1.0,
            disp_int=1,
        ),
    ]
    with pytest.raises(ValueError, match="z_orphan"):
        rollup_predictions(pd.DataFrame(rows), {"z1": "c1"})


def test_warn_mode_drops_orphans_and_continues(caplog):
    import logging as _logging

    rows = [
        _child_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=1.0,
            disp_int=1,
        ),
        _child_row(
            region="z_orphan",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=99.0,
            disp_int=99,
        ),
    ]
    with caplog.at_level(_logging.WARNING):
        out = rollup_predictions(
            pd.DataFrame(rows), {"z1": "c1"}, on_missing_parents="warn"
        )
    assert len(out) == 1  # orphan dropped, only c1 emitted
    assert out.iloc[0]["regionID"] == "c1"
    assert out.iloc[0]["prediction"] == 1
    assert any("z_orphan" in r.message for r in caplog.records)


def test_invalid_on_missing_parents_value_raises():
    with pytest.raises(ValueError, match="on_missing_parents"):
        rollup_predictions(pd.DataFrame([]), {}, on_missing_parents="bogus")


# ---------------------------------------------------------------------------
# Column-level guarantees
# ---------------------------------------------------------------------------


def test_predictionzone_is_na_after_rollup():
    """Zone re-derivation happens in the step, not the lib. Lib must not carry
    child-level zones up (they're computed against child-level thresholds,
    meaningless at parent level)."""
    rows = [
        _child_row(
            region="z1",
            week="2026-07-06",
            method="historical",
            model="ensembleModel",
            raw=1.0,
            disp_int=1,
        ),
    ]
    # Give z1 a non-null child zone to prove rollup doesn't propagate it.
    df = pd.DataFrame(rows)
    df.loc[0, "predictionZone"] = 3
    out = rollup_predictions(df, {"z1": "c1"})
    assert pd.isna(out.iloc[0]["predictionZone"])
