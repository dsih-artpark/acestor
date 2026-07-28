Configuration Reference
=======================

Reference for every YAML key consumed by the three acestor pipelines. Each field
lists **type**, **default**, and **why it exists**. The source of truth is the
``configs.py`` module for each pipeline — this document is derived from those
dataclasses.

Three pipelines are documented:

1. ``dengue_prep`` — builds ``prepared_data/`` from raw case + weather sources.
2. ``dengue`` — trains models, computes thresholds, renders maps and the HTML
   brief.
3. ``dengue_downscale`` — takes parent-level predictions from a ``dengue`` run
   and splits them onto child regions.

Each pipeline has a ``pipeline.name`` matching one of the above; the loader
dispatches on it.

Common top-level keys (``state``, ``pipeline``, ``run``, ``logging``,
``storages``, ``email``) are shared and documented once at the end.

.. contents::
   :local:
   :depth: 2

1. dengue_prep
--------------

Config sample: ``configs/ka_district_prep.yaml``,
``configs/od_district_prep.yaml``. Dataclasses:
``pipelines/dengue_prep/configs.py``.

``data.prepared_data``
~~~~~~~~~~~~~~~~~~~~~~

Where prep writes its outputs. Consumed by the downstream ``dengue`` pipeline
via the same block.

.. list-table::
   :header-rows: 1
   :widths: 20 12 22 46

   * - key
     - type
     - default
     - why
   * - ``base_dir``
     - str
     - ``prepared_data``
     - Root folder for the per-region-type subfolders
       (``district/``, ``zone/``…).

``data.case_download``
~~~~~~~~~~~~~~~~~~~~~~

Fetches raw line-list files from a case source.

.. list-table::
   :header-rows: 1
   :widths: 22 14 24 40

   * - key
     - type
     - default
     - why
   * - ``enabled``
     - bool
     - ``false``
     - Master switch. When false, prep assumes files are already staged under
       ``source_path``.
   * - ``source_mode``
     - str
     - ``""``
     - Selects a case-source plugin: ``dashboard``, ``filesystem``, a dotted
       module path, or a direct file path. Empty → legacy backend.
   * - ``source_backend``
     - str
     - ``filesystem``
     - Legacy path used only when ``source_mode`` is blank; ``filesystem`` or
       ``s3``.
   * - ``source_path``
     - str
     - ``""`` (env ``DENGUE_PREP_CASE_SOURCE`` fallback)
     - Root path or URL the source plugin reads from.
   * - ``base_url``
     - str
     - plugin-specific
     - Dashboard API root, e.g.
       ``https://apps.artpark.ai/disease-dashboard``.
   * - ``disease``
     - str
     - ``Dengue``
     - Dashboard disease slug.
   * - ``date_start``
     - str
     - ``""``
     - Dashboard: inclusive start of the fetch window (YYYY-MM-DD).
   * - ``date_end``
     - str
     - ``""`` (→ ``run_date``)
     - Dashboard: inclusive end. Blank tracks the current run.
   * - ``chunk_days``
     - int
     - plugin default
     - Dashboard pagination window; smaller values ease per-request payload.
   * - ``backfill_days``
     - int
     - plugin default
     - Dashboard: how many days before ``date_start`` to re-fetch to catch
       late-arriving reports.
   * - ``selected_region_id``
     - str
     - ``""``
     - Restrict the dashboard fetch to a single region (debugging).
   * - ``cache_enabled``
     - bool
     - ``true``
     - Turn off to force re-download on every run.
   * - ``cache_dir``
     - str
     - ``./cache/raw_case``
     - Local cache root.
   * - ``cache_strategy``
     - str
     - ``local_first``
     - ``local_first`` skips the network when the cache hits.
   * - ``filesystem_base_path``
     - str
     - ``""``
     - Absolute or relative filesystem root when ``source_backend=filesystem``.
   * - ``s3_bucket``, ``s3_prefix``
     - str
     - ``""``
     - S3 backend location.
   * - ``source_paths``
     - list[str]
     - ``[]``
     - Explicit list of files a plugin should ingest.
   * - ``dest_relpath``
     - str
     - ``datasets/raw_linelist_data/linelist``
     - Relative destination under the run artifact root.

``data.case_parse``
~~~~~~~~~~~~~~~~~~~

Parses raw case files into ``cases_daily.csv``.

