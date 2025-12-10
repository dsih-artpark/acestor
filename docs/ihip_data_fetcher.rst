IHIP Data Fetcher and Parser
=============================

Overview
--------

The IHIP Data Fetcher (``fetch_and_parse_ihip_data.py``) is a standalone script that automates the complete pipeline for fetching, processing, and aggregating IHIP (Integrated Health Information Platform) dengue surveillance data from AWS S3. It transforms raw linelist data into analysis-ready datasets for epidemiological modeling.

Location: ``src/acestor/fetch_and_parse_ihip_data.py``

Pipeline Workflow
-----------------

The script executes four sequential steps:

1. **S3 Download**: Fetches all IHIP linelist files from configured S3 bucket
2. **Format Conversion**: Converts XLSX files to CSV format for standardized processing
3. **Data Merging**: Consolidates multiple CSV files into a single unified linelist
4. **Aggregation**: Creates district-level daily positive case counts with standardized region identifiers

Data Source Configuration
--------------------------

The script retrieves data from AWS S3 using the following configuration:

.. list-table:: Data Source Parameters
   :header-rows: 1
   :widths: 30 70

   * - Parameter
     - Details
   * - **S3 Location**
     - Defined in ``config/dengue_pipeline.yaml`` (``ihip_s3_location``)
   * - **File Format**
     - XLSX spreadsheets containing dengue test records
   * - **Authentication**
     - AWS credentials from ``.env`` file (``AWS_ACCESS_KEY``, ``AWS_SECRET_KEY``)
   * - **Bucket**
     - Parsed from S3 URI (e.g., ``s3://dsih-artpark-01-raw-data/...``)

Data Transformations
--------------------

Column Mapping
~~~~~~~~~~~~~~

The script performs the following column transformations:

.. list-table:: Column Renaming
   :header-rows: 1
   :widths: 40 40 20

   * - Original Column
     - Final Column
     - Action
   * - ``Sub District``
     - ``subdistrict.name``
     - Renamed
   * - ``District``
     - ``district.name``
     - Renamed
   * - ``State``
     - ``state.name``
     - Renamed
   * - ``Test Result``
     - ``test_result``
     - Renamed
   * - ``Date Of Onset``
     - (merged into ``date``)
     - Priority 1
   * - ``Sample Collected Date``
     - (merged into ``date``)
     - Priority 2
   * - ``Test Performed Date``
     - (merged into ``date``)
     - Priority 3

Date Consolidation Logic
~~~~~~~~~~~~~~~~~~~~~~~~

A single ``date`` column is created using priority-based selection:

1. **First priority**: Use ``Date Of Onset`` if available
2. **Second priority**: Use ``Sample Collected Date`` if onset date is missing
3. **Third priority**: Use ``Test Performed Date`` if both previous dates are missing

All dates are standardized to **YYYY-MM-DD** format using ``pandas.to_datetime()``.

.. note::
   Unparseable dates are set to null and logged as warnings.

District Name Corrections
~~~~~~~~~~~~~~~~~~~~~~~~~

The script corrects known spelling discrepancies between IHIP data and the region ID database:

.. list-table:: District Name Fixes
   :header-rows: 1
   :widths: 50 50

   * - IHIP Spelling
     - Corrected Name (in regionids.csv)
   * - Uttar Kannad
     - UTTARA KANNADA
   * - Dakshin Kannad
     - DAKSHINA KANNADA
   * - Bagalkot
     - BAGALKOTE
   * - Chikballapur
     - CHIKKABALLAPURA

These corrections ensure accurate matching with standardized region identifiers.

Region ID Mapping
~~~~~~~~~~~~~~~~~

The script merges with ``data/regionids.csv`` to add standardized identifiers:

- **district.ID**: Standard district identifier (e.g., ``district_550``)
- **state.ID**: Standard state identifier (e.g., ``state_29``)

The mapping process:

1. Normalizes district and state names (uppercase, strip whitespace)
2. Applies district name corrections
3. Performs left join with region ID database
4. Logs warnings for unmatched districts

Case Aggregation
~~~~~~~~~~~~~~~~

The aggregation logic creates daily district-level summaries:

- **Grouping**: By ``date`` and ``district.name``
- **Filtering**: Only records with ``test_result = "POSITIVE"`` (case-insensitive)
- **Counting**: Number of positive test results per group
- **Preserving**: District name, state name, district ID, and state ID

Output Files
------------

The script generates three output artifacts:

1. Raw Downloads (``data/ihip/linelist/``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Individual files downloaded from S3:

- Original XLSX files
- Converted CSV files
- Intermediate processing artifacts

2. Consolidated Linelist (``data/ihip/linelist.csv``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Merged dataset with all individual test records.

.. list-table:: Linelist Schema
   :header-rows: 1
   :widths: 30 70

   * - Column
     - Description
   * - ``date``
     - Test/onset date (YYYY-MM-DD)
   * - ``subdistrict.name``
     - Sub-district name
   * - ``district.name``
     - District name (corrected)
   * - ``state.name``
     - State name
   * - ``test_result``
     - Test result (e.g., POSITIVE, NEGATIVE)

3. Aggregated Cases (``datasets/cases_district_daily.csv``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Daily positive case counts by district with region identifiers.

.. list-table:: Cases Schema
   :header-rows: 1
   :widths: 25 75

   * - Column
     - Description
   * - ``date``
     - Date of cases (YYYY-MM-DD)
   * - ``district.ID``
     - Standard district identifier (e.g., district_550)
   * - ``district.name``
     - District name (uppercase, corrected)
   * - ``state.ID``
     - Standard state identifier (e.g., state_29)
   * - ``state.name``
     - State name
   * - ``case``
     - Number of positive test results for this district on this date

**Data Characteristics**:

- One row per district per date (only dates with positive cases)
- Sorted by date and district name
- All district names in uppercase
- Missing region IDs logged as warnings

Usage
-----

Basic Command
~~~~~~~~~~~~~

Run with default output location:

.. code-block:: bash

    python src/acestor/fetch_and_parse_ihip_data.py

This outputs the aggregated cases file to ``datasets/cases_district_daily.csv``.

Custom Output File
~~~~~~~~~~~~~~~~~~

Specify a custom output location:

.. code-block:: bash

    python src/acestor/fetch_and_parse_ihip_data.py -o path/to/custom_output.csv

Or using the long form:

.. code-block:: bash

    python src/acestor/fetch_and_parse_ihip_data.py --output-file path/to/custom_output.csv

Help
~~~~

Display command-line options:

.. code-block:: bash

    python src/acestor/fetch_and_parse_ihip_data.py -h

Command-Line Arguments
----------------------

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Flag
     - Type
     - Description
   * - ``-o``, ``--output-file``
     - string
     - Output file path for aggregated cases (default: ``datasets/cases_district_daily.csv``)

Configuration Requirements
--------------------------

Required Files
~~~~~~~~~~~~~~

The script requires the following files to be present:

1. **config/dengue_pipeline.yaml**

   Must contain the ``ihip_s3_location`` parameter:

   .. code-block:: yaml

       ihip_s3_location: s3://bucket-name/prefix/path/

2. **.env**

   Must contain AWS credentials:

   .. code-block:: bash

       AWS_ACCESS_KEY=your_access_key
       AWS_SECRET_KEY=your_secret_key

3. **data/regionids.csv**

   Region ID mapping database with columns:

   - ``regionID``: Identifier (e.g., district_550, state_29)
   - ``regionName``: Human-readable name
   - ``parentID``: Parent region identifier

Logging and Monitoring
----------------------

Console Output
~~~~~~~~~~~~~~

The script provides real-time progress updates including:

- Step-by-step progress through the pipeline
- File download progress bars (via ``tqdm``)
- Row counts and column information
- Summary statistics (total cases, date ranges, unique districts)

Log Files
~~~~~~~~~

Detailed logs are written to:

.. code-block:: text

    logs/fetch_ihip_data_YYYYMMDD-HHMMSS.log

Log entries include:

- Timestamps for all operations
- File processing details
- Data quality warnings
- Error messages with stack traces

Warnings
~~~~~~~~

The script logs warnings for:

- **Missing columns**: When expected columns are not found in source files
- **Unmatched districts**: Districts that couldn't be mapped to region IDs
- **Unparseable dates**: Date values that couldn't be converted to YYYY-MM-DD format
- **Empty results**: No positive test results found in the data

Dependencies
------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Package
     - Purpose
   * - ``boto3``
     - AWS S3 client for file downloads
   * - ``pandas``
     - Data manipulation, CSV/XLSX handling
   * - ``pyyaml``
     - Configuration file parsing
   * - ``python-dotenv``
     - Environment variable management
   * - ``tqdm``
     - Progress bar display
   * - ``openpyxl``
     - XLSX file reading (pandas dependency)

Error Handling
--------------

Common Errors
~~~~~~~~~~~~~

**AWS Authentication Failure**

.. code-block:: text

    ValueError: AWS credentials not found in environment variables

**Solution**: Ensure ``.env`` file contains valid ``AWS_ACCESS_KEY`` and ``AWS_SECRET_KEY``.

**Missing Configuration**

.. code-block:: text

    ValueError: ihip_s3_location not found in dengue_pipeline.yaml

**Solution**: Add ``ihip_s3_location`` parameter to configuration file.

**No Files in S3**

.. code-block:: text

    WARNING: No files found in s3://bucket/prefix/

**Solution**: Verify S3 path is correct and bucket contains IHIP data files.

**Region ID Mismatches**

.. code-block:: text

    WARNING: Could not match N districts to region IDs: ['DISTRICT1', 'DISTRICT2', ...]

**Solution**: Add missing district name corrections to the ``district_name_fixes`` dictionary in the script.

Performance Considerations
--------------------------

- **Download time**: Depends on number and size of XLSX files in S3 bucket
- **Memory usage**: Peak memory usage during XLSX to CSV conversion (~2-3x file size)
- **Processing time**: Typically 5-15 minutes for full Karnataka IHIP dataset
- **Disk space**: Requires ~3x the size of downloaded data (original + CSV + merged)

Best Practices
--------------

1. **Run in scheduled pipeline**: Automate execution via cron or CI/CD
2. **Monitor logs**: Check warnings for data quality issues
3. **Validate region IDs**: Ensure all districts are matched (no warnings)
4. **Archive outputs**: Keep timestamped versions of generated files
5. **Test with small datasets**: Use ``debug: true`` in config for initial testing

See Also
--------

- :doc:`data_specification` - Complete data schema documentation
- :doc:`usage` - Main pipeline usage guide
- :doc:`output_format` - Output format specifications
