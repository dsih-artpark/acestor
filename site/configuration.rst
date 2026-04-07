Configuration Reference
=======================

All pipeline behaviour is controlled by a single YAML file. Start from an example in ``configs-example/`` and edit it to suit your deployment.

Environment variables can be used anywhere in the config with ``${VAR}`` or ``${VAR:-default}`` syntax — they are resolved at load time. Never commit secrets directly to config files.

pipeline
--------

.. code-block:: yaml

    pipeline:
      name: "dengue"           # used as a prefix in logs and artifact paths
      title: "Dengue Intelligence"  # used in report titles and email subjects

run
---

.. code-block:: yaml

    run:
      run_date: "2026-04-07"   # reference date for this run (empty = today)

storages
--------

Where pipeline artifacts are written. Supports filesystem and S3 backends.

.. code-block:: yaml

    storages:
      artifacts:
        kind: filesystem
        filesystem:
          base_path: "./artifacts"   # outputs go under {base_path}/{run_id}/

    # S3 alternative:
    storages:
      artifacts:
        kind: s3
        s3:
          bucket: my-bucket
          base_prefix: "artifacts/"
          profile: default
          region: ap-south-1

data
----

case_download
~~~~~~~~~~~~~

Controls how raw case data is fetched.

.. code-block:: yaml

    data:
      case_download:
        enabled: true
        source_backend: "filesystem"   # "filesystem" or "s3"
        source_path: "datasets/raw_linelist_data/KA_linelist"
        cache_enabled: true
        cache_dir: "./cache/raw_case"
        cache_strategy: "local_first"  # "local_first" | "local" | "cloud_first"

case_parse
~~~~~~~~~~

Controls how the raw case data is parsed into daily case series.

.. code-block:: yaml

    data:
      case_parse:
        region_types: ["district"]     # ["district"] or ["zone", "corp"] etc.
        date_start: "2015-01-01"
        date_end: ""                   # empty = run_date

case_sufficiency
~~~~~~~~~~~~~~~~

Early validation gate. The pipeline exits here if data does not meet the minimum requirements.

.. code-block:: yaml

    data:
      case_sufficiency:
        enabled: true
        min_total_rows: 30
        min_distinct_regions: 2
        min_date_span_days: 14

geojson
~~~~~~~

Path to the folder containing GeoJSON boundary files.

.. code-block:: yaml

    data:
      geojson:
        base_path: "datasets/geojsons/geojsons_GBA"

GeoJSON files must follow the layout ``{base_path}/{region_type}s/{region_id}.geojson``. See :doc:`data_specification` for the full GeoJSON format.

weather_download
~~~~~~~~~~~~~~~~

Controls how weather data is fetched. Use ``source_mode: "filesystem"`` to read from a local cache, or ``"cds"`` to download fresh data from the Copernicus CDS API.

.. code-block:: yaml

    data:
      weather_download:
        enabled: true
        source_mode: "filesystem"      # "filesystem" or "cds"
        netcdf_cache_path: "./datasets/netcdf"
        parsed_output_path: "./datasets/parsednetcdf"
        region_type: "district"
        w_params: ["t2m", "d2m", "tp"]
        threshold_km: 25.0
        bounds_resolution_deg: 0.1
        start_date: "2015-01-01"
        end_date: "2024-07-01"

weather_parse
~~~~~~~~~~~~~

Aggregation rules applied to the raw weather variables.

.. code-block:: yaml

    data:
      weather_parse:
        region_type: "district"
        weather_variables: ["2mTemperature", "totalPrecipitation", "2mDewpointTemperature"]
        daily_agg:
          - name: "2mTemperature"
            op: "mean"
          - name: "2mTemperature"
            op: "max"
            output_name: "2mTemperature_max"
        rolling_agg:
          - name: "2mTemperature"
            op: "mean"
        rolling_n_days: 7
        sampling_rate: 7
        intermediate_col_rename:
          "2mTemperature": "t2m_mean"
          "totalPrecipitation": "tp_sum"
          "2mDewpointTemperature": "d2m_mean"

cutoff
------