.. list-table::
   :header-rows: 1
   :widths: 22 18 22 38

   * - key
     - type
     - default
     - why
   * - ``region_types``
     - list[str]
     - required
     - Non-empty list of region tiers to emit; each becomes a subfolder under
       ``prepared_data/``.
   * - ``date_start``
     - str
     - ``""``
     - Inclusive start filter on the case date. Empty → no lower bound.
   * - ``date_end``
     - str
     - ``""``
     - Inclusive end filter. Empty → no upper bound.
   * - ``date_column``
     - str | list[str]
     - ``["Sample Collected Date"]``
     - Candidate column(s) for the case date. Parser tries in order; first one
       that parses cleanly wins. String is accepted for back-compat.
   * - ``region_id_column``
     - str
     - ``""``
     - When set and present in the file, this column is trusted verbatim as the
       resolved ``region_id`` — skips LGD lookup, geocode, spatial join.
   * - ``lgd_code_column``
     - str
     - ``""``
     - Column with an LGD code. Present → used to resolve to ``region_id``.
   * - ``lat_column``
     - str
     - ``Latitude``
     - Fallback spatial-join latitude column.
   * - ``lon_column``
     - str
     - ``Longitude``
     - Fallback spatial-join longitude column.
   * - ``header_row``
     - int
     - ``0``
     - 0-indexed row containing headers; use ``1`` for banner-row IHIP exports.
   * - ``filters``
     - list[dict]
     - ``[]``
     - Row filters: each entry ``{column, values}``; AND across entries, OR
       within ``values``.
   * - ``geocoding``
     - dict
     - disabled
     - Google-geocode + spatial-join resolver; see below.

``data.case_parse.geocoding``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Runs only when a row has neither LGD code nor ``region_id_column`` and only
free-text address fields are usable.

.. list-table::
   :header-rows: 1
   :widths: 24 14 24 38

   * - key
     - type
     - default
     - why
   * - ``enabled``
     - bool
     - ``false``
     - Opt in explicitly.
   * - ``address_fields``
     - list[str]
     - required when enabled
     - Columns concatenated to build the primary query.
   * - ``fallback_address_fields``
     - list[str]
     - ``[]``
     - Second-pass columns when the first fails validation.
   * - ``cache_file``
     - str
     - ``./cache/geocode_cache.json``
     - On-disk memoisation of Google responses.
   * - ``extra_stopwords``
     - list[str]
     - ``[]``
     - Extra tokens stripped before validation.
   * - ``require_address``
     - bool
     - ``true``
     - Drop rows whose first address field is empty.
   * - ``require_validation``
     - bool
     - ``true``
     - Drop rows whose Google response fails token validation.
   * - ``bounds``
     - list[4]
     - ``null``
     - ``[min_lat, min_lon, max_lat, max_lon]`` viewport bias for the geocoder.
   * - ``restrict_admin_area_tokens``
     - list[str]
     - ``[]``
     - Substrings that must appear in the formatted address; hard reject.

``data.weather_download``
~~~~~~~~~~~~~~~~~~~~~~~~~

Fetches gridded weather. Two source families: CDS (``reanalysis-era5-land``)
and openmeteo.

