Installation
============

Prerequisites
-------------

Before installing, make sure you have the following on your system:

- **Python 3.10+** — `python.org <https://www.python.org/downloads/>`_
- **uv** — fast Python package manager (`install guide <https://docs.astral.sh/uv/getting-started/installation/>`_)
- **Geospatial system libraries** — required for ``geopandas`` / ``shapely``:

  - **macOS**: ``brew install gdal proj geos``
  - **Linux (Debian/Ubuntu)**: ``apt-get install gdal-bin libgdal-dev libgeos-dev libproj-dev``
  - **Windows**: install `OSGeo4W <https://trac.osgeo.org/osgeo4w/>`_ or use WSL

- **~1 GB free disk** *(optional)* — TimesFM checkpoint cache at ``.cache/timesfm/`` on first use

Install
-------

**1. Clone the repository**

.. code-block:: bash

    git clone https://github.com/dsih-artpark/acestor.git
    cd acestor

**2. Install dependencies**

.. code-block:: bash

    uv sync --all-extras

.. list-table:: Available extras
   :header-rows: 1
   :widths: 20 80

   * - Extra
     - What it adds
   * - ``dengue``
     - Geospatial + classical modeling stack: ``geopandas``, ``scikit-learn``, ``statsmodels``, ``xgboost``, ``matplotlib``
   * - ``timesfm``
     - Google's TimesFM 2.5 foundation model (``timesfm[torch]==2.0.0``). Heavy (pulls torch and downloads ~800 MB checkpoint on first use).
   * - ``cds``
     - Copernicus Climate Data Store (CDS) API client for ERA5 weather downloads
   * - ``s3``
     - AWS S3 storage backend (``boto3``)

Install only what you need. For a minimal local run using Open-Meteo weather and classical models only:

.. code-block:: bash

    uv sync --extra dengue

For runs that use TimesFM in the ensemble:

.. code-block:: bash

    uv sync --extra dengue --extra timesfm

Environment Variables
---------------------

Create a ``.env`` file in the project root with your secrets. These are loaded automatically at runtime.

.. code-block:: bash

    # Copernicus CDS — required if weather_download.source_mode = "cds"
    CDS_API_KEY=your-cds-api-key

    # Dashboard case source — required if case_download.source_mode = "dashboard"
    DASHBOARD_URL=https://apps.artpark.ai/disease-dashboard
    DASHBOARD_CLIENT_ID=your-email@example.com
    DASHBOARD_CLIENT_SECRET=your-password
    # Optional: pin the TimesFM checkpoint revision
    TIMESFM_REVISION=1d952420fba87f3c6dee4f240de0f1a0fbc790e3

    # SMTP — required if email.enabled = true
    SMTP_FROM=sender@example.com
    SMTP_HOST=smtp.example.com
    SMTP_USERNAME=sender@example.com
    SMTP_PASSWORD=your-password

    # AWS — required if using S3 storage or S3 data sources
    AWS_PROFILE=default
    AWS_REGION=ap-south-1

You can reference these in your YAML config using ``${VAR}`` or ``${VAR:-default}`` syntax — they are resolved at config load time. Never commit real secrets to the config files.

Docker
------

A pre-built Docker image is available on Docker Hub:

.. code-block:: bash

    docker pull dsihartpark/acestor:latest

Images are tagged by version (e.g., ``dsihartpark/acestor:1.0.0``) and built automatically on every GitHub release tag.

For production deployment on Docker or AWS EC2 — including systemd setup, IAM roles, S3 artifact storage, and log monitoring — see the `Deployment Guide <https://github.com/dsih-artpark/acestor/blob/production/docs/DEPLOYMENT.md>`_.

Verifying Installation
----------------------

Run a quick smoke test using the bundled example config:

.. code-block:: bash

    uv run python -m acestor.run \
      --pipeline pipelines.dengue.pipeline:build_pipeline \
      --config configs-example/gba_docker_test.yaml \
      --run-id smoke-test

Exit code ``0`` means success. Outputs will be written to ``artifacts/smoke-test/``.
