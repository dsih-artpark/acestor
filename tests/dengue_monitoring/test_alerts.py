from __future__ import annotations


import pandas as pd
import pytest

from pipelines.dengue_monitoring.metrics import baseline, compare


from .helpers import observations


def test_alerts_equality_negative_flags_and_invalid_breaks():
    reference = baseline(observations([0, 0.1, 0.2, 0.3]))
    current = observations([0.2, 0.3, 0.4, float("nan"), 0.4, -0.1, -0.2, 0.4, 0.4])
    out = compare(current, reference)
    assert out.upper_flag.tolist() == [
        False,
        True,
        True,
        False,
        True,
        False,
        False,
        True,
        True,
    ]
    assert out.alert.tolist() == [
        False,
        False,
        True,
        False,
        False,
        False,
        False,
        False,
        True,
    ]
    assert out.underprediction_flag.sum() == 2


def test_absent_calendar_week_breaks_streak():
    out = compare(
        observations([1, 1], ["2025-01-07", "2025-01-21"]), baseline(observations([0]))
    )
    assert not out.alert.any()


def test_negative_p75_current_behaviour_pending_policy_review():
    # Characterises existing behaviour; the negative-p75 policy decision is deferred.
    out = compare(observations([-0.1, -0.1]), baseline(observations([-0.5, -0.4])))
    assert out.upper_flag.all() and out.underprediction_flag.all()
    assert out.alert.tolist() == [False, True]


def test_regions_models_scales_scopes_and_leads_do_not_share_sequences():
    base = observations([1])
    parts = [base]
    for column, value in (
        ("scope", "other"),
        ("spatial_scale", "zone"),
        ("model", "xgb"),
        ("lead", 2),
        ("region_id", "002"),
        ("derivation", "rollup"),
    ):
        parts.append(
            base.assign(**{column: value, "target_end": pd.Timestamp("2025-01-14")})
        )
    current = pd.concat(parts)
    ref = baseline(current.assign(signed_error=0))
    assert not compare(current, ref).alert.any()
    assert len(ref) == 6  # regions alone pool into a shared reference


def test_no_baseline_still_flags_underprediction_and_duplicates_rejected():
    current = observations([-0.1]).assign(model="missing")
    out = compare(current, baseline(observations([0])))
    assert not out.baseline_available.any() and out.underprediction_flag.all()
    with pytest.raises(ValueError, match="Multiple selected"):
        compare(pd.concat([current, current]), baseline(observations([0])))


def test_streak_continues_then_resets_and_restarts_in_date_order():
    current = observations([1, 1, 1, 0, 1, 1]).iloc[::-1]
    out = compare(current, baseline(observations([0])))
    assert out.consecutive_upper_weeks.tolist() == [1, 2, 3, 0, 1, 2]
    assert out.alert.tolist() == [False, True, True, False, False, True]


def test_sustained_underprediction_does_not_escalate():
    out = compare(observations([-0.1, -0.2, -0.3, -0.4]), baseline(observations([0])))
    assert out.underprediction_flag.all()
    assert not out.upper_flag.any() and not out.alert.any()
    assert out.consecutive_upper_weeks.eq(0).all()


def test_separate_executions_do_not_accumulate_a_streak():
    reference = baseline(observations([0]))
    first = observations([1], ["2025-01-07"])
    second = observations([1], ["2025-01-14"])
    assert not compare(first, reference).alert.any()
    assert not compare(second, reference).alert.any()
    assert compare(pd.concat([first, second]), reference).alert.tolist() == [
        False,
        True,
    ]
