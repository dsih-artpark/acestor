# Understanding Thresholds — From First Principles

> This document builds intuition from scratch. It is the "why" companion
> to `threshold-methods-reference.md`, which is the "how".
> Each section only adds one new idea. No config keys until you need
> them; no formulas until they help.

---

## Part 1 — the core problem

### Imagine you are a district health officer

It is Monday morning. Your surveillance system reports:

> **Kurnool district: 50 dengue cases this week.**

Is that bad? Should you act?

You cannot answer that question with just the number 50. You need
context. Specifically, you need to know **what "normal" looks like for
Kurnool in this week of the year**, so you can decide whether 50 is a
warning shot or Tuesday.

That is what a threshold is: a number that separates "normal for here,
now" from "not normal — investigate". Everything else on this page is
mechanics.

### Why a single global number will not do

You cannot pick one national number ("more than 50 cases = emergency").
50 cases in a rural sub-district with 40,000 residents is a crisis; 50
cases in urban Bengaluru is a slow Tuesday. Population, seasonality,
elevation, prior-year outbreak history — all of it means the threshold
has to be **per region** and **per date**, not global.

The dashboard needs a threshold for every `(region, week)` cell it
colours. Thousands of them. So we compute them programmatically from
history, not by hand.

---

## Part 2 — from a threshold to a coloured cell

The dashboard does not show you thresholds. It shows you **coloured
bands**. Green / yellow / orange / red. Each colour maps to a range of
prediction values, and the ranges are defined by the thresholds.

So the pipeline actually produces two things every week:

1. **A raw prediction** — a floating-point number, e.g. `predictionRaw =
   1.566` cases for `district_502` in the forecast week.
2. **A risk zone** — an integer band `1..N` that says "this prediction
   sits in the green / yellow / orange / red range". This is the
   `predictionZone` column.

The threshold values are the boundaries between the bands. Something
has to compute them. That "something" is one of the three
threshold-generation methods (`historical`, `prev_nweeks`,
`weighted_baseline` — see the reference doc).

---

## Part 3 — three ways to define "normal"

There is no single correct definition of "normal for this region right
now". Three legitimate answers, each answering a slightly different
question:

### (a) "Same week, past years" — the `historical` method

Look at the same calendar week in previous years, take the mean and
standard deviation. That is your baseline. This is what public-health
handbooks usually mean by a threshold.

**Best when:** you have several clean years of history and the seasonal
cycle is real.

**Fails when:** the last five years include one enormous outbreak that
inflates the "normal" bar. Excluded-year config lets you drop that.

### (b) "Recent weeks, this year" — the `prev_nweeks` method

Ignore history. Just look at the last handful of weeks in this same
region. Compute a rolling mean and rolling std.

**Best when:** history is short, dirty, or the disease dynamics have
shifted (new serotype, new vector control regime).

**Fails when:** the disease is intrinsically seasonal — using July to
"predict" August is fine, but using December cases to set thresholds for
July will always underestimate the summer normal.

### (c) "Blend the two" — the `weighted_baseline` method

Weighted average of "recent weeks" (default 70%) and "same weeks last
year" (default 30%). This is the SOP-recommended approach: recency
dominates but seasonality still gets a vote.

**Best when:** the SOP says so, or you have ≥ 1 year of history and
the seasonal signal is present but noisy.

**Fails when:** the seasonal window is dominated by an anomaly year and
you have not filtered it out.

The pipeline lets you run more than one method in the same run — every
threshold-generation method is computed for every `(region, date)`. Only
one method is used to derive the final `whoZone`, but the others are
retained in the long-form thresholds table for backtesting.

---

## Part 4 — from threshold to risk band

Suppose the pipeline has computed, for `(district_502, 2026-03-17,
previousNweeks)`:

```
Mean   = 1.667
StdDev = 0.479
```

We construct three cut-points at α ∈ `{0, 1, 2}` — the standard-deviation
multipliers listed in `list_alpha`:

```
T0.00 = Mean + 0·StdDev = 1.667
T1.00 = Mean + 1·StdDev = 2.145
T2.00 = Mean + 2·StdDev = 2.624
```

