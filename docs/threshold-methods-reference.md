# Threshold & Classification Methods — Reference

> **Audience:** Modelling team, backend engineers, config authors.  
> **Purpose:** One place to understand every threshold and classification method,
> where each one lives in the pipeline, and how to configure it.  
> Sources: PRISM-H Logic Reference, High Risk Districts SOP (Draft V3), codebase.

---

## The Short Version

There are **three distinct concepts** that all get called "thresholds." They answer different questions and run at different stages of the pipeline. Do not mix them up.

| Concept | Question answered | When it runs | Config key |
|---------|------------------|--------------|-----------|
| **WHO baseline** | Is this district unusual vs its own seasonal history? | Before predictions (`generate_thresholds` step) | `thresholds.methods` |
| **ICMR quartile** | Where does this district rank vs all other districts right now? | After predictions (`train_and_predict` step) | `thresholds.classification_method: icmr` |
| **SOP weighted baseline** | Does this district exceed its statistical control limit? | After predictions (not yet implemented) | — |

---

## 1. WHO Baseline Methods

**Source:** WHO EWAR guidance. Used for Karnataka and Odisha Risk Class. Also the fallback method when ICMR is not configured.

### 1a. `historical` — Same-week historical mean

For each district, for each date:

```
μ = mean of case counts from the same (month, weekday) in previous N years
σ = std dev of the same
```

Risk zones are then assigned based on where the prediction falls:

| Zone | Range | Label |
|------|-------|-------|
| 1 | 0 < prediction ≤ μ | Low |
| 2 | μ < prediction ≤ μ + σ | Moderate |
| 3 | μ + σ < prediction ≤ μ + 2σ | High |
| 4 | prediction > μ + 2σ | Very High (Outbreak) |

**Config knobs:**
```yaml
thresholds:
  historical_n_years: 4       # how many prior years to use (null = all)
  excluded_years: [2020, 2021]
  included_years: []          # non-empty = whitelist
  method_configs:
    historical:
      historical_n_years: 2   # per-method override
```

**Cold start:** If a district has no prior-year data for that week, Mean=NaN → district appears grey on maps. This is expected and correct.

---

### 1b. `prev_nweeks` — Rolling N-week fallback

Used when there is not enough historical data (e.g., first year of surveillance).

```
For each date, look back N weeks:
  col = 4-week moving average of case counts
  Mean = mean of col over 3 prior windows
  StdDev = std dev of col over 3 prior windows
```

**Config knobs:**
```yaml
thresholds:
  n_weeks: 4
  method_configs:
    prev_nweeks:
      n_weeks: 6   # per-method override
```

---

### How WHO methods fit in the pipeline

```
generate_thresholds step
  → reads case CSV
  → runs historical + prev_nweeks
  → writes datasets/thresholds/{region_type}_all_thresholds.csv
       columns: region_id | date | Mean | StdDev | threshold_method

train_and_predict step
  → reads threshold CSV
  → merges with predictions
  → computes T0, T2.0, T3.0 columns (Mean + α×StdDev)
  → assigns predictionZone 1–4 based on where prediction falls
       (only when classification_method = "who", which is the default)

assess_thresholds step
  → for each date, compares how many regions each method calls High/Very High
  → picks the best-performing threshold method
  → writes best_method_{region}_{date}.csv
```

Both methods always run and both appear as rows in the threshold CSV. `assess_thresholds` decides which one drives the final map on each date.

---

## 2. ICMR Quartile Classification

**Source:** ICMR "Threshold Range Categorization" doc. Used for Andhra Pradesh Risk Class and for High Risk Areas (all states).

### What it does

At each prediction date, look at all districts' predicted case counts simultaneously. Divide them into four equal strata by distinct value count:

```
distinct_values = sorted set of predicted case values, descending
values_per_stratum = ceil(len(distinct_values) / 4)

Top values_per_stratum → A1 Critical  → predictionZone = 4
Next values_per_stratum → A2 High     → predictionZone = 3
Next values_per_stratum → A3 Caution  → predictionZone = 2
Remainder               → A4 Low      → predictionZone = 1
```

