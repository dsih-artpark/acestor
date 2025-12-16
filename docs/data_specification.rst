Input Data Specification
=========================

The Acestor pipeline requires three types of input data:

1. **Cases Data**: Daily dengue case counts at the specified granularity
2. **Configuration File**: Pipeline configuration settings
3. **GeoJSON Files**: Geographic boundary data for regions

1. Cases Data Specification
============================

The primary input to the Acestor pipeline is the daily dengue case data at the granularity level specified in the configuration file.

File Location
-------------

::

    datasets/cases_{granularity}_daily.csv

Where ``{granularity}`` is specified in the ``dengue_pipeline.yaml`` configuration file (e.g., ``district``, ``subdistrict``, etc.)

File Format
-----------

The file is a comma-separated values (CSV) file with the following structure:

Column Specifications
---------------------

.. list-table:: Case Data Columns
   :header-rows: 1
   :widths: 20 15 65

   * - Column Name
     - Data Type
     - Description
   * - ``region_id``
     - String
     - LGD Standard identifier for the region in the format {region_type}_{lgd_code} (e.g., "district_524", "subdistrict_2345")
   * - ``date``
     - String
     - Date of the observation in YYYY-MM-DD format (e.g., "2021-12-11")
   * - ``case``
     - Float
     - Number of confirmed dengue cases on that date in the region

Data Characteristics
--------------------

- **Temporal Coverage**: Model performs better with more amount of data - Threshold calculation logic is of 2 types. One using historical data and other using past n weeks data. So data must either be recent upto current day, or there must be data of previous years for the model to work accurately.
- **Spatial Granularity**: Configurable (district, subdistrict, etc.) as specified in ``dengue_pipeline.yaml``
- **Geographic Scope**: Indian states and their administrative divisions
- **Primary Metrics**:

  - Number of confirmed dengue cases per day per region

Converting from Linelist Data
------------------------------

If your starting point is linelist (individual case records) rather than aggregated daily case counts, you can add a preprocessing step to convert it to the required daily cases format.

The Acestor module includes a linelist-to-cases conversion function for Karnataka linelist data (``parse_linelist_to_no_of_cases`` in ``ParseCaseData.py``) that can be used as a reference implementation. This function:

- Takes raw linelist data with individual case records
- Filters and aggregates by date and region
- Outputs the required ``cases_{granularity}_daily.csv`` format

See ``ParseCaseData.py:parse_linelist_to_no_of_cases`` for the Karnataka implementation example.

Example Records
---------------

.. code-block:: text

    region_id,date,case
    district_524,2021-12-11,0.0
    district_524,2021-12-12,0.0
    district_524,2021-12-13,1.0
    district_525,2021-12-11,2.0
    district_525,2021-12-12,0.0

Data Quality Requirements
--------------------------

The pipeline performs strict data quality checks before processing. Your data must meet the following requirements:

**Data Completeness**

- **Case Values**: Case counts are non-negative floats
- **Date Format**: All dates follow the YYYY-MM-DD (ISO 8601) format
- **Identifiers**: region_id values must match the corresponding GeoJSON filenames and are based on LGD Standard

**Continuity Requirements**

The pipeline requires continuous weekly data to function properly:

- **Weekly Coverage**: Each ISO week must contain at least **4 days** of data
- **No Gaps**: Data must be continuous with no missing weeks between the start and end dates
- **Cross-Region Consistency**: All regions must have overlapping continuous data periods

**Minimum Data Requirements**

The amount of data determines what operations the pipeline can perform:

.. list-table:: Pipeline Capabilities by Data Duration
   :header-rows: 1
   :widths: 30 70

   * - Data Duration
     - Pipeline Capability
   * - **< 4 months**
     - ❌ Insufficient - Pipeline will exit with an error
   * - **4-12 months**
     - ⚠ Can generate alert thresholds only (no predictions)
   * - **≥ 12 months**
     - ✓ Full functionality - Can generate thresholds AND run predictions

**Data Validation Process**

When you run the pipeline, it automatically:

1. Checks each region for continuous weekly data (≥4 days per ISO week)
2. Identifies the longest continuous data range for each region
3. Finds the common overlapping period across all regions
4. Calculates the data span in months
5. Determines if there's sufficient data to proceed

If validation fails, the pipeline will log detailed error messages indicating:

