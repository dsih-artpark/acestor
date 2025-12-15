Running the Pipeline
====================

The Acestor pipeline is executed through the main entry point at ``src/acestor/main.py``.

Basic Usage
-----------

.. code-block:: bash

    python src/acestor/main.py [OPTIONS]

Command-Line Options
--------------------

.. list-table:: Pipeline Arguments
   :header-rows: 1
   :widths: 30 15 55

   * - Option
     - Flag
     - Description
   * - ``--config``
     - ``-c``
     - Path to configuration file (default: ``config/dengue_pipeline.yaml``)
   * - ``--date``
     - ``-d``
     - Date to run predictions for in YYYY-MM-DD format (default: today's date)
   * - ``--download-linelist``
     - ``-dl``
     - Download linelist data from S3 (default: False)
   * - ``--process-linelist``
     - ``-pl``
     - Process linelist data to generate daily case counts (default: False)
   * - ``--download-and-use-weather-data-from-s3``
     - ``-ws``
     - Download and use weather data from S3 (default: False)
   * - ``--use-previously-downloaded-weather-data-from-s3``
     - ``-rws``
     - Use previously downloaded weather data from S3 without re-downloading (default: False)
   * - ``--generate-thresholds``
     - ``-t``
     - Generate alert thresholds based on historical data (default: False)
   * - ``--model-train-and-predict``
     - ``-m``
     - Train the model and generate predictions (default: False)
   * - ``--generate-maps``
     - ``-gm``
     - Generate map visualizations of predictions (default: False)

Pipeline Stages
---------------

The pipeline executes in the following sequence:

1. **Download Case Data** (optional): Downloads linelist data from S3 when ``-dl`` is specified
2. **Process Linelist** (optional): Converts raw linelist data to daily case counts when ``-pl`` is specified
3. **Aggregate Case Data**: Validates and aggregates case data at the configured granularity
4. **Weather Data**: Downloads and uses S3 data (``-ws``) or uses previously downloaded data (``-rws``)
5. **Generate Thresholds** (optional): Calculates alert thresholds when ``-t`` is specified
6. **Model Training & Predictions** (optional): Trains models and generates predictions when ``-m`` is specified
7. **Generate Maps** (optional): Creates geographic visualizations when ``-gm`` is specified

Example Commands
----------------

**Standard run with freshly downloaded S3 weather data:**

.. code-block:: bash

    python src/acestor/main.py -ws -t -m

**Use previously downloaded weather data (faster):**

.. code-block:: bash

    python src/acestor/main.py -rws -t -m

**Full pipeline from linelist data:**

.. code-block:: bash

    python src/acestor/main.py -dl -pl -ws -t -m

**Run with custom configuration:**

.. code-block:: bash

    python src/acestor/main.py -c config/custom_config.yaml -ws -t -m

**Generate predictions with maps:**

.. code-block:: bash

    python src/acestor/main.py -ws -t -m -gm

**Run predictions for a specific date:**

.. code-block:: bash

    python src/acestor/main.py -d 2024-12-01 -rws -t -m

**Quick test run (using previously downloaded weather data, no maps):**

.. code-block:: bash

    python src/acestor/main.py -rws -t -m

Configuration File
------------------

The pipeline requires a YAML configuration file specifying:

- ``root_dir``: Root directory for data and outputs
- ``region_name``: Geographic region to process (e.g., "Karnataka")
- ``granularity``: Level of spatial detail ("district" or "subdistrict")
- ``weather_data_path``: Path to weather data
- ``geojson_folder``: Path to GeoJSON boundary files
- ``raw_linelist_path``: Path to raw linelist data (if using linelist input)
- ``ihip_s3_location``: S3 bucket location for IHIP data (if downloading from S3)
- ``debug``: Enable debug mode (processes only last 3 years of data)

See the :doc:`data_specification` page for detailed configuration file documentation.

Important Notes
---------------

**Weather Data Options:**

- Use ``-ws`` for the first run or when you need fresh weather data
- Use ``-rws`` for subsequent runs to save time (reuses previously downloaded data)
- Weather data is cached in the ``weather_data_path`` specified in the config

**Threshold and Prediction Flags:**

- Both ``-t`` (thresholds) and ``-m`` (predictions) are typically used together
- ``-t`` generates alert thresholds based on historical data
- ``-m`` trains models and generates predictions
- These are separate flags to allow flexibility in pipeline execution

**Date Selection:**

- By default, the pipeline runs predictions for today's date
- Use ``-d`` to run predictions for historical dates or specific future dates
- Date must be in YYYY-MM-DD format

GeoJSON Files
-------------

GeoJSON boundary files must be organized in the folder specified by ``geojson_folder`` in the configuration:

.. code-block:: text

    geojsons/
    └── <StateName>/
        ├── districts/
        │   └── district_<LGD_CODE>.geojson
        └── subdistricts/
            └── subdistrict_<LGD_CODE>.geojson

See the :doc:`data_specification` page for detailed GeoJSON requirements and format specifications.

Outputs
-------

Pipeline outputs are saved to the ``results/`` directory and include:

- District-level predictions CSV
- State-level predictions CSV
- Log files in ``logs/`` directory
- Map visualizations (when ``-gm`` is specified)
