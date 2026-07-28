# Downscale Pipeline

The `dengue_downscale` pipeline takes a completed dengue forecast at some parent spatial level (district, ULB) and disaggregates it to a finer child level (mandal, block, ward, subdistrict) using observed case shares from the child geography.

Entry point: `pipelines.dengue_downscale.pipeline:build_pipeline`.

DAG:

```
load_predictions → downscale_predictions ─┬─ generate_downscale_maps
                                          └─ generate_downscale_brief
```

---

## What it does

For each parent-week prediction, it multiplies the parent's raw prediction by each child's share of recent observed cases within that parent, producing one row per (child, week, thresholdMethod, model). Zones are then **re-derived from child-level case history** (never inherited from the parent), and integer allocations are apportioned via the Largest Remainder Method so that `sum(child.predictionInt) == parent.predictionInt` per group.

Input contract:

- **Parent predictions CSV** — `outputs/predictions.csv` from a completed dengue run at `parent_level`. Located via `run.source_run_id` (or `"latest"` — see below).
- **Child case history CSV** — `downscale.cases_csv`, typically `prepared_data/<child_level>/cases_daily.csv` from a `dengue_prep` run at the child level. Columns: `region_id, date, case_count`.
- **Child geojsons** — `downscale.geojson_base_path/<child_level>s/*.geojson`. Each feature carries `region_id` and `parent`; the mapping `{child_id → parent_id}` is built from those two properties.

Output: `outputs/predictions.csv` at the run's artifact root, plus interactive HTML brief and maps.

---

## Output CSV schema

Same columns as the parent CSV, with one addition and one boundary rename:

```
dateOfComputingPrediction,startDatePredictedWeek,regionID,LGD_code,
prediction,predictionRaw,predictionMin,predictionMax,
thresholdMethod,predictionZone,model
```

At the CSV boundary (same convention as the main pipeline):

- **`prediction`** — integer allocation from LRM apportionment (`predictionInt` internally).
- **`predictionRaw`** — the model float (`parent_raw × share`).
- **`predictionMin` / `predictionMax`** — parent ensemble extremes proportionally scaled by the child's share (only when the parent CSV carried them). LRM-apportioned int is enforced to sit inside this band by widening `min` / `max` if the rounded int falls outside.

Sample row (KA district → subdistrict, mandal downscaling from a district-level ensemble run):

```
2025-11-24,2025-12-01,subdistrict_29_08_02,29-08-02,2,1.84,1,3,previousNweeks,3,ensembleModel
```

---

## The 3-tier share fallback (issue #86 / PR #88)

`compute_shares` in `pipelines/dengue_downscale/lib/downscale.py` walks a strict fallback chain per parent:

1. **Primary window** — sum of child cases over `(as_of_date - window_weeks, as_of_date]`. If the parent's total is `> 0`, use per-child fractional shares. **Tier: `"primary"`**.
2. **Historical fallback** — if `historical_fallback_weeks` is set and the primary window is empty, retry over the longer window. **Tier: `"historical"`**. Logged as WARNING because the primary signal was insufficient.
3. **Uniform** — every child gets `1 / n_children`. **Tier: `"uniform"`**. Logged as WARNING; the downscale for that parent is not data-driven.

`historical_fallback_weeks` must be strictly greater than `window_weeks` (validated in `DownscaleConfig.from_raw`). Setting it to `None` (default) disables the tier and is byte-identical to the pre-#86 behaviour: primary → uniform.

Tier counts are surfaced on `child_preds.attrs["tier_stats"]` and logged by the step:

```
downscale_predictions: share tiers — primary=27, historical_fallback=3, uniform=1
```

**Refusal to guess.** NaN case counts inside the primary window are a hard `ValueError` — a data-integrity problem, not something to silently treat as zero. Negative case counts get clipped to zero with a warning.

---

## LRM apportionment for integer conservation (issue #83 / PR #88)

The main pipeline emits `predictionInt = round_half_up(prediction)` at the CSV boundary. Downscale must preserve the integer invariant:

