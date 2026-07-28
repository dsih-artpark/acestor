Running the Pipeline
====================

Basic Usage
-----------

Every pipeline is invoked via ``python -m acestor.run`` with three flags: a builder function, a YAML config, and a run id.

.. code-block:: bash

    uv run python -m acestor.run \
      --pipeline pipelines.dengue.pipeline:build_pipeline \
      --config configs/my-config.yaml \
      --run-id my-run-001

.. list-table:: Pipeline builders
   :header-rows: 1
   :widths: 40 60

   * - ``--pipeline`` value
     - What it runs
   * - ``pipelines.dengue_prep.pipeline:build_pipeline``
     - Prep pipeline — ingest raw case + weather data, write ``prepared_data/*/{cases,weather}_daily.csv``. See :doc:`dengue_prep`.
   * - ``pipelines.dengue.pipeline:build_pipeline``
     - Main pipeline — thresholds, models, predictions, report.
   * - ``pipelines.dengue_downscale.pipeline:build_pipeline``
     - Downscale a parent-level ``predictions.csv`` to child regions. See :doc:`downscale`.

A typical end-to-end flow is: prep → main → (optional) downscale.

.. list-table:: Arguments
   :header-rows: 1
   :widths: 25 75

   * - Argument
     - Description
   * - ``--pipeline``
     - Python import path to the pipeline builder function (``module:function``)
   * - ``--config``
     - Path to the YAML configuration file
   * - ``--run-id``
     - Unique identifier for this run; all outputs are written under ``{artifacts_base}/{run-id}/``

Exit code ``0`` = success, non-zero = failure.

Starting from an Example Config
--------------------------------

Copy one of the bundled example configs and edit it:

.. code-block:: bash

    cp configs-example/gba_docker_test.yaml configs/my-config.yaml

``gba_docker_test.yaml`` is the recommended starting point — it uses local filesystem sources and minimal data, making it fast to run.

See :doc:`configuration` for a full reference of every config key.

Using Make
----------

The project ships a ``Makefile`` with convenience targets:

.. code-block:: bash

    # Standard run
    make run-dengue-pipeline DENGUE_RUN_ID=my-run

    # With a custom config
    DENGUE_CONFIG=configs/my-config.yaml make run-dengue-pipeline DENGUE_RUN_ID=my-run

    # Incremental run (skips steps whose outputs already exist — faster for testing)
    make run-dengue-pipeline-incremental DENGUE_RUN_ID=smoke-001

Outputs
-------

All outputs land under ``{storages.artifacts.filesystem.base_path}/{run_id}/``:

.. code-block:: text

    {run_id}/
    ├── sampling_day.json
    ├── datasets/
    ├── inputs/
    │   ├── cases_{region}_sampled.csv
    │   └── weather_{region}_sampled.csv
    ├── cutoffs.json
    ├── outputs/
    │   ├── predictions.csv          # ensemble (or primary model per ``report.primary``)
    │   ├── per_model/               # only present with ``model.output`` = per_model | both
    │   │   ├── predictions_rf.csv
    │   │   ├── predictions_xgb.csv
    │   │   ├── predictions_tse.csv
    │   │   └── predictions_timesfm.csv
    │   ├── charts/
    │   │   └── hero_forecast.png
    │   ├── maps/                    # skipped when ``maps.enabled: false``
    │   │   └── {region}_{model}_{threshold}_{date}.png
    │   └── report.html              # self-contained HTML brief with interactive D3 map
    └── run.log

See :doc:`output_format` for a full column-by-column reference.

Scheduling Pipelines
--------------------

Use ``scripts/run_schedules.py`` to run one or more pipelines on a recurring schedule.

**1. Configure your pipelines**

Edit the ``PIPELINES`` list at the top of ``scripts/run_schedules.py``:

.. code-block:: python

    PIPELINES = [
        {
            "name":     "gba-weekly",
            "cron":     "0 6 * * 1",   # every Monday at 06:00 UTC
            "pipeline": "pipelines.dengue.pipeline:build_pipeline",
            "config":   "configs/gba_stage1_s3.yaml",
        },
        # add more pipelines here
    ]

Cron expression format: ``minute  hour  day  month  day_of_week``

.. list-table:: Cron examples
   :header-rows: 1
   :widths: 30 70

   * - Expression
     - Meaning
   * - ``0 6 * * 1``
     - Every Monday at 06:00 UTC
   * - ``0 8 * * *``
     - Every day at 08:00 UTC
   * - ``*/30 * * * *``
     - Every 30 minutes

**2. Run the scheduler**

Foreground (for testing):

.. code-block:: bash

    uv run python scripts/run_schedules.py

Background on Mac/Linux:

.. code-block:: bash

    nohup uv run python scripts/run_schedules.py > .acestor/scheduler.out 2>&1 &
    echo $!   # save this PID to stop the scheduler later

Stop the scheduler:

.. code-block:: bash

    kill <PID>

**3. View logs**

Each run writes its own log file under ``logs/{pipeline_name}/``:

.. code-block:: bash

    # Watch a run live
    tail -f logs/gba-weekly/run-20260407_060000.log

    # List all runs for a pipeline
    ls -lht logs/gba-weekly/

.. note::
   If the scheduler was briefly down and missed a scheduled run, it will catch up automatically within a 1-hour grace window.
