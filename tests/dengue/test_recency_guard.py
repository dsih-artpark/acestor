"""Tests for the recency guard (#65) — part 1 of the silent-stale-run footgun fix.

Covers:
  * CaseSufficiencyConfig.max_staleness_days parsing + default.
  * check_data_recency raises when run_date is materially ahead of the most
    recent observed case date in prepared_data.
  * Same helper is silent when within the staleness budget.
  * max_staleness_days=0 (default) preserves legacy behaviour (no guard).
"""

from __future__ import annotations

import pandas as pd
import pytest

from pipelines.dengue.configs import CaseSufficiencyConfig
from pipelines.dengue.steps.load_prepared_case_data import check_data_recency


# ---------------------------------------------------------------------------
# CaseSufficiencyConfig.max_staleness_days
# ---------------------------------------------------------------------------


def test_max_staleness_days_defaults_to_zero():
    """0 = disabled. Preserves legacy behaviour for configs that don't set it."""
    cfg = CaseSufficiencyConfig.from_raw({})
    assert cfg.max_staleness_days == 0


def test_max_staleness_days_parses_int():
    cfg = CaseSufficiencyConfig.from_raw({"max_staleness_days": 14})
    assert cfg.max_staleness_days == 14


# ---------------------------------------------------------------------------
# check_data_recency
# ---------------------------------------------------------------------------


def _daily(last_date: str) -> pd.DataFrame:
    end = pd.Timestamp(last_date)
    dates = pd.date_range(end=end, periods=30)
    return pd.DataFrame({"date": dates, "case": 1})


def test_recency_guard_raises_when_run_date_is_too_far_ahead():
    """run_date 30 days past the last observed date with a 7-day budget → fail."""
    df = _daily("2026-05-01")
    run_date = pd.Timestamp("2026-05-31")
    with pytest.raises(ValueError, match="recency check failed"):
        check_data_recency(df, run_date, max_staleness_days=7)


def test_recency_guard_passes_when_within_budget():
    """run_date 5 days past the last observed date with a 7-day budget → ok."""
    df = _daily("2026-05-01")
    run_date = pd.Timestamp("2026-05-06")
    assert check_data_recency(df, run_date, max_staleness_days=7) == 5


def test_recency_guard_disabled_by_default():
    """max_staleness_days=0 → no check, even with very stale data."""
    df = _daily("2024-01-15")
    run_date = pd.Timestamp("2026-05-31")
    assert check_data_recency(df, run_date, max_staleness_days=0) is None


def test_recency_guard_handles_empty_dataframe():
    """No rows in prepared data → no signal → return None, don't raise."""
    df = pd.DataFrame({"date": [], "case": []})
    run_date = pd.Timestamp("2026-05-31")
    assert check_data_recency(df, run_date, max_staleness_days=7) is None


def test_recency_guard_run_date_exactly_at_max_observed():
    """0-day staleness with any positive budget → ok."""
    df = _daily("2026-05-01")
    run_date = pd.Timestamp("2026-05-01")
    assert check_data_recency(df, run_date, max_staleness_days=7) == 0


def test_recency_guard_threshold_boundary():
    """Staleness == threshold → ok (off-by-one guard)."""
    df = _daily("2026-05-01")
    run_date = pd.Timestamp("2026-05-08")  # exactly 7 days past
    assert check_data_recency(df, run_date, max_staleness_days=7) == 7
