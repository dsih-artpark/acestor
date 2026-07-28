# Threshold & Classification Methods — Reference

> **Audience:** modelling team, backend engineers, config authors.
> **Purpose:** one place to look up every threshold-generation method and
> every classification method, the exact formula each one uses, and the
> config keys that drive them.
> **Sources of truth:** `pipelines/dengue/lib/thresholds.py`,
> `pipelines/dengue/lib/zones.py`, PRISM-H Logic Reference,
> High Risk Districts SOP (Draft V3).

---

## Two independent knobs

The word "threshold" gets overloaded. In this codebase there are two
independent knobs:

1. **Threshold generation** (`thresholds.methods`) — how do we compute a
   per-region `Mean` and `StdDev`? Three methods available:
   `historical`, `prev_nweeks`, `weighted_baseline`. You can configure
   more than one; every configured method produces its own row in the
   long-form thresholds table.
2. **Classification** (`thresholds.classification_method`) — given the
   `Mean`, `StdDev`, and a raw prediction, which discrete risk band does
   the region land in? Four methods: `who`, `icmr`, `percentile`, plus
   the default WHO band that runs unconditionally.

The two knobs compose. `historical + icmr` and `weighted_baseline + who`
are both legal.

---

## Threshold generation methods

All three methods produce the same output schema: one row per `(region,
date, method)` with columns `Mean`, `StdDev`, `threshold_method`. The
Mean/StdDev are combined with `list_alpha` at merge-time to produce the
`T{alpha}` cut-points in `predictions.csv`.

`list_alpha` (default `[1.0, 2.0]`) is a list of standard-deviation
multipliers. Every configured α yields one `T{α}` column:

```
T_α = Mean + α · StdDev + i · 1e-6
```

where `i` is the α's 1-based position in the list — the epsilon nudge
guarantees strict monotonicity even when Mean/StdDev collapse to zero.
The α=0 case (`T0.00`) is always added implicitly so binning starts at
the Mean itself.

### `historical`

**Question:** "how does this week compare to the same week in past
years?"

For each `(region, year, month, weekday)`, take all past rows with the
same `(month, weekday)` from earlier years and compute:

```
Mean_hist(r, year, m, wd)   = mean(case[r, y, m, wd]  for y < year)
StdDev_hist(r, year, m, wd) = std(case[r, y, m, wd]   for y < year)
```

Filters (all optional):

* `historical_n_years` — cap the look-back window at N years.
* `included_years` — restrict to a whitelist.
* `excluded_years` — drop specific years (e.g. drop COVID-affected 2020).

If no years remain after filtering, `Mean` and `StdDev` come out `NaN`
and the module logs a warning listing every affected `(year, region)`.
Those rows survive downstream but paint light grey on maps and end up
with `predictionZone = 0` after the sentinel fill.

**When to use:** the region has ≥ 3 clean years of past data, the
seasonal cycle is real, and outbreak years don't dominate the history.

### `prev_nweeks`

**Question:** "how does the incoming prediction compare to the last
handful of weeks?"

Two-stage rolling statistic, computed per region on the weekly case
series:

```
ν_t  = mean(case_{t-1}, case_{t-2}, …, case_{t-n_weeks})   # excludes current week
μ_t  = mean(ν_t, ν_{t-1}, ν_{t-2})                          # 3-value smoother
σ_t  = std(ν_t, ν_{t-1}, ν_{t-2}, ν_{t-3})                  # 4-value spread
```

The stride between "weeks" is 7 days (`k = 7`); `n_weeks` defaults to 4.
`Mean = μ_t`, `StdDev = σ_t`.

**When to use:** the region has short or noisy history but a fresh
weekly time-series; you want thresholds that track recent conditions
rather than an old seasonal average.

### `weighted_baseline` (SOP-style)

**Question:** "blend the last few weeks with what happened at this time
of year last year."

```
recent_mean(t)    = mean(case_{t-i} for i in 0..recent_weeks-1)
seasonal_mean(t)  = mean(case_{t-52-i} for i in 0..recent_weeks-1)
Mean(t)           = weight_recent · recent_mean + weight_seasonal · seasonal_mean
StdDev(t)         = std(case_{t-i} for i in 0..sd_window_weeks-1, ddof=1)
```

Defaults: `recent_weeks=4`, `sd_window_weeks=8`, `weight_recent=0.7`,
`weight_seasonal=0.3`. When seasonal data is missing (cold start),
`Mean` falls back to `recent_mean` alone; when recent is missing but
seasonal is present, the reverse. Both missing → `NaN`.

**When to use:** the SOP requires blending recency with seasonality; you
have ≥ 52 weeks of history but the seasonal signal isn't clean enough
for pure `historical`.

### Preference order (assessment)

`assess_thresholds` picks a "best" method per prediction date using this
priority (PRISM-H §4.2):

