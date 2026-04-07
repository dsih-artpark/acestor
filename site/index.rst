Acestor Documentation
=====================

**Acestor** is a production dengue intelligence pipeline. It ingests case and weather data, estimates risk thresholds, runs forecasting models, produces choropleth maps, and generates report artifacts — all driven from a single YAML configuration file.

The pipeline is built around a **14-step DAG** that executes steps concurrently where possible. Each step reads typed inputs from upstream steps and writes its outputs to an artifact store.

.. list-table:: Pipeline Stages
   :header-rows: 1
   :widths: 5 30 65

   * - #
     - Step
     - What it does
   * - 1
     - ``identify_sampling_day``
     - Resolves the run date and case sampling window
   * - 2
     - ``download_case_data``
     - Fetches or copies raw case inputs (filesystem or S3)
   * - 3
     - ``download_weather_data``
     - Fetches weather data from CDS API or local cache
   * - 4
     - ``parse_case_data``
     - Parses linelist → daily case series by region
   * - 5
     - ``validate_case_data_sufficiency``
     - Early gate — stops if data is too thin to model
   * - 6
     - ``parse_weather_data``
     - Aggregates ERA5 weather features aligned to regions
   * - 7
     - ``identify_cutoff_dates``
     - Computes case/weather cutoffs and prediction calendar
   * - 8
     - ``generate_thresholds``
     - Builds per-region threshold tables from historical data
   * - 9
     - ``train_and_predict``
     - Fits NBR and TSE models, writes predictions
   * - 10
     - ``combine_predictions``
     - Merges region predictions into a single table
   * - 11
     - ``assess_thresholds``
     - Threshold assessment and figure metadata
   * - 12
     - ``generate_maps``
     - Produces choropleth map PNGs
   * - 13
     - ``generate_report``
     - Bundles JSON metadata, LaTeX source, and maps zip
   * - 14
     - ``notify_run``
     - Sends success/failure email (if configured)

.. toctree::
   :maxdepth: 2
   :caption: Contents:
   :hidden:

   installation
   usage
   configuration
   data_specification
   output_format

.. toctree::
   :maxdepth: 1
   :caption: Links:
   :hidden:

   GitHub Repository <https://github.com/dsih-artpark/acestor>