- Which regions lack continuous data
- The actual data span available
- What minimum data is required

**Example Error Messages**

.. code-block:: text

    ERROR: Insufficient data: Only 3.2 months of continuous data available
    ERROR: Required: At least 4 months to generate thresholds, 12 months to run predictions

    WARNING: Only 8.5 months of data available
    WARNING: Can generate thresholds but cannot run predictions (need >= 12 months)

2. Configuration File Specification
====================================

The pipeline is configured using a YAML configuration file located at ``config/dengue_pipeline.yaml``.

File Location
-------------

::

    config/dengue_pipeline.yaml

Configuration Parameters
------------------------

.. list-table:: Configuration Parameters
   :header-rows: 1
   :widths: 25 15 60

   * - Parameter
     - Data Type
     - Description
   * - ``root_dir``
     - String (Path)
     - Root directory path for the project. All relative paths are resolved from this location.
   * - ``debug``
     - Boolean
     - Set to ``true`` to process only the last 3 years of data for testing/debugging purposes. Set to ``false`` for production runs with all available data.
   * - ``region_name``
     - String
     - Name of the region for weather data downloads (e.g., "Karnataka", "Maharashtra").
   * - ``weather_data_path``
     - String (Path)
     - Path to the directory containing weather data files (absolute or relative to root_dir).
   * - ``geojson_folder``
     - String (Path)
     - Path to the folder containing GeoJSON files for region boundaries (absolute or relative to root_dir).
   * - ``raw_linelist_path``
     - String (Path)
     - Path to the raw linelist data directory (if using linelist data as input).
   * - ``granularity``
     - String
     - Spatial granularity level for analysis. Common values: ``district``, ``subdistrict``. This determines the naming of the cases file (``cases_{granularity}_daily.csv``).
   * - ``ihip_s3_location``
     - String (S3 URI)
     - S3 bucket location for IHIP data fetching (optional, used only if fetching data from IHIP).
   * - ``enable_email_reports``
     - Boolean
     - Set to ``true`` to enable automatic email reports after pipeline completion (default: false).
   * - ``email_recipients``
     - List of Strings
     - List of email addresses to receive reports, in format "Name <email@example.com>".

Example Configuration
---------------------

.. code-block:: yaml

    # main pipeline
    root_dir: /Users/akhil/workspace/acestor
    debug: true  # Set to true to process only last 3 years of data for testing
    region_name: Karnataka  # Name of the region for weather data downloads
    weather_data_path: /Users/akhil/workspace/acestor/weather_data_from_s3
    geojson_folder: /Users/akhil/workspace/acestor/geojsons  # Path to geojson folder
    raw_linelist_path: /Users/akhil/workspace/acestor/datasets/raw_linelist_data/KA_linelist
    granularity: district
    # fetch ihip data
    ihip_s3_location: s3://dsih-artpark-01-raw-data/EPRDS34-KA_IHIP_Dengue_LL/KA/
    # email configuration
    enable_email_reports: true
    email_recipients:
      - "User Name <user@example.com>"

Configuration Notes
-------------------

- The ``granularity`` parameter is crucial as it determines:

  - The expected input file name: ``datasets/cases_{granularity}_daily.csv``
  - The spatial level at which the model runs
  - Which GeoJSON files are required

- When ``debug`` is enabled, only the most recent **3 years** of data is processed, significantly reducing processing time for testing.
- All path parameters support both absolute and relative paths (relative to ``root_dir``).

Email Configuration
-------------------

The pipeline can automatically send email reports with pipeline status, attached results, and maps after completion.

**Environment Variables**

Email functionality requires SMTP credentials to be configured in a ``.env`` file in the project root:

.. code-block:: bash

    # SMTP Configuration
    SMTP_SERVER=smtp.gmail.com
    PORT=587
    EMAIL=your.email@gmail.com
    PASSWORD=your_app_specific_password

**For Gmail Users:**

1. Enable 2-factor authentication on your Google account
2. Generate an App-Specific Password at https://myaccount.google.com/apppasswords
3. Use the app-specific password (not your regular Gmail password)

**Email Report Contents:**

When enabled, the pipeline sends an HTML email report containing:

- Pipeline status for each major step (data processing, thresholds, predictions, maps)
- Color-coded status indicators (green for success, red for errors, orange for warnings)
- Attached files:

  - ``thresholds_df.csv`` - Generated alert thresholds
  - ``Predictions_*.csv`` - Model prediction results
  - Map images (PNG/PDF) if ``-gm`` flag was used