**Key differences from WHO:**
- Cross-sectional (compares districts to each other), not temporal (comparing to historical self)
- Operates on predictions, not historical case data → runs after predictions exist
- "A1 Critical" in a quiet week ≠ "A1 Critical" in peak season — ICMR strata are not comparable across time
- Ties collapse: only distinct values are ranked

### Worked example (from PRISM-H §5.3)

26 AP districts. `ceil(26 / 4) = 7` per stratum.

| Stratum | Districts |
|---------|-----------|
| A1 Critical (zone 4) | Top 7 by predicted cases |
| A2 High (zone 3) | Next 7 |
| A3 Caution (zone 2) | Next 7 |
| A4 Low (zone 1) | Remaining 5 |

### Edge cases

- All districts have 0 predicted cases → all A4 (zone 1). Do not show Critical colours.
- Fewer than 4 distinct values → some strata will be empty. This is correct behaviour.
- Window with < 10 total cases across all districts → consider showing "Insufficient data" instead of forcing strata (not yet enforced in pipeline — open item).

### How ICMR fits in the pipeline

```
train_and_predict step
  → WHO zone assignment runs first (always, even for AP)
  → if classification_method = "icmr":
       icmr_quartile_zones() overrides predictionZone
       degenerate-threshold NaN check is skipped (not relevant for ICMR)
```

WHO thresholds still run in `generate_thresholds` even when ICMR is enabled — they feed `assess_thresholds` and drive the grey-region logic (Mean=NaN → no historical data → grey on map). That behaviour is separate from the predictionZone colour.

### Config

```yaml
thresholds:
  classification_method: icmr   # who (default) | icmr
```

---

## 3. SOP Weighted Baseline (not yet implemented)

**Source:** High Risk Districts SOP Draft V3. Describes the operational procedure for identifying high-risk districts in the weekly report.

### Algorithm

```
Weighted Mean = 0.7 × mean(cases over last 4 weeks)
              + 0.3 × mean(cases for same epi-weeks last year)

SD = std dev of cases over the past 8 weeks (rolling ~2 months)

UCL = Weighted Mean + n × SD
```

Classification:
- `prediction > UCL` → **High Risk**
- Otherwise → **Normal / Monitoring**

This is a 2-class output (High Risk vs not), not 4-tier.

### Open questions before implementing

1. **n = 2 or 3?** Section 4.6 of the SOP says `UCL = Mean + 2×SD`. Section 5 says `UCL = Weighted Mean + 3×SD`. These are inconsistent — clarify with the team before implementing.
2. **"Same epi-weeks last year"**: does this mean the exact same ISO week numbers (e.g., weeks 16–19 of last year), or exactly 52 weeks back (same 4-week window shifted back by 364 days)? These can diverge by a week around year-end.
3. **Does this replace historical/prev_nweeks for AP, or is it additive?** The SOP covers operational High Risk District reporting; PRISM-H covers dashboard Risk Class. They may be parallel outputs from the same pipeline run.
4. **Is this what AP actually uses for the weekly SOP reporting, or is ICMR their live method?** Worth confirming with state DHS.

### Planned implementation

When the above questions are resolved, `weighted_baseline` will be a new registered method:

```python
@register("weighted_baseline")
def _weighted_baseline_method(df, ctx):
    return weighted_baseline_threshold_params(
        df,
        recent_weeks=ctx.recent_weeks,     # default 4
        sd_window_weeks=ctx.sd_window_weeks,  # default 8
        weight_recent=ctx.weight_recent,   # default 0.7
        weight_seasonal=ctx.weight_seasonal,  # default 0.3
        n_sigma=ctx.n_sigma,              # 2 or 3 — TBD
    )
```

YAML config (when implemented):
```yaml
thresholds:
  methods: [historical, prev_nweeks, weighted_baseline]
  method_configs:
    weighted_baseline:
      recent_weeks: 4
      sd_window_weeks: 8
      weight_recent: 0.7
      weight_seasonal: 0.3
      n_sigma: 3   # confirm with team
```

---

## 4. State Assignment Summary

From PRISM-H Logic Reference §3:

