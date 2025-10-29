Output Format
=============

The pipeline generates prediction files in the ``results/`` directory.

Output Files
------------

Two types of prediction files are generated:

.. list-table:: Output Files
   :header-rows: 1
   :widths: 40 60

   * - File Pattern
     - Description
   * - ``Predictions_<MonthRange>_District_<Date>.csv``
     - District-level predictions with threshold analysis
   * - ``Predictions_<MonthRange>_<State>_<Date>.csv``
     - State-level aggregated predictions

Example filenames:

- ``Predictions_Oct - Nov 2025_District_20251027.csv``
- ``Predictions_Oct - Nov 2025_Karnataka_20251027.csv``

District-Level Predictions
--------------------------

Column Specifications
~~~~~~~~~~~~~~~~~~~~~

.. list-table:: District Prediction Columns
   :header-rows: 1
   :widths: 20 15 65

   * - Column Name
     - Data Type
     - Description
   * - ``district``
     - String
     - District identifier (e.g., "district_374")
   * - ``recordDate``
     - Date
     - Date of the prediction record (YYYY-MM-DD)
   * - ``ISOWeek``
     - Integer
     - ISO week number of the year
   * - ``thresholdMethod``
     - String
     - Threshold calculation method: "historical" or "previousNweeks"
   * - ``Mean``
     - Float
     - Mean of historical cases for threshold calculation
   * - ``StdDev``
     - Float
     - Standard deviation of historical cases
   * - ``Zero``
     - Float
     - Lower bound (typically 0.0)
   * - ``Inf``
     - String
     - Upper bound indicator
   * - ``T0.00``
     - Float
     - Threshold tier 0 (baseline)
   * - ``T1.00``
     - Float
     - Threshold tier 1 (elevated)
   * - ``T2.00``
     - Float
     - Threshold tier 2 (high)
   * - ``startDatePredictedWeek``
     - Date
     - Start date of the week being predicted
   * - ``dateOfComputingPrediction``
     - Date
     - Date when the prediction was computed
   * - ``regionID``
     - String
     - Region identifier matching the district
   * - ``prediction``
     - Float
     - Predicted number of dengue cases
   * - ``model``
     - String
     - Model used (typically "ensembleModel")
   * - ``predictionZone``
     - Float
     - Zone classification based on threshold tiers

State-Level Predictions
-----------------------

Column Specifications
~~~~~~~~~~~~~~~~~~~~~

.. list-table:: State Prediction Columns
   :header-rows: 1
   :widths: 25 15 60

   * - Column Name
     - Data Type
     - Description
   * - ``dateOfComputingPrediction``
     - Date
     - Date when prediction was computed
   * - ``startDatePredictedWeek``
     - Date
     - Start date of predicted week
   * - ``regionID``
     - String
     - State identifier (e.g., "state_29")
   * - ``prediction``
     - Float
     - Predicted number of dengue cases for the state
   * - ``thresholdMethod``
     - String
     - Threshold calculation method used
   * - ``predictionZone``
     - Integer
     - Alert zone classification (0=low, higher=elevated risk)
   * - ``model``
     - String
     - Model used for prediction

Data Characteristics
--------------------

- **Temporal Resolution**: Weekly predictions
- **Prediction Horizon**: Multiple weeks ahead (typically 2-4 weeks)
- **Threshold Methods**: Two methods for alert classification:

  - ``historical``: Based on historical mean and standard deviation
  - ``previousNweeks``: Based on recent N weeks of data

- **Prediction Zones**: Numerical classification indicating risk levels
- **Model Type**: Ensemble model combining multiple forecasting approaches

Example Record
--------------

District-level:

.. code-block:: text

    district_374,2025-10-27,42,historical,4.5,0.707,0.0,inf,4.5,5.207,5.914,2025-10-27,2025-10-27,district_374,0.819,ensembleModel,1.0

State-level:

.. code-block:: text

    2025-10-27,2025-10-27,state_29,21.49,historical,0,ensembleModel