Minimum number of regions required in the case and weather datasets after cutoff filtering. Runs with fewer regions than these thresholds are aborted.

.. code-block:: yaml

    cutoff:
      case_min_regions: 10
      weather_min_regions: 25

thresholds
----------

.. code-block:: yaml

    thresholds:
      region_type: "district"
      n_weeks: 4                 # number of recent weeks used for the "previousNweeks" method
      historical_n_years: 5      # years of history used for the "historical" method
      excluded_years: [2020, 2021]  # years excluded from threshold calculation (e.g. pandemic years)

model
-----

.. code-block:: yaml

    model:
      spatial_res: "district"
      data_features:
        - "case"
        - "recordDate"
        - "recordYear"
        - "recordMonth"
        - "ISOWeek"
        - "t2m_mean"
        - "tp_sum"
        - "d2m_mean"
      lag:
        lag_temp: [12]           # temperature lag in days
        lag_rf: [4]              # rainfall / humidity lag in days
      years_to_exclude: [2020, 2021]
      list_alpha: [1.0, 2.0]    # NBR alpha parameters

assess
------

.. code-block:: yaml

    assess:
      total_corp_regions: 5
      total_zone_regions: 10

maps
----

.. code-block:: yaml

    maps:
      output_dir: "plots"
      figure_title: "Dengue Risk Map"

report
------

.. code-block:: yaml

    report:
      output_dir: "reports"
      compile_pdf: false         # set true if pdflatex is installed

report_distribution
-------------------

Metadata embedded in the generated reports.

.. code-block:: yaml

    report_distribution:
      system_name: "Dengue Early Warning System"
      organization: "ARTPARK, IISc Bengaluru"
      state: "Karnataka"
      region: "Greater Bengaluru Area (GBA)"
      department: "Directorate of Health & Family Welfare, GoK"

email
-----

Run completion notifications via SMTP.

.. code-block:: yaml

    email:
      enabled: true
      on: ["success"]            # "success", "failed", or both
      from: "${SMTP_FROM}"
      to:
        - "user@example.com"
      smtp:
        host: "${SMTP_HOST}"
        port: 587
        use_tls: true
        username: "${SMTP_USERNAME}"
        password: "${SMTP_PASSWORD}"

schedule
--------

Used by ``scripts/run_schedules.py`` to configure recurring runs.

.. code-block:: yaml

    schedule:
      cron: "0 6 * * 1"         # every Monday at 06:00 UTC
      timezone: "Asia/Kolkata"
      pipeline: "pipelines.dengue.pipeline:build_pipeline"
      config: "configs/gba_stage1.yaml"

logging
-------

.. code-block:: yaml

    logging:
      level: INFO                # DEBUG, INFO, WARNING, ERROR

Full Example
------------

A minimal working config for a local filesystem run:

.. code-block:: yaml

    pipeline:
      name: dengue
      title: "Dengue Intelligence"

    run:
      run_date: ""               # empty = today

    storages:
      artifacts:
        kind: filesystem
        filesystem:
          base_path: "./artifacts"

    data:
      case_download:
        enabled: true
        source_backend: filesystem
        source_path: datasets/raw_linelist_data/KA_linelist

      case_parse:
        region_types: [district]
        date_start: "2015-01-01"
        date_end: ""

      case_sufficiency:
        enabled: true
        min_total_rows: 30
        min_distinct_regions: 2
        min_date_span_days: 14

      geojson:
        base_path: datasets/geojsons

      weather_download:
        enabled: true
        source_mode: filesystem
        netcdf_cache_path: datasets/netcdf
        parsed_output_path: datasets/parsednetcdf
        region_type: district
        w_params: [t2m, d2m, tp]
        start_date: "2015-01-01"
        end_date: ""

    thresholds:
      region_type: district
      n_weeks: 4
      historical_n_years: 5
      excluded_years: [2020, 2021]

    model:
      spatial_res: district
      years_to_exclude: [2020, 2021]

    report:
      output_dir: reports
      compile_pdf: false

    email:
      enabled: false

    logging:
      level: INFO