| State | Risk Class method | Levels | Notes |
|-------|------------------|--------|-------|
| Andhra Pradesh | **ICMR quartile** | A1 Critical · A2 High · A3 Caution · A4 Low | `classification_method: icmr` |
| Karnataka | **WHO historical** | Low · Moderate · High · Very High | default `classification_method: who` |
| Odisha | **WHO historical** | Low · Moderate · High · Very High | default `classification_method: who` |

**High Risk Areas (all three states):** ICMR A1 ∪ A2 AND Trend = Rising. This is a separate output from Risk Class (used on the Overview tab, not Forecast tab). Not yet implemented as a pipeline step.

**Trend arrows (all surfaces):** `ratio = mean(last 2W) / mean(prior 2W)`. Rising ≥ 1.20, Falling ≤ 0.80, Stable otherwise. Not yet implemented.

---

## 5. Pipeline Flow (full picture)

```
generate_thresholds
  Inputs:  case CSV
  Runs:    WHO methods listed in thresholds.methods (historical, prev_nweeks)
           ICMR is NOT listed here — it needs predictions, not historical data
  Outputs: datasets/thresholds/{region}_all_thresholds.csv
           columns: region_id | date | Mean | StdDev | threshold_method

train_and_predict
  Inputs:  case CSV, weather CSV, threshold CSV, model weights
  Runs:    train + predict (NBR / RF / XGB / ensemble)
           WHO zone assignment via zones.merge_predictions_thresholds()
           if classification_method=icmr: override predictionZone with ICMR quartile
  Outputs: results/Predictions_{month}_{region}_{date}.csv
           columns: regionID | prediction | predictionZone | thresholdMethod | Mean | StdDev | ...

assess_thresholds
  Inputs:  predictions CSV (all threshold methods stacked as rows)
  Runs:    for each date, compares Risk_Zone_Sum per method → picks best WHO method
  Outputs: dumps/best_method_{region}_{date}.csv
  Note:    when classification_method=icmr, predictionZone is ICMR-based,
           but thresholdMethod rows still exist for each WHO method —
           assess_thresholds still runs and picks best WHO method for the grey-region logic

generate_maps + generate_report
  Inputs:  best_method CSV, predictions CSV, geojson
  Renders: maps coloured by predictionZone (1=green, 2=yellow, 3=orange, 4=red)
```

---

## 6. Quick Config Reference

### AP district (ICMR, recommended for AP runs)
```yaml
thresholds:
  region_type: district
  n_weeks: 4
  historical_n_years: 4
  excluded_years: []
  included_years: []
  methods: [historical, prev_nweeks]
  classification_method: icmr
```

### Karnataka / Odisha (WHO, default)
```yaml
thresholds:
  region_type: district
  n_weeks: 4
  historical_n_years: 4
  excluded_years: []
  included_years: []
  methods: [historical, prev_nweeks]
  # classification_method defaults to "who"
```

### Per-method overrides (optional)
```yaml
thresholds:
  ...
  method_configs:
    prev_nweeks:
      n_weeks: 6          # use 6-week window instead of 4
    historical:
      historical_n_years: 2
      excluded_years: [2020, 2021]
```

---

## 7. Open Items

| # | Item | Blocks |
|---|------|--------|
| 1 | SOP: is UCL = Mean + 2σ or Mean + 3σ? Section 4.6 and Section 5 disagree. | `weighted_baseline` implementation |
| 2 | SOP: "same epi-weeks last year" — ISO week numbers or 52-week offset? | `weighted_baseline` implementation |
| 3 | Is SOP weighted baseline for AP operational reporting, or is ICMR the live method? Are they parallel? | Knowing whether `weighted_baseline` produces a separate output or replaces WHO risk classification |
| 4 | ICMR: window < 10 total cases across all districts — should pipeline emit "Insufficient data" instead of strata? (PRISM-H §5.4) | ICMR edge case handling |
| 5 | High Risk Areas (ICMR A1+A2 AND Trend=Rising) not yet implemented as a pipeline step | PRISM-H Overview tab |
| 6 | Trend arrows (Δ% test) not yet implemented | PRISM-H everywhere |
| 7 | WHO baseline N for Odisha: confirm it is 4 years like Karnataka (PRISM-H open item #3) | Odisha config |
