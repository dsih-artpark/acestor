Data Specification
==================

Overview
--------

acestor's dengue forecasting pipeline runs in two stages, each with its own
input surface:

1. **Raw case data** — the IHIP linelist (per-case rows). Input to
   ``dengue_prep``.
2. **Prepared data** — ``cases_daily.csv`` and ``weather_daily.csv`` under
   ``prepared_data/{region_type}/``. Output of ``dengue_prep``, input to the
   main ``dengue`` pipeline.
3. **GeoJSON files** — region boundaries and identifiers. Read by both
   stages (spatial join, parent-chain rollup, region metadata).

Weather is downloaded at prep-time from the openmeteo API by default — there
is no raw local weather input.

1. Raw case data (IHIP linelist)
--------------------------------

The prep parser (``pipelines/dengue_prep/lib/ihip.py``) reads every
``.xlsx``, ``.xls``, and ``.csv`` file under
``data.case_download.source_path``. Each row is treated as one confirmed
case; case counts are produced by grouping on ``(region_id, date)``.

Only the date column is strictly required. Region resolution and row
filtering are configurable — the parser adapts to whichever columns the
file provides.

Canonical IHIP L-form columns
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Dashboard exports (``/api/cases/export.xlsx``) and standard IHIP linelists
emit the 30 L-form columns below. The parser only requires the date column;
the rest are optional and consulted opportunistically by row filters,
region resolvers, and geocoding.

.. code-block:: text

    Case No, Aadhar No, Patient Name, Age, Gender, Contact Number,
    Address, Village/Town/Ward, District, State, Pincode,
    Latitude, Longitude,
    Date Of Onset, Sample Collected Date, Test Performed Date,
    Confirmed Diagnosis, Test Name, Test Result,
    Hospitalised, Hospital Name, Outcome,
    Reporting Unit, Reporting District, Reporting State,
    Region Id, District Code, Block Code, Ward Code, ULB Code

Date column — candidate fallback
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``data.case_parse.date_column`` accepts either a string or a list. When a
list is given, the parser walks it top-to-bottom per file and keeps the
first column that yields any parseable rows. This lets one config span
sources with different date semantics (e.g. dashboard exports populate
``Test Performed Date``, some state files only have ``Sample Collected
Date``).

.. code-block:: yaml

    data:
      case_parse:
        date_column:
          - Test Performed Date
          - Date Of Onset
          - Sample Collected Date

Both ISO (``YYYY-MM-DD``) and day-first (``dd/mm/yyyy``) formats are tried
for each candidate; whichever parses more rows wins.

Region resolution
~~~~~~~~~~~~~~~~~

Four resolvers, tried in priority order until one applies:

1. **Pre-resolved ``region_id`` column** — set
   ``data.case_parse.region_id_column: "Region Id"`` (dashboard exports).
   The parser trusts the column verbatim. IDs at a finer granularity than
   ``data.region_type`` (e.g. ward IDs in a district-level run) are
   rolled up via the geojson ``parent`` chain.
2. **LGD code column** — set ``lgd_code_column: "District Code"``. The
   integer code is mapped directly: ``502 → district_502``. Fast and
   exact.
3. **Geocoding** — enabled via ``data.case_parse.geocoding.enabled: true``.
   Composes a free-text address from configured fields, geocodes via
   Google, PIPs the result against the region layer.
4. **Spatial join** — falls back to point-in-polygon on ``Latitude`` /
   ``Longitude`` columns. Points just outside a polygon are snapped to
   the nearest one within 1000 m; anything farther is dropped.

Rows resolved to a ``region_id`` that isn't present in the target
geojson layer are dropped (out-of-scope guard).

Row filters
~~~~~~~~~~~

``data.case_parse.filters`` is a list of ``{column, values}`` entries.
Rows must match every entry (AND); within an entry, any listed value
matches (OR). Missing columns are logged and skipped.

.. code-block:: yaml

    data:
      case_parse:
        filters:
          - column: Confirmed Diagnosis
            values: [Dengue]
          - column: Test Result
            values: [Positive]

Case sources
~~~~~~~~~~~~

``data.case_download.source_mode`` selects a plugin from
``pipelines/dengue_prep/lib/case_sources/``:

- ``filesystem`` — read files from ``source_path``.
- ``dashboard`` — pull IHIP-shaped XLSX from a running dengue-dashboard
  backend via ``POST /api/auth/login`` +
  ``GET /api/cases/export.xlsx?from=…&to=…``. Handles chunked backfill
  and incremental refetch (``backfill_days``) automatically.

External sources can be loaded by file path or dotted module path.

2. Prepared case data
---------------------

Written to ``prepared_data/{region_type}/cases_daily.csv`` (full overwrite
per run). Three columns, aggregated to one row per ``(region_id, date)``.

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Column
     - Type
     - Description
   * - ``date``
     - ISO date (``YYYY-MM-DD``)
     - Case date, chosen from the configured ``date_column`` candidates.
   * - ``region_id``
     - String
     - LGD-standard identifier — ``{region_type}_{lgd_code}``.
   * - ``case_count``
     - Integer
     - Confirmed cases in ``region_id`` on ``date`` (row count after
       filters).

