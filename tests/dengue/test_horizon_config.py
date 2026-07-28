"""Tests for the configurable prediction horizon.

Covers:
* Three ``horizon_anchor`` modes (weather_cutoff, case_cutoff, today)
* The horizon-shortfall WARNING (data-anchor produces fewer weeks than expected)
* Config parsing / validation
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from pipelines.dengue.configs import CutoffConfig, HorizonConfig
from pipelines.dengue.lib.cutoffs import identify_cutoff_dates


# ---------------------------------------------------------------------------
# HorizonConfig parsing
# ---------------------------------------------------------------------------


def test_horizon_config_defaults_to_weather_cutoff():
    hc = HorizonConfig.from_raw({})
    assert hc.anchor == "weather_cutoff"
    assert hc.weeks == 4


def test_horizon_config_accepts_all_valid_anchors():
    for anchor in ("weather_cutoff", "case_cutoff", "today"):
        hc = HorizonConfig.from_raw({"anchor": anchor, "weeks": 4})
        assert hc.anchor == anchor


def test_horizon_config_rejects_unknown_anchor():
    with pytest.raises(ValueError, match="run.horizon.anchor must be one of"):
        HorizonConfig.from_raw({"anchor": "yesterday"})


def test_horizon_config_rejects_non_int_weeks():
    with pytest.raises(ValueError, match="run.horizon.weeks must be"):
        HorizonConfig.from_raw({"weeks": "four"})


def test_horizon_config_rejects_zero_weeks():
    with pytest.raises(ValueError, match=r"run.horizon.weeks must be >= 1"):
        HorizonConfig.from_raw({"weeks": 0})


def test_cutoff_config_carries_default_horizon_when_absent():
    cc = CutoffConfig.from_raw({})
    assert cc.horizon.anchor == "weather_cutoff"
    assert cc.horizon.weeks == 4


# ---------------------------------------------------------------------------
# identify_cutoff_dates — per-anchor behaviour
# ---------------------------------------------------------------------------


def test_weather_cutoff_anchor_matches_legacy_behavior():
    """Sanity: default (weather_cutoff) reproduces the legacy formula
    pred_upto = cutoff_weather + weeks*7d."""
    cutoff, pred_upto, dates = identify_cutoff_dates(
        cutoff_case=pd.Timestamp("2026-07-13"),
        cutoff_weather=pd.Timestamp("2026-07-20"),
        horizon_anchor="weather_cutoff",
        horizon_weeks=4,
    )
    assert cutoff == pd.Timestamp("2026-07-13")
    # weather + 28d = 08-17
    assert pred_upto == pd.Timestamp("2026-08-17")


def test_case_cutoff_anchor_produces_stable_horizon():
    """case_cutoff always emits exactly weeks*7d from the case cutoff,
    regardless of where weather is."""
    cutoff, pred_upto, _ = identify_cutoff_dates(
        cutoff_case=pd.Timestamp("2026-07-17"),
        cutoff_weather=pd.Timestamp("2026-07-10"),  # weather BEHIND case
        horizon_anchor="case_cutoff",
        horizon_weeks=4,
    )
    assert cutoff == pd.Timestamp("2026-07-17")
    assert pred_upto == pd.Timestamp("2026-08-14")  # case + 28d


def test_today_anchor_produces_current_week_plus_horizon_minus_1_weeks():
    """today emits `weeks` sampling-day slots starting from the sampling day
    of the current week."""
    # run_date is a Friday (2026-07-24), sampling_dayofweek=4 (Friday)
    _, pred_upto, dates = identify_cutoff_dates(
        cutoff_case=pd.Timestamp("2026-07-17"),
        cutoff_weather=pd.Timestamp("2026-07-10"),
        horizon_anchor="today",
        horizon_weeks=4,
        run_date=pd.Timestamp("2026-07-24"),
        sampling_dayofweek=4,  # Friday
    )
    assert pred_upto == pd.Timestamp("2026-08-14")
    assert dates == [
        "2026-07-24",
        "2026-07-31",
        "2026-08-07",
        "2026-08-14",
    ]


def test_today_anchor_aligns_to_sampling_weekday_when_run_date_off_weekday():
    """If run_date is a Wednesday but sampling is Friday, this-week anchor
    walks FORWARD to the coming Friday (the same calendar week's Friday)."""
    # run_date 2026-07-22 (Wednesday), sampling=Fri (4). Coming Fri = 07-24.
    _, pred_upto, dates = identify_cutoff_dates(
        cutoff_case=pd.Timestamp("2026-07-17"),
        cutoff_weather=pd.Timestamp("2026-07-10"),
        horizon_anchor="today",
        horizon_weeks=4,
        run_date=pd.Timestamp("2026-07-22"),  # Wednesday
        sampling_dayofweek=4,  # Friday
    )
    # Anchor Friday = 2026-07-24. pred_upto = 07-24 + 21d = 08-14.
    assert pred_upto == pd.Timestamp("2026-08-14")
    assert dates == [
        "2026-07-24",
        "2026-07-31",
        "2026-08-07",
        "2026-08-14",
    ]


def test_today_anchor_requires_run_date_and_dayofweek():
    with pytest.raises(ValueError, match="requires run_date and sampling_dayofweek"):
        identify_cutoff_dates(
            cutoff_case=pd.Timestamp("2026-07-17"),
            cutoff_weather=pd.Timestamp("2026-07-10"),
            horizon_anchor="today",
            horizon_weeks=4,
        )


# ---------------------------------------------------------------------------
# Horizon shortfall diagnostic WARNING
# ---------------------------------------------------------------------------


def test_horizon_shortfall_emits_warning_with_reason(caplog):
    """weather_cutoff anchor + weather behind case → shortfall → WARN with the
    reason spelled out (case_cutoff / weather_cutoff dates + gap)."""
    with caplog.at_level(logging.WARNING, logger="pipelines.dengue.lib.cutoffs"):
        identify_cutoff_dates(
            cutoff_case=pd.Timestamp("2026-07-17"),
            cutoff_weather=pd.Timestamp("2026-07-10"),  # 7 days behind case
            horizon_anchor="weather_cutoff",
            horizon_weeks=4,
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a horizon-shortfall WARNING"
    msg = warnings[0].getMessage()
    assert "3 target weeks" in msg  # actual
    assert "expected 4" in msg
    assert "weather is -7 days from case" in msg
    assert "anchor=case_cutoff" in msg or "anchor=today" in msg  # suggested fixes


def test_horizon_no_warning_when_full_weeks_emitted(caplog):
    """case_cutoff anchor always emits weeks*7d gap → no shortfall → no
    WARNING (just an INFO line)."""
    with caplog.at_level(logging.WARNING, logger="pipelines.dengue.lib.cutoffs"):
        identify_cutoff_dates(
            cutoff_case=pd.Timestamp("2026-07-17"),
            cutoff_weather=pd.Timestamp("2026-07-10"),
            horizon_anchor="case_cutoff",
            horizon_weeks=4,
        )
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
