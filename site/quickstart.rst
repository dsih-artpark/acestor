Quickstart
==========

Get from zero to your first pipeline run in 5 minutes.

.. note::
   This guide uses a local filesystem config with minimal data. No S3 or CDS credentials needed.

1. Install
----------

.. code-block:: bash

    git clone https://github.com/dsih-artpark/acestor.git
    cd acestor
    uv sync --extra dengue

.. seealso::
   Need help with prerequisites (Python, geospatial libs, env vars)? See :doc:`installation`.

2. Copy the example config
--------------------------

.. code-block:: bash

    cp configs-example/gba_docker_test.yaml configs/my-first-run.yaml

Open ``configs/my-first-run.yaml`` and update the paths to match your local setup:

.. code-block:: yaml

    storages:
      artifacts:
        kind: filesystem
        filesystem:
          base_path: "./artifacts"   # where outputs will be written

    data:
      geojson:
        base_path: "datasets/geojsons"   # path to your GeoJSON files

      case_download:
        source_path: "datasets/raw_linelist_data/..."   # path to case data

.. tip::
   Set ``debug: true`` in your config for the first run — it limits processing to the last 3 years of data, making the run much faster.

3. Run
------

.. code-block:: bash

    uv run python -m acestor.run \
      --pipeline pipelines.dengue.pipeline:build_pipeline \
      --config configs/my-first-run.yaml \
      --run-id my-first-run

Watch the logs as each step completes. Exit code ``0`` = success.

4. Check outputs
----------------

.. code-block:: bash

    ls artifacts/my-first-run/

You should see:

.. code-block:: text

    artifacts/my-first-run/
    ├── predictions/      ← dengue case predictions (CSV)
    ├── plots/            ← choropleth map PNGs
    ├── reports/          ← JSON + LaTeX report bundle
    └── logs/

Next Steps
----------

- :doc:`installation` — full prerequisites and environment setup
- :doc:`configuration` — complete guide to every config option
- :doc:`usage` — scheduling, Make targets, Docker