> **For every (parent, week, thresholdMethod, model) group:** `sum(child.predictionInt) == round_half_up(sum(child.prediction)) == parent.predictionInt`.

Achieved via `pipelines/dengue_downscale/lib/apportionment.py::largest_remainder` (Hamilton's method):

1. `target = round_half_up(sum(child_raw))` — the parent's displayable total.
2. Each child gets `base_i = floor(raw_i)`.
3. `leftover = target - sum(base_i)` — always in `[0, n_children]`.
4. Rank children by fractional remainder `raw_i - floor(raw_i)` descending. Tie-breaks in order:
   - a. **Higher `recent_cases` wins** — primary window case count; ties on the fractional remainder go to the ward with more real recent cases (Prerna's ask in #86).
   - b. **Lower `region_id` lexicographically** — deterministic tail.
5. The top `leftover` children get `+1`.

Property: each child's int is exactly `floor(raw_i)` or `floor(raw_i) + 1`, so no child is off by more than 1.

Conservation is asserted at the end of `downscale_predictions` via `check_numeric_sanity`; violations `raise ValueError`. Integer conservation is separately reported by `downscale_diagnostics`:

```
sum(predictionInt) vs round_half_up(sum(prediction)): 124/124 parent-weeks match
```

---

## Zone re-derivation happens on the RAW child prediction

`assign_child_zones` re-computes `predictionZone` from **child-level thresholds** (never inherited from the parent). Every classification method mirrors the parent pipeline's:

- **`who`** — per-region `T_α = Mean + α·StdDev` derived from the child's own case history via the parent run's `thresholdMethod` (historical / prev_nweeks / weighted_baseline). The per-method context (`n_weeks`, `historical_n_years`, `recent_weeks`, …) is copied out of the downscale config's `thresholds.method_configs`.
- **`icmr`** — cross-sectional quartile strata across all children per predicted week.
- **`percentile`** — per-region historical-percentile bands cut at `percentile_cutoffs` of each child's own case history.

Zones use the **raw** child prediction, not the LRM-apportioned integer. This matters because a child with `raw=0.4` that gets rounded to 0 by LRM would otherwise appear zero-zone; the raw float carries the sub-integer signal into the WHO band computation. Degenerate thresholds (`Mean=0 & StdDev=0`) map to `NA` and then to `predictionZone=0`, so a zero-case child can never carry an inherited elevated risk zone.

---

## `source_run_id: "latest"` — longest-prefix classifier

`run.source_run_id` accepts either a specific run ID or the sentinel `"latest"`. Under `"latest"`, `_resolve_latest_run` in `pipelines/dengue_downscale/steps/load_predictions.py` picks the most recently modified run dir whose `predictions.csv` is at the configured `parent_level`.

The classification is a **longest-prefix match** against the region_types available in the geojson tree. Naive `startswith(parent_level + "_")` breaks on compound region types: `"ulb_ward_..."` starts with `"ulb_"`, so a ULB-ward downscale would pick up a previous ULB-district run as its source and silently produce nonsense. The longest-first scan guarantees `"ulb_ward"` wins over `"ulb"` on `"ulb_ward_..."`.

Prior downscale outputs (which are at the child level) are filtered out for the same reason — otherwise `"latest"` will happily pick a previous downscale run and try to downscale it again.

---

## Empty maps fix — report primary fallback (issue #103)

`generate_downscale_brief` renders the HTML report using the model named by `report.primary`. If that model is not present in the downscaled predictions (e.g. `report.primary: ensemble` on a single-model upstream run like TimesFM-only), the step falls back to the first available model in the CSV and logs a warning:

```python
if primary_model not in available_models:
    fallback = next(iter(sorted(available_models)), None)
    if fallback:
        context.log.warning(
            "generate_downscale_brief: primary model %r not in predictions "
            "(available=%s); falling back to %r for the report.",
            primary_model, sorted(available_models), fallback,
        )
        primary_model = fallback
```

Before #103, an upstream `[timesfm]` run (which never emits `ensembleModel`) would silently render an empty-state brief.

---

## Configuration

Minimal `downscale.yaml` fragment:

```yaml
pipeline:
  entrypoint: pipelines.dengue_downscale.pipeline:build_pipeline

run:
  source_run_id: latest        # or a specific run ID

downscale:
  parent_level: district
  child_level: mandal
  window_weeks: 4
  historical_fallback_weeks: 12   # tier-2 fallback; must be > window_weeks
  cases_csv: prepared_data/mandal/cases_daily.csv
  geojson_base_path: ap_datasets/geojsons/geojsons_AP
  on_missing_parents: warn        # "error" (default) or "warn"

thresholds:
  # same shape as the dengue pipeline — child zones re-derive using these
  methods: [historical, previousNweeks]
  classification_method: who
  list_alpha: [1.0, 2.0, 3.0]
  method_configs:
    historical:
      historical_n_years: 5
    previousNweeks:
      n_weeks: 4

report:
  primary: ensemble
  threshold_method_for_report: previousNweeks

state:
  code: KA
```

---

## CLI recipes

**Andhra Pradesh: district → mandal.**

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_downscale.pipeline:build_pipeline \
  --config configs/ap_district_downscale.yaml \
  --run-id ap-mandal-2025-w49
```

**Odisha: district → block/ULB.** Uses compound region types under a single child level.

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_downscale.pipeline:build_pipeline \
  --config configs/od_district_downscale.yaml \
  --run-id od-block-2025-w49
```

**Odisha: ULB → ward.** Two-hop downscale — first run OD district→ULB, then use *that* run as source for ULB→ward. The `"latest"` classifier picks the ULB run correctly thanks to longest-prefix matching over `["ulb_ward", "ulb", "district", ...]`.

```bash
# Step 1 (district → ulb) — produces a ULB-level predictions.csv
uv run python -m acestor.run \
  --pipeline pipelines.dengue_downscale.pipeline:build_pipeline \
  --config configs/od_district_to_ulb.yaml \
  --run-id od-ulb-2025-w49

# Step 2 (ulb → ward) — sources the ULB run above
uv run python -m acestor.run \
  --pipeline pipelines.dengue_downscale.pipeline:build_pipeline \
  --config configs/od_ulb_to_ward.yaml \
  --run-id od-ward-2025-w49 \
  --override run.source_run_id=od-ulb-2025-w49
```

**Karnataka: district → subdistrict.**

```bash
uv run python -m acestor.run \
  --pipeline pipelines.dengue_downscale.pipeline:build_pipeline \
  --config configs/ka_district_downscale.yaml \
  --run-id ka-subdistrict-2025-w49
```

---

## Diagnostics — what to look for in the logs

```
downscale_predictions: 32 parent rows, 1076 children mapped, window=4 weeks
downscale_predictions: zone re-derivation — classification=who, list_alpha=[1.0, 2.0, 3.0], methods=['historical', 'previousNweeks']
downscale_predictions: share tiers — primary=28, historical_fallback=3, uniform=1
downscale_predictions: sanity — conservation_max_abs_err=2.84e-13; 1 parent(s) uniform-split (no-data fallback); risk vs parent over 128 parent-week(s): 42 below / 39 above / 47 match; sum(predictionInt) vs round_half_up(sum(prediction)): 128/128 parent-weeks match
```

Signals to act on:

- **`conservation_max_abs_err` > 1e-6** — LRM invariant broken; check that `predictionInt` and `prediction` haven't been swapped in a pre-existing CSV read (`downscale_predictions.py` reverses the boundary rename on read).
- **`n_parents_uniform > 0`** — that many parents had no case data in the primary or historical windows. Consider raising `historical_fallback_weeks` or investigating child case ingestion.
- **`sum(predictionInt) ... N/M parent-weeks match` where N < M** — integer conservation broken. Should never happen; file a bug against #83 / #88.
- **`parents dropped (no children mapped)`** — a parent has predictions but no child in the geojson tree points at it. Under `on_missing_parents: error` this raises; under `warn` those parent rows are dropped.