- Pipeline execution time and completion summary

**Error Handling:**

- If email sending fails, the pipeline logs the error but continues execution
- The pipeline will complete successfully even if email delivery fails
- Check the log files for email-related error messages

3. GeoJSON File Specification
==============================

The pipeline requires GeoJSON files for each region being analyzed. These files define the geographic boundaries used for spatial analysis and visualization.

Folder Structure
----------------

GeoJSON files should be organized in the following directory structure:

::

    {geojson_folder}/
    └── {state_name}/
        ├── districts/
        │   ├── district_{lgd_code}.geojson
        │   └── district_{lgd_code}.geojson
        └── subdistricts/
            ├── subdistrict_{lgd_code}.geojson
            └── subdistrict_{lgd_code}.geojson

Where:
- ``{geojson_folder}`` is the base path specified in ``dengue_pipeline.yaml``
- ``{state_name}`` is the name of the state (e.g., "Karnataka", "Maharashtra")
- Files are organized in ``districts/`` or ``subdistricts/`` subdirectories based on granularity

File Location
-------------

::

    {geojson_folder}/{state_name}/{granularity_plural}/{region_type}_{lgd_code}.geojson

Example paths:

::

    geojsons/Karnataka/districts/district_524.geojson
    geojsons/Karnataka/subdistricts/subdistrict_2345.geojson
    geojsons/Maharashtra/districts/district_360.geojson

Naming Convention
-----------------

GeoJSON files must follow the LGD Standard naming format:

- **District**: ``district_529.geojson`` (where 529 is the LGD district code)
- **Subdistrict**: ``subdistrict_2345.geojson`` (where 2345 is the LGD subdistrict code)
- **State**: ``state_29.geojson`` (where 29 is the LGD state code)

This naming must match the identifiers used in the ``region_id`` column of the cases data file.

File Format
-----------

GeoJSON files must follow the standard GeoJSON specification (RFC 7946) with the following structure:

.. code-block:: json

    {
      "id": "district_123",
      "type": "Feature",
      "properties": {
        "regionName": "DISTRICT NAME",
        "Shape_Leng": 123.456,
        "Shape_Area": 789.012,
        "regionType": "district",
        "parentID": "state_12",
        "parentName": "STATE NAME"
      },
      "geometry": {
        "type": "Polygon",
        "coordinates": [
          [
            [75.123, 15.456],
            [75.234, 15.567],
            [75.345, 15.678],
            [75.123, 15.456]
          ]
        ]
      }
    }

Requirements
------------

- **Feature ID**: Each feature must have a top-level ``id`` field matching the filename pattern (e.g., "district_524")
- **Coordinate System**: WGS84 (EPSG:4326) - longitude/latitude coordinates
- **Geometry Types**: Typically ``Polygon`` or ``MultiPolygon`` for administrative boundaries
- **Properties**: The following properties are **strictly required** for each feature:

  - ``regionName``: Name of the region (e.g., "BAGALKOTE", "KARNATAKA")
  - ``regionType``: Type of administrative division (e.g., "district", "subdistrict", "state")
  - ``parentID``: ID of the parent administrative unit (e.g., "state_29")
  - ``parentName``: Name of the parent administrative unit (e.g., "KARNATAKA")
  - ``Shape_Leng``: Perimeter/length of the shape (float)
  - ``Shape_Area``: Area of the shape (float)

- **File Organization**: All GeoJSON files must be in the folder structure specified by ``geojson_folder`` in the configuration

GeoJSON Quality Notes
----------------------

- **ID Matching**: The ``id`` field in the GeoJSON must exactly match:

  - The filename (e.g., file ``district_524.geojson`` must have ``"id": "district_524"``)
  - The corresponding ``region_id`` values in the cases data CSV

- **Geometry Accuracy**: Ensure boundary geometries are accurate and up-to-date
- **Coordinate Order**: Coordinate pairs must be in [longitude, latitude] order (per GeoJSON specification)
- **Performance**: Complex polygons may need simplification for better performance
- **Validation**: Files must be valid GeoJSON (can be validated using tools like geojsonlint.com)
- **Naming Convention**: Region names in ``regionName`` should follow LGD Standard naming conventions (typically uppercase)