```
historical  →  previousNweeks  →  weightedBaseline
```

The first method with a non-null, non-zero `Mean` wins. If none qualify,
the first available row is returned as a graceful-degrade fallback.

---

## Config-key breakdown

```yaml
thresholds:
  methods: [historical, prev_nweeks]        # generation methods to compute
  list_alpha: [1.0, 2.0]                    # α multipliers → T-columns
  classification_method: who                # who | icmr | percentile
  percentile_cutoffs: [50, 75, 90]          # only used when method=percentile
  method_configs:
    historical:
      historical_n_years: 5
      excluded_years: [2020, 2021]
      included_years: []                    # empty = "no whitelist"
    prev_nweeks:
      n_weeks: 4
    weighted_baseline:
      recent_weeks: 4
      sd_window_weeks: 8
      weight_recent: 0.7
      weight_seasonal: 0.3
```

Only the methods listed in `methods` get executed. `method_configs`
entries for other methods are ignored. Every method listed in `methods`
must have its `ThresholdContext` fields resolvable (defaults are
supplied per method — you never need to fill everything).

---

## Classification methods

The three classification columns (`whoZone`, `icmrZone`,
`percentileZone`) are **always all populated**. `predictionZone` is a
copy of whichever one `thresholds.classification_method` selects, with
`NA` values sentinel-filled to `0`.

### WHO band (default)

`pipelines/dengue/lib/zones.py::assign_zone`. Given the T-columns for a
row, build half-open intervals `[Zero, T0.00), [T0.00, T1.00), …, [T2.00,
Inf)` and place `predictionRaw` into one:

```
zone = 1 + count(T_i < predictionRaw for T_i in [Zero, T0.00, T1.00, …])
```

With default `list_alpha: [1.0, 2.0]` this yields 4 bands. Degenerate
rows (`Mean == 0 and StdDev == 0`) are set to `NA` — no meaningful band
can be assigned.

**Uses:** `Mean + α · StdDev` cut-points → answers "is this prediction
statistically unusual for this region under this baseline?"

### ICMR quartile

`pipelines/dengue/lib/thresholds.py::icmr_quartile_zones`.
Cross-sectional per date. For each `startDatePredictedWeek`:

1. Take the distinct values of `predictionRaw` across all regions.
2. Rank descending, split into 4 equal strata of size
   `ceil(n_distinct / 4)`.
3. Map: top stratum → 4 (A1 Critical), next → 3 (A2 High),
   next → 2 (A3 Caution), last → 1 (A4 Low).

Guard (PRISM-H §5.4): if the total predicted caseload for the date is
< 10, every region on that date gets `icmrZone = NA` and downstream
consumers render this as "Insufficient data".

**Uses:** cross-region ranking → answers "which districts are the
highest risk *right now* relative to the others?"

### Percentile

`pipelines/dengue/lib/thresholds.py::percentile_historical_zones`
(PR #73, issue #62). Per-region, using the region's own weekly-aggregated
case history:

```
cuts_r = np.percentile(weekly_cases_r, percentile_cutoffs)
band(pred) = 1 + searchsorted(cuts_r, pred, side='right')
```

`N` percentile cutoffs produce `N + 1` bands. The default `[50, 75, 90]`
gives four bands: `≤ p50`, `(p50, p75]`, `(p75, p90]`, `> p90`. Regions
with no history → `NA`.

Weekly aggregation is not optional — see PR #98 in
`thresholds-explained.md`.

**Uses:** self-referential ranking → answers "is this prediction high
compared to this region's own past?"

### Sentinel fill

After the selected classification is copied into `predictionZone`, any
remaining `NA` is filled with `0`. Dashboards render zone 0 as neutral
grey and the pipeline logs the affected region list at `WARNING`. `0` is
distinguishable from a valid band (which always starts at `1`).

---

## Real values (AP district, ensemble)

From `artifacts/ap/ap-repro-fixed/outputs/predictions.csv`:

| regionID | thresholdMethod | Mean  | StdDev | T0.00 | T1.00 | T2.00 | predictionRaw | whoZone | icmrZone | percentileZone |
|----------|-----------------|-------|--------|-------|-------|-------|---------------|---------|----------|----------------|
| district_502 | previousNweeks   | 1.667 | 0.479  | 1.667 | 2.145 | 2.624 | 1.566 | 1 | 3 | 2 |
| district_502 | weightedBaseline | 1.000 | 1.488  | 1.000 | 2.488 | 3.976 | 1.566 | 2 | 3 | 2 |

The two rows are the same `(region, week, prediction)` but different
`thresholdMethod`, so the T-columns and `whoZone` differ. `icmrZone`
and `percentileZone` are identical across the two rows because they
don't depend on Mean/StdDev at all — one is cross-sectional, one is
based on the region's own historical distribution.