.. list-table::
   :header-rows: 1
   :widths: 24 14 22 40

   * - key
     - type
     - default
     - why
   * - ``enabled``
     - bool
     - ``false``
     - Master switch.
   * - ``source_mode``
     - str
     - ``filesystem``
     - Plugin selector: ``filesystem``, ``cds``, ``openmeteo``.
   * - ``source_backend``
     - str
     - ``filesystem``
     - Backend for the filesystem plugin: ``filesystem`` or ``s3``.
   * - ``temperature_unit``
     - str
     - ``celsius``
     - Unit the *source* returns (``celsius``/``kelvin``). The parser normalises
       on-disk to Kelvin so downstream code sees one convention.
   * - ``precipitation_unit``
     - str
     - ``mm``
     - Unit the source returns (``mm``/``m``). Normalised to metres on disk.
   * - ``netcdf_cache_path``
     - str
     - ``""``
     - Local NetCDF cache path; empty → plugin default.
   * - ``parsed_output_path``
     - str
     - ``""``
     - Where per-region CSVs land after parsing.
   * - ``cds_variables``
     - list[str]
     - env ``GBA_CDS_VARIABLES``
     - CDS variable names.
   * - ``region_bounds``
     - list[4] | null
     - ``null``
     - ``[N, W, S, E]``; null → auto-compute from the geojson.
   * - ``region_type``
     - str
     - ``district``
     - Subfolder under the geojson root used to compute bounds.
   * - ``w_params``
     - list[str]
     - ``[t2m, d2m, tp]``
     - Short names of NetCDF variables the parser extracts.
   * - ``threshold_km``
     - float
     - ``25.0``
     - Max distance from a region's boundary a grid point may lie to still be
       attributed.
   * - ``bounds_resolution_deg``
     - float
     - ``0.1``
     - Snap auto-computed bounds to this grid (0.25 for ERA5, 0.1 for
       ERA5-Land).
   * - ``start_date``
     - str
     - ``2015-01-01``
     - Inclusive fetch start.
   * - ``end_date``
     - str
     - ``""``
     - Inclusive end. Empty → ``run_date``.
   * - ``cache_enabled``
     - bool
     - ``true``
     - Turn off to force re-download.
   * - ``cache_dir``
     - str
     - ``./cache/raw_weather``
     - Local cache root.
   * - ``cache_strategy``
     - str
     - ``local_first``
     - ``local_first`` skips the network on cache hit.
   * - ``source_path``, ``filesystem_base_path``, ``s3_bucket``, ``s3_prefix``,
       ``source_storage``, ``source_prefix``, ``source_paths``, ``dest_relpath``
     - str/list
     - ``""`` / ``datasets/raw_weather_data``
     - Location knobs mirroring ``case_download``.

``data.weather_parse``
~~~~~~~~~~~~~~~~~~~~~~

Daily aggregation of downloaded weather.

.. list-table::
   :header-rows: 1
   :widths: 22 16 32 30

   * - key
     - type
     - default
     - why
   * - ``region_type``
     - str
     - ``district``
     - Region tier the daily rows are keyed to.
   * - ``weather_variables``
     - list[str]
     - ``["2mTemperature","totalPrecipitation","2mDewpointTemperature"]``
     - Variables to aggregate.
   * - ``daily_agg``
     - list[dict]
     - ``mean`` for temps, ``sum`` for precip
     - Per-variable daily aggregation ops.

``data.geojson``
~~~~~~~~~~~~~~~~

``base_path`` (str, required) — root of the geojson layer; subfolders per
region type.

``data.date_range.start``
~~~~~~~~~~~~~~~~~~~~~~~~~

``start`` (str, required) — global anchor for the prep window; passed to
plugins that need a fetch start when ``date_start`` is blank.

2. dengue
---------

Config sample: ``configs/ka_district.yaml``, ``configs/gba_zone.yaml``.
Dataclasses: ``pipelines/dengue/configs.py``.

``data.prepared_data``
~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 12 22 46

   * - key
     - type
     - default
     - why
   * - ``base_dir``
     - str
     - ``prepared_data``
     - Root of the prep output the pipeline reads.
   * - ``region_type``
     - str
     - ``district``
     - Subfolder to load from (``district``, ``zone``, ``corp``, ``ward``,
       ``subdistrict``).

``data.case_parse``
~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 22 14 18 46

   * - key
     - type
     - default
     - why
   * - ``region_types``
     - list[str]
     - required
     - Non-empty, no duplicates. **Order matters**: the last entry drives
       cutoffs and the sampled CSV that maps/thresholds consume.
   * - ``date_start``
     - str
     - required
     - Inclusive start.
   * - ``date_end``
     - str
     - required
     - Inclusive end; empty string → parser uses today.

``data.case_sufficiency``
~~~~~~~~~~~~~~~~~~~~~~~~~

Early abort when the parsed case data is too thin to model.

.. list-table::
   :header-rows: 1
   :widths: 26 12 14 48

   * - key
     - type
     - default
     - why
   * - ``enabled``
     - bool
     - ``true``
     - Turn off to run the pipeline on tiny/synthetic inputs.
   * - ``min_total_rows``
     - int
     - ``30``
     - Row-count floor.
   * - ``min_distinct_regions``
     - int
     - ``2``
     - Region-count floor.
   * - ``min_date_span_days``
     - int
     - ``14``
     - Temporal span floor.
   * - ``case_column``
     - str
     - ``case``
     - Which column carries the case count.
   * - ``region_column``
     - str
     - ``""``
     - Empty → derived from ``region_type``.
   * - ``date_column``
     - str
     - ``""``
     - Empty → derived from ``region_type``.
   * - ``max_staleness_days``
     - int
     - ``0``
     - Max acceptable gap between ``run_date`` and the freshest observed case
       row. Catches ``run_date`` running ahead of ingest. ``0`` disables.

