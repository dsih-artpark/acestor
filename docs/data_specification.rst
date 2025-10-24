Input Data Specification
=========================

Case Data Format
----------------

The primary input to the Acestor pipeline is the district-level daily dengue case data, stored in ``datasets/cases_district_daily.csv``.

File Location
~~~~~~~~~~~~~

::

    datasets/cases_district_daily.csv

File Format
~~~~~~~~~~~

The file is a comma-separated values (CSV) file with the following structure:

Column Specifications
~~~~~~~~~~~~~~~~~~~~~

.. list-table:: Case Data Columns
   :header-rows: 1
   :widths: 20 15 65

   * - Column Name
     - Data Type
     - Description
   * - ``state.name``
     - String
     - LGD Standard Name of the Indian state (e.g., "CHHATTISGARH", "MAHARASHTRA")
   * - ``district.name``
     - String
     - LGD Standard Name of the district within the state (e.g., "BALOD", "DURG")
   * - ``date``
     - String
     - Date of the observation in DD/MM/YYYY format (e.g., "01/05/2025")
   * - ``samples_tested``
     - Integer
     - Number of samples tested for dengue on that date in the district
   * - ``case``
     - Integer
     - Number of confirmed dengue cases on that date in the district
   * - ``state.ID``
     - String
     - LGD Standard identifier for the state in the format state_lgd-code (e.g., "state_22")
   * - ``region_id``
     - String
     - LGD Standard identifier for the district/region in the format district_lgd-code(e.g., "district_646")

Data Characteristics
~~~~~~~~~~~~~~~~~~~~

- **Temporal Coverage**: Model performs better with more amount of data - Threshold calculation logic is of 2 types. One using historical data and other using past n weeks data. So data must either be recent upto current day, or there must be data of previous years for the model to work accurately.
- **Spatial Granularity**: District level
- **Geographic Scope**: Indian states and their districts
- **Primary Metrics**:

  - Number of samples tested per day
  - Number of confirmed dengue cases per day

Example Records
~~~~~~~~~~~~~~~

.. code-block:: text

    state.name,district.name,date,samples_tested,case,state.ID,region_id
    CHHATTISGARH,BALOD,01/05/2025,1,0,state_22,district_646
    CHHATTISGARH,BALODABAZAR-BHATAPARA,01/05/2025,3,0,state_22,district_644
    CHHATTISGARH,DURG,01/05/2025,22,0,state_22,district_378

Data Quality Notes
~~~~~~~~~~~~~~~~~~


- **Case Values**: Case counts are non-negative integers
- **Date Format**: All dates follow the DD/MM/YYYY format
- **Identifiers**: Both state.ID and region_id are based on LGD Standard.
