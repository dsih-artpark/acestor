Output Format
=============

Every pipeline run writes its artifacts under
``{storages.artifacts.filesystem.base_path}/{run_id}/``. The canonical
prediction table is always at ``outputs/predictions.csv`` regardless of the
model / output mode chosen. This page documents every column in that CSV,
the auxiliary outputs (charts, maps, interactive HTML brief), and the
Largest-Remainder invariant that ties parent and child predictions together
in the downscale pipeline.

Directory layout
----------------

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Path (relative to run root)
     - What it is
   * - ``inputs/cases_<res>_sampled.csv``
     - Case data trimmed to the modelling window
   * - ``inputs/weather_<res>_sampled.csv``
     - Weather features aligned to the modelling window
   * - ``inputs/thresholds_<res>.csv``
     - Long-form (region_id, date, Mean, StdDev, threshold_method) table
       covering every configured method, used as input to
       ``train_and_predict``
   * - ``outputs/predictions.csv``
     - Canonical predictions table (see column reference below)
   * - ``outputs/per_model/predictions_<model>.csv``
     - Per-model copies (only in ``output: both`` or ``output: per_model``
       modes)
   * - ``outputs/charts/hero_forecast.png``
     - Region-level forecast time-series chart embedded in the HTML brief
   * - ``outputs/maps/*.png``
     - Choropleth PNGs, one per ``(model, thresholdMethod, week)`` — only
       generated when ``maps.enabled: true``
   * - ``outputs/report.html``
     - Interactive HTML brief with an embedded D3 choropleth (see
       `The HTML brief`_ below)
   * - ``outputs/prep_summary.md``
     - Human-readable summary of the downscale step (downscale runs only)

``predictions.csv`` — column reference
--------------------------------------

A real header line from ``artifacts/ap/ap-repro-fixed/outputs/predictions.csv``::

    recordDate,thresholdMethod,Mean,StdDev,Zero,Inf,T0.00,T1.00,T2.00,
    startDatePredictedWeek,dateOfComputingPrediction,regionID,lgdCode,
    predictionRaw,ISOWeek,model,predictionMin,predictionMax,
    predictionZone,whoZone,prediction,icmrZone,percentileZone

A real data row from the same file::

    2026-03-17,previousNweeks,1.6666666666666667,0.478713553878169,
    0.0,inf,1.6666666666666667,2.1453812205448357,2.6240957744230045,
    2026-03-17,2026-07-24,district_502,502,1.5658082125731811,12,
    ensembleModel,,,3,1.0,2,3,2

Columns are grouped by role below.

Identity columns
~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Column
     - Meaning
   * - ``regionID``
     - Fully qualified region id (e.g. ``district_502``, ``ward_198``,
       ``zone_5``). Always prefixed with the spatial resolution.
   * - ``startDatePredictedWeek``
     - ISO date (Mon-anchored) of the week the prediction is for.
   * - ``dateOfComputingPrediction``
     - Wall-clock date the run executed. Useful for hindcast reproducibility
       audits.
   * - ``ISOWeek``
     - ISO week number (1..53) of ``startDatePredictedWeek``.
   * - ``model``
     - Model that produced the row: ``nbr``, ``timesfm``, or
       ``ensembleModel``. In ``output: ensemble`` mode this is always
       ``ensembleModel``.
   * - ``thresholdMethod``
     - Threshold-generation method whose Mean/StdDev feed this row's
       ``T*`` columns. Values: ``historical``, ``previousNweeks``,
       ``weightedBaseline`` (camelCase per PRISM-H).
   * - ``lgdCode``
     - Local Government Directory numeric code, added by
       ``pipelines/dengue/lib/lgd.py`` (PR #59). Blank if the region
       isn't in the LGD map.

Numeric prediction columns
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Column
     - Meaning
   * - ``prediction``
     - **Display integer.** This is the value dashboards and briefs show.
       Rounded from ``predictionRaw`` with ``round_half_up`` (0.5 rounds
       up), clamped ≥ 0. In the downscale pipeline it also satisfies the
       LRM invariant (see `LRM invariant`_).
   * - ``predictionRaw``
     - Raw float from the model / ensemble. Used by backtesting,
       calibration, and downscale shares. **Never rounded internally** —
       the CSV boundary is the only place the rename happens
       (``train_and_predict.py``: ``prediction`` ⇄ ``predictionRaw``).
   * - ``predictionMin``, ``predictionMax``
     - Inter-ensemble range (PR #87 / issue #84). For ensemble rows,
       these are the min and max of the corresponding per-model
       predictions for the same ``(region, date)``. For per-model rows,
       ``min == max == prediction``. **Not a confidence interval**; not
       std; not bootstrap. Semantic: extremes across the members. The
       CSV also enforces ``predictionMin ≤ prediction ≤ predictionMax``
       at the boundary, widening the bracket if rounding pushed the
       display int outside.
   * - ``predictionInt`` *(downscale only, internal)*
     - LRM-consistent integer produced by ``largest_remainder`` in the
       downscale step. Renamed to ``prediction`` at CSV write, so it does
       not appear in the header. See `LRM invariant`_.

Threshold columns
~~~~~~~~~~~~~~~~~

The threshold columns are copied from the ``inputs/thresholds_<res>.csv``
table for the ``(regionID, startDatePredictedWeek, thresholdMethod)`` key.

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Column
     - Meaning
   * - ``Mean``
     - The μ value for the row's ``thresholdMethod``. What "recent" or
       "historical" or "weighted baseline" is depends on the method (see
       ``docs/threshold-methods-reference.md``).
   * - ``StdDev``
     - The σ value. Inflated to ``sqrt(Mean)`` when it collapses to 0
       while ``Mean > 0`` (PRISM-H §4.4).
   * - ``T0.00``, ``T1.00``, ``T2.00``
     - Threshold cut-points at α ∈ ``list_alpha``. Formula:
       ``T_α = Mean + α · StdDev + i · 1e-6`` (i = index of α in the
       list, 1-based, ensuring strict monotonicity when Mean/StdDev
       collapse). Default ``list_alpha: [1.0, 2.0]`` produces three
       T-columns.
   * - ``Zero``, ``Inf``
     - Sentinel bounds (always 0.0 and ``inf``). Combined with the
       T-columns to form ``[Zero, T0.00), [T0.00, T1.00), …, [T2.00,
       Inf)`` half-open intervals that ``predictionRaw`` is bucketed
       into to derive ``whoZone``.

Zone assignment columns
~~~~~~~~~~~~~~~~~~~~~~~

Three parallel classifications are always computed and always written.
``predictionZone`` is a copy of one of them, selected by
``thresholds.classification_method`` in the config.

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Column
     - Meaning
   * - ``whoZone``
     - WHO-style band. Integer in ``1..N`` where N = ``len(list_alpha) +
       2``. Derived by binning ``predictionRaw`` into the ``[Zero, T0.00,
       T1.00, …, Inf]`` intervals. Set to ``NA`` when the row's Mean and
       StdDev are both zero (degenerate threshold).
   * - ``icmrZone``
     - ICMR quartile stratum (A1–A4 → 4..1). Cross-sectional per date:
       distinct predicted values across all regions on that date are
       ranked descending and cut into four equal strata. Set to ``NA``
       when the total predicted caseload for the date is < 10 (PRISM-H
       §5.4 "insufficient data" guard).
   * - ``percentileZone``
     - Per-region historical-percentile band (PR #73, issue #62). Cut
       points come from the region's own weekly-aggregated case history
       at ``percentile_cutoffs`` (default ``[50, 75, 90]`` → 4 bands: ≤
       p50, (p50, p75], (p75, p90], > p90). Regions with no history get
       ``NA``.
   * - ``predictionZone``
     - Convenience copy of whichever of the above is selected by
       ``thresholds.classification_method`` (``who`` / ``icmr`` /
       ``percentile``). ``NA`` values are filled with the sentinel
       ``0`` — dashboards render zone 0 as light grey and log a warning
       listing the affected regions.

LRM invariant
-------------

The downscale pipeline (``pipelines/dengue_downscale``) apportions each
parent-level ``predictionRaw`` across its children using the **Largest
Remainder Method** (Hamilton's method, ``apportionment.py``).

For every ``(parent, week)``::

    sum(child.prediction) == round_half_up(sum(child.predictionRaw))
                          == round_half_up(parent.predictionRaw)

Each child's integer allocation is either ``floor(child_raw)`` or
``floor(child_raw) + 1`` — off by at most one from the natural round.
Ties on fractional remainder are broken by (a) higher recent-cases wins
(issue #86), then (b) lower ``region_id`` lexicographically. The
downscale step logs
``sum(predictionInt) vs round_half_up(sum(prediction)): M/N parent-weeks
match`` — this should always be N/N.

Charts and maps
---------------

* ``outputs/charts/hero_forecast.png`` — the region-focused forecast
  chart embedded at the top of ``report.html``. Always produced.
* ``outputs/maps/<model>_<thresholdMethod>_<YYYYMMDD>.png`` — a static
  choropleth per prediction week. Only produced when
  ``maps.enabled: true`` in the config. The set is deterministic:
  one image per ``(model in outputs, thresholdMethod in outputs,
  startDatePredictedWeek)`` combination.

The HTML brief
--------------

``outputs/report.html`` is a fully self-contained HTML document — no
external assets. It embeds:

* The hero chart as an inline PNG.
* A per-week narrative summary.
* An interactive D3 choropleth. All map data is inlined as a JS global
  by ``pipelines/dengue/templates/partials/_d3_map_script.html.j2``::

      window.__BRIEF_DATA = {
        geojson:       <FeatureCollection of all rendered regions>,
        parent_lookup: {<parent_id>: {name, children: [...], ...}},
        weekly_zones:  {<week_index>: {<region_id>: <zone_int>}}
      };

  ``geojson`` is the union of every parent polygon that has predictions
  in the brief; ``parent_lookup`` powers the drill-in / drill-out
  behaviour; ``weekly_zones`` is the per-week zone assignment used to
  paint the polygons. All three are trimmed to the horizon that the
  brief covers, keeping ``report.html`` small enough to email.

Output modes
------------

``model.output`` controls what ends up on disk:

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - Mode
     - Canonical ``outputs/predictions.csv``
     - ``outputs/per_model/predictions_<m>.csv``
   * - ``ensemble`` *(default)*
     - Ensemble rows
     - *(not written)*
   * - ``both``
     - Ensemble rows
     - One file per non-ensemble model
   * - ``per_model``
     - Copy of ``report.primary``'s per-model file
     - One file per configured model

The canonical path never changes — consumers (officials, the HTML brief,
the downscale pipeline) can always read ``outputs/predictions.csv``
without needing to know the mode.