.. code-block:: text

    date,region_id,case_count
    2021-02-03,district_344,1
    2021-08-21,district_344,1
    2021-09-14,district_344,3

3. Prepared weather data
------------------------

Written to ``prepared_data/{region_type}/weather_daily.csv`` (full
overwrite per run). Eight columns, one row per ``(region_id, date)``.

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Column
     - Type
     - Description
   * - ``region_id``
     - String
     - LGD-standard identifier.
   * - ``date``
     - ISO date
     - Observation date.
   * - ``2mTemperature``
     - Float, **Kelvin**
     - Daily mean 2 m air temperature (ERA5 ``t2m``).
   * - ``2mDewpointTemperature``
     - Float, **Kelvin**
     - Daily mean 2 m dew-point temperature (ERA5 ``d2m``).
   * - ``totalPrecipitation``
     - Float, **metres**
     - Daily total precipitation (ERA5 ``tp``).
   * - ``name``
     - String
     - Region name (from geojson).
   * - ``parent``
     - String
     - Parent region identifier (from geojson).
   * - ``parent_name``
     - String
     - Parent region name (from geojson).

**Units on disk are always Kelvin (temperature) and metres
(precipitation), regardless of source.** Sources declare their native
units via ``data.weather_download.temperature_unit`` and
``precipitation_unit``; the download step normalises before writing.
A detection guard (``normalize_temperature``) compares declared vs
observed magnitude and refuses double-conversion when the declared unit
disagrees with the data.

.. code-block:: text

    region_id,date,2mTemperature,2mDewpointTemperature,totalPrecipitation,name,parent,parent_name
    district_344,2016-01-02,294.6357301806407,289.9042182571895,2.070384761243376e-05,ANUGUL,Odisha,Odisha
    district_344,2016-01-03,294.6549358409932,289.62343289514274,5.913023588065236e-05,ANUGUL,Odisha,Odisha

The default source is openmeteo (ERA5 archive). Its ~5-day archive lag
is applied silently — requested end dates within the lag window are
capped, and empty chunks are logged and skipped rather than errored.

4. GeoJSON files
----------------

Layout under ``data.geojson.base_path``:

.. code-block:: text

    {base_path}/
    ├── districts/
    │   ├── district_344.geojson
    │   └── ...
    ├── blocks/
    │   └── block_<lgd>.geojson
    ├── ulbs/
    │   └── ulb_<lgd>.geojson
    └── ulb_wards/
        └── ulb_ward_<lgd>.geojson

Subdirectory names are **plural** (``districts/``, ``ulbs/``,
``ulb_wards/``). The parser strips the trailing ``s`` to derive the
region-type prefix used in ``region_id`` (``district_``, ``ulb_``,
``ulb_ward_``). Longest-prefix classification is used so ``ulb_ward_``
IDs are not misclassified as ``ulb_``.

Files are RFC 7946 GeoJSON in WGS84 (EPSG:4326). A file may be a single
``Feature`` or a ``FeatureCollection``. Geometry is ``Polygon`` or
``MultiPolygon``.

Feature properties
~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 20 55

   * - Property
     - Type
     - Description
   * - ``region_id``
     - String, required
     - ``{region_type}_{lgd_code}`` — must match the filename stem.
   * - ``name``
     - String, required
     - Region name (used in weather CSV and summary reports).
   * - ``parent``
     - String, required
     - Parent ``region_id`` for the hierarchy walk (e.g.
       ``state_21``). May be empty at the root.
   * - ``parent_name``
     - String
     - Parent region name.

State-specific extras (e.g. ``dtcode11``, ``code2011``, ``state_lgd``)
are preserved but ignored by the pipeline.

.. code-block:: json

    {
      "type": "Feature",
      "properties": {
        "region_id": "district_344",
        "name": "ANGUL",
        "parent": "state_21",
        "parent_name": "ODISHA",
        "dtcode11": "384",
        "code2011": "213840160",
        "state_lgd": 21
      },
      "geometry": {
        "type": "MultiPolygon",
        "coordinates": [ ... ]
      }
    }

5. Data requirements
--------------------

- **Minimum span** — controlled by ``min_date_span_days`` in the main
  ``dengue`` pipeline config. Runs shorter than this exit at the
  sufficiency gate.
- **4–12 months** — thresholds only, no model predictions.
- **≥ 12 months** — full pipeline (thresholds + predictions).
- **Continuity** — each ISO week must contain at least 4 days of data;
  weeks must be contiguous.
- **Region coverage** — ``min_distinct_regions`` (main pipeline config)
  gates whether there is enough spatial coverage to fit the neighbour
  model. Regions with LGD code 0 (placeholder / Union-Territory
  enclaves) are dropped from the prepared weather CSV.
- **Alignment** — every ``region_id`` emitted by prep must exist in the
  geojson layer for the configured ``region_type``. The prep out-of-scope
  guard enforces this; the main pipeline assumes it.