``cutoff``
~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 26 12 14 48

   * - key
     - type
     - default
     - why
   * - ``case_min_regions``
     - int
     - ``2``
     - Minimum regions with observations before the case cutoff is honoured.
   * - ``weather_min_regions``
     - int
     - ``5``
     - Same, for weather.

``thresholds``
~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 24 16 22 38

   * - key
     - type
     - default
     - why
   * - ``region_type``
     - str
     - ``zone``
     - Region tier the thresholds are computed for.
   * - ``methods``
     - list[str]
     - ``[historical, prev_nweeks]``
     - Which methods to compute; each writes its own threshold rows. Registered
       names: ``historical``, ``prev_nweeks``, ``weighted_baseline``.
   * - ``classification_method``
     - str
     - ``who``
     - Zone-labelling scheme: ``who``, ``icmr``, or ``percentile``. Unknown
       values raise (previously silently produced WHO output — issue #62).
   * - ``percentile_cutoffs``
     - list[float]
     - ``[25.0, 50.0, 75.0]``
     - For ``classification_method: percentile`` only. N cutoffs → N+1 bands,
       computed per region against its own weekly history. Common override:
       ``[50, 75, 90]``.
   * - ``n_weeks``
     - int
     - ``4``
     - Prev-N-weeks window.
   * - ``historical_n_years``
     - int | null
     - ``4``
     - Historical lookback in years. ``null`` → all history.
   * - ``excluded_years``
     - list[int]
     - ``[2020, 2021]``
     - Years dropped from the historical baseline (COVID gaps).
   * - ``included_years``
     - list[int]
     - ``[]``
     - Empty → no restriction; non-empty → whitelist.
   * - ``list_alpha``
     - list[float]
     - ``[1.0, 2.0]``
     - Sigma multipliers for the WHO/ICMR banding.
   * - ``recent_weeks``
     - int
     - ``4``
     - ``weighted_baseline`` recent-window length.
   * - ``sd_window_weeks``
     - int
     - ``8``
     - ``weighted_baseline`` std-dev window.
   * - ``weight_recent``
     - float
     - ``0.7``
     - ``weighted_baseline`` recent-mean weight.
   * - ``weight_seasonal``
     - float
     - ``0.3``
     - ``weighted_baseline`` seasonal weight (52-week-lag mean).
   * - ``method_configs.<name>``
     - dict
     - inherit
     - Per-method overrides for ``n_weeks``, ``historical_n_years``,
       ``excluded_years``, ``included_years``, ``recent_weeks``,
       ``sd_window_weeks``, ``weight_recent``, ``weight_seasonal``.
       ``region_type``, ``methods``, ``classification_method`` always inherit
       from the base block.

``model``
~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 26 16 16 42

   * - key
     - type
     - default
     - why
   * - ``spatial_res``
     - str
     - ``zone``
     - Region tier the models train and predict on.
   * - ``models``
     - list[str]
     - ``["tse"]``
     - Subset of ``nbr``, ``tse``, ``rf``, ``xgb``, ``timesfm``. ``nbr`` was
       retired from the default set (issue #63) but is still opt-in.
   * - ``ensemble``
     - str
     - ``mean``
     - Registered ensemble strategy or ``none``. ``none`` is incompatible with
       ``output=ensemble``.
   * - ``output``
     - str
     - ``ensemble``
     - ``ensemble``, ``per_model``, or ``both``.
   * - ``tune``
     - bool | str
     - ``false``
     - ``true`` → always Optuna-retune. ``false`` → use cache when fingerprint
       matches, otherwise retune. ``"never"`` → trust cache regardless
       (hindcast / production reuse); fails loud on cache miss.
   * - ``n_trials``
     - int
     - ``100``
     - Optuna trials when tuning runs.
   * - ``debug``
     - bool
     - ``false``
     - Emit per-model intermediate CSVs under ``artifacts/debug/<model>/``.
   * - ``data_features``
     - list[str]
     - ``[]``
     - Columns fed to the tabular models.
   * - ``years_to_exclude``
     - list[int]
     - ``[2020, 2021]``
     - Training years dropped.
   * - ``years_to_include``
     - list[int]
     - ``[]``
     - Empty → no restriction; non-empty → whitelist.
   * - ``lag.lag_temp``
     - list[int]
     - ``[12]``
     - Temperature lag weeks fed as features.
   * - ``lag.lag_rainfall``
     - list[int]
     - ``[4]``
     - Precipitation lags.
   * - ``lag.lag_humidity``
     - list[int]
     - ``[4]``
     - Dewpoint lags.
   * - ``lag.lag_cases``
     - list[int]
     - ``[]``
     - Case-count lags (autoregressive).
   * - ``clip_multiplier``
     - float | null
     - ``null``
     - Upper clip in recursive forecast: cap each step at
       ``clip_multiplier * max(train cases)``. ``null`` = floor at 0 only
       (default; reference parity).
   * - ``freeze_weather_at_origin``
     - bool
     - ``true``
     - Persistence: freeze weather/exogenous lag features at the forecast
       origin for every future week. Matches upstream vbd-modelbench. Set
       false only for hindcasts that deliberately leak future weather.

``model_configs.<model>``
~~~~~~~~~~~~~~~~~~~~~~~~~

Per-model overrides. Keys mirror the ``model`` block: ``data_features``,
``years_to_exclude``, ``years_to_include``, ``tune``, ``n_trials``, ``debug``,
``clip_multiplier``, ``freeze_weather_at_origin``, and the ``lag.*`` sub-keys.
Unspecified keys inherit from ``model``. Any key not listed in ``model.models``
is rejected at build time (fail-fast; the error message names both the YAML fix
and the ``--set`` CLI trap that re-creates a removed key).

``model_configs.timesfm``
^^^^^^^^^^^^^^^^^^^^^^^^^

TimesFM 2.5 foundation model settings. **All optional** — omit the whole block
and defaults apply.

.. list-table::
   :header-rows: 1
   :widths: 24 10 30 36

   * - key
     - type
     - default
     - why
   * - ``huggingface_repo_id``
     - str
     - ``google/timesfm-2.5-200m-pytorch``
     - HF repo the checkpoint is pulled from.
   * - ``revision``
     - str
     - env ``TIMESFM_REVISION`` or pinned SHA
       ``1d952420fba87f3c6dee4f240de0f1a0fbc790e3``
     - Must be a full 40-hex commit SHA. Tags/branches are rejected —
       checkpoints can move under a floating ref.
   * - ``cache_dir``
     - str
     - ``.cache/timesfm``
     - Project-local, gitignored.
   * - ``max_context``
     - int
     - ``1024``
     - Truncate each region's series to this many trailing weeks before
       inference.
   * - ``per_core_batch_size``
     - int
     - ``32``
     - Torch batch size inside the worker.
   * - ``min_context_weeks``
     - int
     - ``52``
     - Regions with fewer weeks of history are skipped (all-zero regions still
       get zero predictions).
   * - ``timeout_s``
     - float
     - ``300``
     - Subprocess wall-clock timeout.
   * - ``max_regions_per_batch``
     - int
     - ``128``
     - Cap on regions per worker call — bounds worker memory.

``assess``
~~~~~~~~~~

``total_<region>_regions`` (int) — denominator for assess ratios. Recognised
keys: ``total_corp_regions``, ``total_zone_regions``, ``total_ward_regions``,
``total_district_regions``, ``total_subdistrict_regions``.

``maps``
~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 22 10 22 46

   * - key
     - type
     - default
     - why
   * - ``enabled``
     - bool
     - ``true``
     - Set ``false`` to skip static PNG generation. The HTML report renders
       interactive D3 maps from embedded GeoJSON regardless; PNGs remain
       useful for PDF/offline distribution.
   * - ``output_dir``
     - str
     - ``plots``
     - Where PNG choropleths land.
   * - ``figure_title``
     - str
     - ``Dengue risk map``
     - Suptitle line 1; line 2 is the prediction date.

``report``
~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 10 26 34

   * - key
     - type
     - default
     - why
   * - ``document_title``
     - str
     - ``<pipeline.title> — summary report``
     - Report ``<title>``. Falls back to a generic string when both this and
       ``pipeline.title`` are empty.
   * - ``primary``
     - str
     - ``ensemble``
     - Which prediction feeds maps and the report. Accepts ``ensemble`` or a
       model key. Short aliases (``timesfm``, ``ensemble``) resolve to the
       internal model name.
   * - ``threshold_method_for_report``
     - str
     - ``historical``
     - Threshold method the report tables and headline bands use.

``report_distribution``
~~~~~~~~~~~~~~~~~~~~~~~

Free-form dict rendered into the report header (``system_name``,
``organization``, ``state``, ``region``, ``department``, ``contact_email``,
``footer_note``). All fields are strings; empty strings are omitted.

3. dengue_downscale
-------------------

Config sample: ``configs/ka_district_to_subdistrict.yaml``. Dataclasses:
``pipelines/dengue_downscale/configs.py``.

``run``
~~~~~~~

``source_run_id`` (str, required) — run ID whose parent-level predictions are
downscaled. Sentinel ``"latest"`` picks the newest artifact matching
``parent_level`` and the finer brief tolerance (fixed in commit 6d96610).

``downscale``
~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 26 14 26 34

   * - key
     - type
     - default
     - why
   * - ``parent_level``
     - str
     - required
     - Singular region tier the source predictions live at (e.g.
       ``district``).
   * - ``child_level``
     - str
     - required
     - Singular child tier to split onto (e.g. ``subdistrict``). Must differ
       from ``parent_level``.
   * - ``window_weeks``
     - int
     - ``4``
     - Look-back window (weeks of child-level cases) used to derive per-child
       shares of parent cases.
   * - ``historical_fallback_weeks``
     - int | null
     - ``null``
     - Longer window tried when the primary window has zero cases across all
       children of a parent (issue #86). Must strictly exceed ``window_weeks``.
       ``null`` disables (legacy behaviour).
   * - ``cases_csv``
     - str
     - ``prepared_data/mandal/cases_daily.csv``
     - Child-level case history used to derive shares.
   * - ``geojson_base_path``
     - str
     - ``ap_datasets/geojsons/geojsons_AP``
     - Root of the geojson layer used for the child map.
   * - ``on_missing_parents``
     - str
     - ``error``
     - ``error`` or ``warn``. Controls behaviour when a parent has no children
       in the geojson.

``thresholds``
~~~~~~~~~~~~~~

Same schema as the ``dengue`` pipeline's ``thresholds`` block, applied to
child-level predictions. ``method_configs.<name>`` overrides are supported
identically.

``downscale_maps``
~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 22 10 22 46

   * - key
     - type
     - default
     - why
   * - ``enabled``
     - bool
     - ``true``
     - Set false to skip child-level PNG choropleths (interactive maps still
       render).
   * - ``output_dir``
     - str
     - ``outputs/maps``
     - Where PNGs land.
   * - ``figure_title``
     - str
     - ``Dengue risk map``
     - Suptitle.

Common top-level keys
---------------------

``state``
~~~~~~~~~

Two-letter state slug used in artifact paths and report copy. Free-form.

``pipeline``
~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 22 10 14 54

   * - key
     - type
     - default
     - why
   * - ``name``
     - str
     - —
     - Selects the pipeline: ``dengue``, ``dengue_prep``, or
       ``dengue_downscale``.
   * - ``title``
     - str
     - ``""``
     - Human title used in the report header when ``report.document_title`` is
       blank.
   * - ``display_name``
     - str
     - ``""``
     - Alternate hint used the same way as ``title``.

``run``
~~~~~~~

``run_date`` (str, default ``""`` → today) — anchor date for the pipeline.
Blank tracks wall clock.

``logging``
~~~~~~~~~~~

``level`` (str, default ``INFO``) — Python logging level (``DEBUG``, ``INFO``,
``WARNING``, ``ERROR``).

``storages.artifacts``
~~~~~~~~~~~~~~~~~~~~~~

Where the pipeline writes per-run artifacts.

.. list-table::
   :header-rows: 1
   :widths: 26 10 14 50

   * - key
     - type
     - default
     - why
   * - ``kind``
     - str
     - —
     - ``filesystem`` or ``s3``.
   * - ``filesystem.base_path``
     - str
     - —
     - Root path when ``kind=filesystem``. Each run gets an isolated subfolder
       keyed by ``--run-id``.
   * - ``s3.bucket``, ``s3.prefix``
     - str
     - —
     - Bucket / key prefix when ``kind=s3``.

``email``
~~~~~~~~~

``enabled`` (bool, default ``false``) — turn on to have the report step send
the rendered HTML via SMTP. Other keys (``smtp_host``, ``smtp_port``, ``to``,
``from``, ``subject``, credentials via env) are consumed by the email step
directly.
