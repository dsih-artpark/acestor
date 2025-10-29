Running the Pipeline
====================

The Acestor pipeline is executed through the main entry point at ``src/acestor-prod/main.py``.

Basic Usage
-----------

.. code-block:: bash

    python src/acestor-prod/main.py [OPTIONS]

Command-Line Options
--------------------

.. list-table:: Pipeline Arguments
   :header-rows: 1
   :widths: 25 15 60

   * - Option
     - Flag
     - Description
   * - ``--config``
     - ``-c``
     - Path to configuration file (default: ``config/dengue_pipeline.yaml``)
   * - ``--download-linelist``
     - ``-dl``
     - Download linelist data from S3
   * - ``--process-linelist``
     - ``-pl``
     - Process linelist data
   * - ``--download-weather-data-from-cds-api``
     - ``-dw``
     - Download weather data from CDS API
   * - ``--use-weather-data-from-s3``
     - ``-ws``
     - Use weather data from S3
   * - ``--generate-maps``
     - ``-gm``
     - Generate map visualizations of predictions

Pipeline Stages
---------------

The pipeline executes in the following sequence:

1. **Download Case Data** (optional): Downloads ARTPARK linelist data from S3 when ``-dl`` is specified
2. **Process Linelist** (optional): Parses raw linelist data when ``-pl`` is specified
3. **Aggregate Case Data**: Processes and aggregates case data at the configured granularity
4. **Weather Data**: Downloads from CDS API (``-dw``) or uses S3 data (``-ws``)
5. **Identify Cutoff Dates**: Determines prediction windows based on available data
6. **Run Predictions**: Executes district-level predictions
7. **Generate Maps** (optional): Creates geographic visualizations when ``-gm`` is specified

Example Commands
----------------

**Standard run with S3 weather data:**

.. code-block:: bash

    python src/acestor-prod/main.py -ws

**Full pipeline with data download:**

.. code-block:: bash

    python src/acestor-prod/main.py -dl -pl -ws

**Run with custom configuration:**

.. code-block:: bash

    python src/acestor-prod/main.py -c config/custom_config.yaml -ws

**Generate predictions with maps:**

.. code-block:: bash

    python src/acestor-prod/main.py -ws -gm

Configuration File
------------------

The pipeline requires a YAML configuration file specifying:

- ``root_dir``: Root directory for data and outputs
- ``region_name``: Geographic region to process (e.g., "Karnataka")
- ``granularity``: Level of spatial detail ("district" or "subdistrict")
- ``weather_data_path``: Path to weather data
- ``geojson_folder``: Path to GeoJSON boundary files
- ``debug``: Enable debug mode (processes only last year of data)

GeoJSON Folder Structure
-------------------------

The ``geojson_folder`` must contain region-specific boundary files organized hierarchically:

.. code-block:: text

    geojsons/
    ├── <RegionName>/
    │   ├── districts/
    │   │   ├── district_<ID>.geojson
    │   │   └── ...
    │   └── subdistricts/ (optional)
    │       ├── subdistrict_<ID>.geojson
    │       └── ...

**Structure Requirements:**

- Top-level folders named by region (e.g., ``Karnataka``, ``Chhattisgarh``)
- ``districts/`` subfolder containing district boundary GeoJSON files
- ``subdistricts/`` subfolder (required only if ``granularity: subdistrict``)
- File naming: ``district_<ID>.geojson`` or ``subdistrict_<ID>.geojson`` where ID matches the region identifiers in case data

**Example:**

.. code-block:: text

    geojsons/Karnataka/districts/district_524.geojson
    geojsons/Karnataka/subdistricts/subdistrict_5433.geojson
    geojsons/Chhattisgarh/districts/district_374.geojson

Outputs
-------

Pipeline outputs are saved to the ``results/`` directory and include:

- District-level predictions CSV
- State-level predictions CSV
- Log files in ``logs/`` directory
- Map visualizations (when ``-gm`` is specified)
