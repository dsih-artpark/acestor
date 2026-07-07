"""Tests for the Largest Remainder Method helper (issues #83 + #86).

Invariants being enforced:
  1. sum(output) == round_half_up(sum(input))    — parent conservation
  2. each output_i ∈ {floor(v_i), floor(v_i)+1}  — off by at most one
  3. deterministic across runs                   — same input → same output
  4. tie-break: remainder desc → recent cases desc → region_id asc
     (per Prerna's ask on #86: "more real recent cases wins")
"""

from __future__ import annotations

import math
import random

import pytest

from pipelines.dengue_downscale.lib.apportionment import (
    largest_remainder,
    round_half_up,
)


# ---------------------------------------------------------------------------
# round_half_up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "x, expected",
    [
        (0.0, 0),
        (0.4, 0),
        (0.5, 1),  # half rounds up
        (1.4, 1),  # below half → down (would be 2 under ceil)
        (1.5, 2),
        (2.7, 3),
        (-0.3, 0),  # clamped to non-negative
    ],
)
def test_round_half_up(x, expected):
    assert round_half_up(x) == expected


# ---------------------------------------------------------------------------
# largest_remainder — basic
# ---------------------------------------------------------------------------


def test_docstring_example_04_03_02_01():
    """Parent raw sum = 1.0 → round_half_up = 1 → highest remainder wins."""
    out = largest_remainder([0.4, 0.3, 0.2, 0.1], ["a", "b", "c", "d"])
    assert sum(out) == 1
    assert out == [1, 0, 0, 0]


def test_target_uses_round_half_up_not_ceil():
    """Sum 1.4 stays 1 under round_half_up, would have been 2 under ceil."""
    out = largest_remainder([0.5, 0.5, 0.4], ["a", "b", "c"])
    # sum = 1.4; round_half_up(1.4) = 1 (below 0.5 fractional)
    assert sum(out) == 1
    # sum 1.5 rounds up:
    out2 = largest_remainder([0.5, 0.5, 0.5], ["a", "b", "c"])
    assert sum(out2) == 2


def test_exact_integer_input_passes_through():
    out = largest_remainder([2.0, 1.0, 3.0], ["r1", "r2", "r3"])
    assert out == [2, 1, 3]
    assert sum(out) == 6


def test_all_zeros_returns_all_zeros():
    out = largest_remainder([0.0, 0.0, 0.0, 0.0], ["a", "b", "c", "d"])
    assert out == [0, 0, 0, 0]


def test_single_child_gets_full_rounded_target():
    """[2.7] → round_half_up(2.7) = 3."""
    out = largest_remainder([2.7], ["only"])
    assert out == [3]
    # [2.4] → round_half_up(2.4) = 2 (would be 3 under ceil)
    out = largest_remainder([2.4], ["only"])
    assert out == [2]


def test_empty_input_returns_empty():
    assert largest_remainder([], []) == []


# ---------------------------------------------------------------------------
# Tie-break behaviour
# ---------------------------------------------------------------------------


def test_tie_break_prefers_higher_recent_cases_when_provided():
    """Prerna's ask: identical remainders → ward with more recent cases wins.

    raw=[0.6, 0.6] both have remainder 0.6, target=round_half_up(1.2)=1.
    recent_cases=[5, 40] → 'small' has 40 cases → 'small' wins the +1.
    """
    out = largest_remainder([0.6, 0.6], ["big", "small"], recent_cases=[5, 40])
    assert sum(out) == 1
    assert out == [0, 1]  # 'small' has higher recent cases


def test_tie_break_falls_through_to_region_id_when_no_cases_data():
    """No recent_cases → all default to 0 → alphabetical wins."""
    out = largest_remainder([0.5, 0.5], ["zeta", "alpha"])
    # remainder both 0.5, cases both 0 → alphabetical: alpha (index 1) wins
    assert out == [0, 1]


def test_tie_break_secondary_region_id_when_recent_cases_tied():
    """Recent cases also tied → alphabetical region_id wins."""
    out = largest_remainder([0.5, 0.5], ["zeta", "alpha"], recent_cases=[10, 10])
    assert out == [0, 1]  # alpha wins


def test_ulb_example_from_issue():
    """20 blocks × raw 0.15 → sum 3.0 → round_half_up = 3."""
    raws = [0.15] * 20
    ids = [f"block_{i:02d}" for i in range(20)]
    out = largest_remainder(raws, ids)
    assert sum(out) == 3
    assert all(v in (0, 1) for v in out)
    assert out.count(1) == 3


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_across_runs():
    raws = [0.4, 0.3, 0.2, 0.1, 0.5, 0.8]
    ids = ["a", "b", "c", "d", "e", "f"]
    cases = [10, 5, 20, 2, 15, 30]
    outs = [largest_remainder(raws, ids, recent_cases=cases) for _ in range(50)]
    assert all(o == outs[0] for o in outs)


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


def test_sum_invariance_random_inputs():
    """sum(int) == round_half_up(sum(raw)) for random inputs."""
    rng = random.Random(42)
    for _ in range(200):
        n = rng.randint(1, 30)
        raws = [rng.uniform(0, 10) for _ in range(n)]
        ids = [f"r{i}" for i in range(n)]
        cases = [rng.uniform(0, 50) for _ in range(n)]
        out = largest_remainder(raws, ids, recent_cases=cases)
        assert sum(out) == round_half_up(
            sum(raws)
        ), f"conservation broke for raws={raws}"


def test_off_by_at_most_one_random_inputs():
    """Each output_i is floor(v_i) or floor(v_i)+1."""
    rng = random.Random(43)
    for _ in range(200):
        n = rng.randint(1, 30)
        raws = [rng.uniform(0, 10) for _ in range(n)]
        ids = [f"r{i}" for i in range(n)]
        out = largest_remainder(raws, ids)
        for v, o in zip(raws, out):
            assert o in (
                math.floor(v),
                math.floor(v) + 1,
            ), f"child got {o} but floor(v)={math.floor(v)}, v={v}"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_length_mismatch_region_ids_raises():
    with pytest.raises(ValueError, match="length mismatch"):
        largest_remainder([1.0, 2.0], ["only-one"])


def test_length_mismatch_recent_cases_raises():
    with pytest.raises(ValueError, match="length mismatch"):
        largest_remainder([1.0, 2.0], ["a", "b"], recent_cases=[1.0])


def test_negative_value_raises():
    with pytest.raises(ValueError, match="negative value"):
        largest_remainder([1.0, -0.5, 2.0], ["a", "b", "c"])


def test_non_finite_value_raises():
    with pytest.raises(ValueError, match="non-finite"):
        largest_remainder([1.0, float("inf"), 2.0], ["a", "b", "c"])
    with pytest.raises(ValueError, match="non-finite"):
        largest_remainder([1.0, float("nan"), 2.0], ["a", "b", "c"])
