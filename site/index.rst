Acestor Documentation
=====================

**Acestor** is a production dengue-intelligence pipeline. It ingests case and weather data, estimates per-region risk thresholds, runs an ensemble of forecasting models (RF, XGB, TSE, NBR, TimesFM), produces interactive risk maps, and emits a self-contained HTML brief — all driven from a single YAML configuration file.

.. grid:: 2

    .. grid-item-card:: Get Started
        :link: quickstart
        :link-type: doc

        New here? Run your first pipeline in a few minutes.

    .. grid-item-card:: GitHub
        :link: https://github.com/dsih-artpark/acestor
        :link-type: url

        Browse the source code, open issues, and contribute.

----

Three Pipelines
---------------

Acestor is not a single monolithic pipeline — three independently-runnable DAGs share the same config surface:

.. list-table::
   :header-rows: 1
   :widths: 20 50 30

   * - Pipeline
     - What it does
     - Config example
   * - ``dengue_prep``
     - Ingest raw case data (IHIP linelist / dashboard API / custom source) + weather (Open-Meteo / cache / custom) → produce cleaned ``prepared_data/*/cases_daily.csv`` and ``weather_daily.csv``
     - ``configs/ka_district_prep.yaml``
   * - ``dengue``
     - Load prepared data → compute thresholds → train models → write ``predictions.csv`` + interactive HTML brief
     - ``configs/ka_district.yaml``
   * - ``dengue_downscale``
     - Take a parent-level ``predictions.csv`` and apportion it to child regions via observed case shares + Largest Remainder Method (integer conservation guarantee)
     - ``configs/ka_district_to_subdistrict.yaml``

Main-Pipeline Steps
-------------------

The ``dengue`` pipeline is a **11-step DAG** — steps run concurrently where dependencies allow.

.. list-table::
   :header-rows: 1
   :widths: 5 30 65

   * - #
     - Step
     - What it does
   * - 1
     - ``identify_sampling_day``
     - Derives the weekly anchor (``W-MON``/``W-SAT``/…) from ``run.run_date``
   * - 2
     - ``parse_case_data``
     - Loads and re-samples ``prepared_data/{region_type}/cases_daily.csv`` to the sampling grid
   * - 3
     - ``parse_weather_data``
     - Loads ``weather_daily.csv``, rolls to weekly with configurable lags
   * - 4
     - ``validate_case_data_sufficiency``
     - Early gate — stops if data is too thin (min rows / regions / span)
   * - 5
     - ``identify_cutoff_dates``
     - Picks case + weather cutoffs and the 4-week prediction calendar
   * - 6
     - ``generate_thresholds``
     - Per-region ``historical`` / ``prev_nweeks`` / ``weighted_baseline`` threshold tables
   * - 7
     - ``train_and_predict``
     - Fits every model in ``model.models`` (RF, XGB, TSE, NBR, TimesFM), writes per-model + ensemble predictions
   * - 8
     - ``assess_thresholds``
     - Threshold assessment table + figure metadata
   * - 9
     - ``generate_maps``
     - Static PNG choropleth maps *(skip via* ``maps.enabled: false`` *— HTML uses interactive D3 maps regardless)*
   * - 10
     - ``generate_report``
     - Single-file ``report.html`` with hero chart, weekly zone tables, interactive D3 map
   * - 11
     - ``send_report``
     - Emails the report (opt-in via ``email.enabled: true``)

The ``dengue_prep`` and ``dengue_downscale`` DAGs are documented in :doc:`dengue_prep` and :doc:`downscale`.

Key Features
------------

- **Pluggable data sources.** Case data from local files, the dengue-dashboard API, or a custom Python module. Weather from Open-Meteo, filesystem cache, or a state API. See :doc:`sources`.
- **TimesFM 2.5 forecasting.** Google's pretrained time-series foundation model, run in an isolated subprocess so torch can't collide with xgboost's OpenMP.
- **Integer-consistent downscaling.** Largest Remainder Method guarantees ``sum(child.predictionInt) == parent.predictionInt``.
- **Historical-window fallback.** When the recent 4-week share window is empty, fall back to a 52-week window before uniform-splitting.
- **Percentile risk classification.** Per-region historical percentile cutoffs (default 50/75/90) — plus WHO and ICMR classifiers available in parallel.
- **Interactive HTML brief.** D3-based choropleth + zone tables in a single ``report.html``, no server required.

.. toctree::
   :hidden:
   :caption: Getting Started

   quickstart
   installation

.. toctree::
   :hidden:
   :caption: User Guide

   usage
   configuration
   deployment

.. toctree::
   :hidden:
   :caption: Reference

   data_specification
   output_format
   dengue_prep
   sources
   models
   downscale
   thresholds_explained
   threshold_methods_reference
   config_reference

.. toctree::
   :hidden:
   :caption: Help

   troubleshooting

.. toctree::
   :hidden:
   :caption: Links

   GitHub <https://github.com/dsih-artpark/acestor>