The predicted value is `predictionRaw = 1.566`. That falls in `[Zero,
T0.00)` — below the mean itself — so `whoZone = 1` (green: "no unusual
signal, below the baseline").

If the same region had a prediction of 3.0, it would fall in `[T2.00,
Inf)` and get `whoZone = 4` (red: "more than 2σ above baseline").

The "α" values (`list_alpha`) are how you tune sensitivity. Smaller α =
tighter bands = more red cells. Public-health teams generally negotiate
`list_alpha` empirically until the volume of red cells matches what
their surveillance team can actually investigate.

---

## Part 5 — three different questions, three different colourings

The pipeline actually paints the dashboard **three ways** and writes all
three into `predictions.csv`. Which one appears in `predictionZone`
depends on `classification_method`.

### `whoZone` — "unusual for this region?"

Uses the Mean/StdDev thresholds from Part 4. Answers a *within-region*
question.

### `icmrZone` — "highest risk right now compared to peers?"

For each week, rank every region's prediction. Cut into 4 equal strata.
Top stratum = red (A1 Critical), bottom = green (A4 Low). Answers a
*cross-region* question — no thresholds needed.

Useful when you have limited response capacity and need to prioritise
between districts *this week*, regardless of whether any of them are
individually "unusual".

Guard: if the total predicted caseload across all regions on a given
week is < 10, ICMR zones for that week are `NA` and the dashboard
renders "insufficient data" — ranking noise around zero would be
meaningless.

### `percentileZone` — "high vs this region's own past?"

For each region, compute percentiles of its own historical weekly case
totals (default `p50, p75, p90` → 4 bands). Place the prediction in
the appropriate band.

Answers a *self-referential* question that is more robust than
`historical` when the historical distribution is heavy-tailed: instead
of Mean + α·StdDev (which is sensitive to a single outbreak year),
`percentile` uses order statistics that survive one big year cleanly.

---

## Part 6 — the aggregation-consistency trap (PR #98)

`percentileZone` was originally computed off `cases_daily.csv` directly.
That looked fine but produced badly-inflated bands. The bug:

`cases_daily.csv` is **sparse** — rows exist only for days that had at
least one case. Days with zero cases are simply absent. Computing
`p50` over those rows gives you the median of positive-case days —
a *conditional-on-having-cases* statistic. When a region's median
non-zero day is 3 cases, that becomes the p50 cutoff — and any weekly
prediction ≥ 3 lands above p50.

The fix: aggregate to **weekly totals per region** first, then compute
percentiles on that. A weekly total is comparable to a weekly
prediction; a daily conditional-on-hot-day total is not.

```python
weekly = cases.groupby([region, week]).sum()
cuts   = np.percentile(weekly[case_col].dropna(), percentile_cutoffs)
```

This is now baked into `percentile_historical_zones` — you don't need
to think about it, but if you're reading old code and wondering why
p50 cuts look strange, that's the reason.

---

## Part 7 — when thresholds go bad

Not every `(region, date)` produces a meaningful threshold. Three
degenerate cases show up in production:

### The all-zeros region

A region has been reporting zero cases for years — genuinely, or because
reporting has broken down and we can't distinguish the two.
`Mean = 0`, `StdDev = 0`. Every threshold `T_α = 0`. Everything gets
classified as "above the baseline" including a prediction of 0.1. This
is meaningless.

**Handling:** `train_and_predict` explicitly detects `Mean == 0 and
StdDev == 0` and sets `whoZone = NA` for those rows. A `WARNING` log
lists the affected regions.

### The `NaN` region

Historical method with `historical_n_years = 5` and only 3 years of
data → past window is empty → `Mean = NaN, StdDev = NaN`. `T_α` also
becomes NaN. Any comparison with the prediction is false, so no zone is
assigned.

**Handling:** the thresholds module logs a `WARNING` at the point of
detection so you see exactly which `(year, region)` combinations lack
usable history.

### The out-of-band prediction

The prediction happens to fall exactly at `T_α` — the half-open
intervals `[T_i, T_{i+1})` are strict, and the epsilon nudge
(`+ i · 1e-6`) prevents this in practice, but there's still an edge case
where `predictionRaw` sits below `Zero` (rare, but possible if a model
returns a negative value that survives the pipeline). No interval
matches → no zone assigned.

**Handling (all three cases):** after the classification step, any
`predictionZone` that is still `NA` gets filled with the sentinel `0`.
`0` is not a valid band (bands start at 1), so the dashboard renders it
as neutral grey and the run logs a `WARNING`:

```
train_and_predict[ensemble]: 3 region(s) have predictionZone=0
  (prediction fell outside all threshold pairs): ['district_501', …]
```

If you see zone-0 regions in a production run, that is not a rendering
bug — it is the pipeline correctly signalling that thresholds were
degenerate for those regions. Fix upstream (extend history, exclude
outbreak years, switch method) rather than papering over it.

---

## Part 8 — how to choose

A rough decision tree:

* **≥ 5 clean years of history, seasonal disease** →
  `historical`, optionally with `excluded_years` for outbreak years.
  Classification: `who`.
* **Short or noisy history, weekly signal is good** → `prev_nweeks`.
  Classification: `who`.
* **SOP requires it** → `weighted_baseline`. Classification: `who`.
* **Response capacity is the binding constraint; you must prioritise
  between regions** → any generation method, classification `icmr`.
* **Heavy-tailed history, one big outbreak year dominates** →
  classification `percentile` (with any generation method — it doesn't
  actually use Mean/StdDev).

You can (and often should) compute more than one classification. All
three parallel columns are always written to `predictions.csv`; only
`predictionZone` is the "official" one. Analysts can compare
`whoZone` vs `icmrZone` vs `percentileZone` after the fact to see where
methods agree and where they diverge.
