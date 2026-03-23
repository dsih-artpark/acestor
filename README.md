# acestor

**acestor** is the **production dengue intelligence pipeline**. It ingests case and weather data, estimates risk thresholds, runs forecasting models, produces maps, and emits report artifacts—config-driven, repeatable, and suitable for scheduled or on-demand operation.

---

## Why this exists

- **Reproducible runs** from a single YAML configuration and a fixed processing graph.  
- **Operational flexibility**: run entirely on **local disk** (no cloud storage required) or connect **S3** when you need it.  
- **Configurable data sources**: weather from **pre-parsed files**, **local NetCDF**, or **Copernicus CDS** (optional).  
- **Observable outputs**: artifacts per run (predictions, plots, report bundles) under a configurable storage root.

---

## Project layout

| Path | Purpose |
|------|---------|
| [`acestor/`](acestor/) | Runtime: configuration loading, orchestration, storage backends, CLI entrypoint. |
| [`pipelines/`](pipelines/) | Dengue pipeline stages, domain logic (`lib/`), typed step outputs, and step config. |
| [`configs/`](configs/) | Example YAML configurations for local and staged runs. |

Contributor-focused notes (lint, pre-commit) for the Python tree: [`acestor/README.md`](acestor/README.md).

---

## Requirements

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** (recommended) or another PEP 517 installer  
- **Geospatial & modeling stack**: install with the **`dengue`** optional extra.  
- **CDS weather download** (optional): **`cds`** extra + CDS API credentials.  
- **S3** (optional): **`s3`** extra.  
- **Report stage outputs** (by default): **`rep_dict` JSON**, a **summary `.tex`**, a **LaTeX bundle zip** (`main.tex`, `bibliography.bib`, `Images/`), and **`AllMaps_*.zip`**. Optional **PDF** when `report.compile_pdf: true` and **`pdflatex`** is on `PATH`.

---

## Installation

From the repository root:

```bash
uv sync --extra dengue
```

Optional extras:

```bash
uv sync --extra dengue --extra cds --extra s3
```

For contributors (lint, tests, pre-commit):

```bash
make install-dev
# or: uv sync --all-extras --dev
```

---

## Quick start: clone → configure → run

1. **Clone the repository**
   ```bash
   git clone https://github.com/dsih-artpark/acestor.git acestor-v2
   cd acestor-v2
   ```
2. **Install dependencies** (from the repo root; requires Python 3.10+ and [uv](https://docs.astral.sh/uv/))
   ```bash
   uv sync --extra dengue
   ```
   Add `--extra cds` and/or `--extra s3` if you use CDS downloads or S3 storages.
3. **Pick a config file** — start from an example under [`configs/`](configs/), e.g. `configs/gba_stage1.yaml` (full graph) or `configs/gba_stage1_multiregion_raw_smoke.yaml` (smoke / integration).
4. **Edit the YAML for your deployment** (same file you pass to the runner). Typical touch points:
   - **`pipeline:`** — `name`, `title` (used for default report titles).
   - **`run:`** — `run_date` (and any run metadata you rely on).
   - **`storages:`** — `artifacts.filesystem.base_path` (required); `raw_case` / `raw_weather` bases so downloads and reads resolve.
   - **`data:`** — enable/disable downloads, case/weather parse options, paths, **`case_sufficiency`** gate.
   - **`model:`**, **`thresholds:`**, **`cutoff:`** — modeling and threshold behaviour.
   - **`assess:`**, **`maps:`**, **`report:`** — assessment, map outputs, report/PDF options.
   - **Secrets / env** — e.g. `${CDS_API_KEY:-}` in YAML; do not commit real keys.
5. **Lay out inputs** — place case CSVs, weather files, and GeoJSON trees where your config points (`geojson_base`, `geojson_folder`, etc.).
6. **Run the pipeline** (default config in the `Makefile` is `configs/gba_stage1.yaml`; override as needed):
   ```bash
   make run-dengue-pipeline DENGUE_RUN_ID=my-first-run
   # or:
   DENGUE_CONFIG=configs/your_config.yaml make run-dengue-pipeline DENGUE_RUN_ID=my-first-run
   ```
   Incremental / integration graph:
   ```bash
   make run-dengue-pipeline-incremental DENGUE_RUN_ID=smoke-001
   ```
7. **Inspect outputs** — under **`{storages.artifacts.filesystem.base_path}/{run_id}/`** (predictions, `plots/`, `reports/`, `results/` zips, etc.).

---

## Pipeline stages (full graph)

Steps run according to the DAG dependencies below (names match logs and the code under `pipelines/gba_dengue/steps/`).

| # | Step | Role |
|---|------|------|
| 1 | `identify_sampling_day` | Resolve sampling day / case window metadata for the run. |
| 2 | `download_case_data` | Copy or fetch case inputs into the run layout (if enabled). |
| 3 | `download_weather_data` | Copy, CDS, or other weather ingest (if enabled). |
| 4 | `parse_nonstd_case_data` | Parse case data (e.g. linelist → daily series by region). |
| 5 | `validate_case_data_sufficiency` | Optional gate: stop early if case data are too thin. |
| 6 | `parse_weather_data` | Aggregate / sample weather features aligned to regions. |
| 7 | `identify_cutoff_dates` | Case/weather cutoffs and prediction-week calendar. |
| 8 | `generate_thresholds` | Build threshold tables from history and config. |
| 9 | `train_and_predict` | Fit models and write per-run predictions. |
| 10 | `combine_predictions` | Single combined predictions table for the run. |
| 11 | `assess_thresholds` | Threshold assessment and best-method tables (+ figure metadata for reports). |
| 12 | `generate_maps` | Choropleth map PNGs under the configured plots directory. |
| 13 | `generate_report` | `rep_dict` JSON, summary LaTeX, bundle zip, maps zip; optional PDF. |
| 14 | `notify_run` | Optional SMTP **success** notification when top-level `email:` is enabled and `success` is listed in `email.on`. |

**Dependency sketch:** sampling day + case download → parse case → sufficiency gate; sampling day + weather download → parse weather; sufficiency + weather parse → cutoffs → thresholds → train/predict; train + cutoffs → combine → assess; assess + combine → maps; assess + maps + cutoffs → report → **notify_run**.

**Failure email:** if the run stops before `notify_run`, **`python -m acestor.run`** still sends a **failed** notification when `email.enabled` and `failed` is in `email.on` (handled in the CLI after the runner returns). If you embed `PipelineRunner` elsewhere, call `acestor.infra.send_run_notification_email_if_configured` yourself for failures.

The **`run-dengue-pipeline-incremental`** target uses [`pipeline_incremental.py`](pipelines/gba_dengue/pipeline_incremental.py): the same step names, with a slightly different edge into `train_and_predict` (cutoffs + thresholds both feed it—useful for staged tests).

To **change the graph** (add/remove/reorder steps), edit the pipeline builder in [`pipelines/gba_dengue/pipeline.py`](pipelines/gba_dengue/pipeline.py) (or `pipeline_incremental.py`) and, if you add a new module, pass `--pipeline module.path:build_pipeline` to the CLI.

---

## Running acestor

Runs are started with the **`acestor.run`** CLI: you pass a **pipeline entrypoint** (which graph to execute) and a **config file**.

### Using Make (recommended)

Full production graph:

```bash
make run-dengue-pipeline DENGUE_RUN_ID=my-production-run
```

Staged / integration graph (subset of stages for testing):

```bash
make run-dengue-pipeline-incremental DENGUE_RUN_ID=smoke-001
```

The default config path is set in the `Makefile` (`DENGUE_CONFIG`); override with `DENGUE_CONFIG=path/to/config.yaml`.

### Using the CLI directly

```bash
uv run python -m acestor.run \
  --pipeline <module.path:build_function> \
  --config path/to/config.yaml \
  --run-id my-run
```

Pipeline entrypoints and example configs live under `pipelines/` and `configs/` (see the `Makefile` for the exact module paths used in production).

Exit code **0** means the run finished with `status=success`; non-zero indicates failure.

---

## Configuration overview

- **Pipeline name & run metadata** under `pipeline:` and `run:` (`pipeline.title` feeds default report titles when `report.document_title` is omitted).  
- **Data paths** under `data:` (case download/parse, weather download/parse, **case sufficiency** gate).  
- **Model & thresholds** under `model:`, `thresholds:`, `cutoff:`.  
- **Post-processing** under `assess:`, `maps:`, `report:`.  
- **Storages** under `storages:` — at minimum configure `artifacts` and any `raw_*` backends you use.

### Environment variables in YAML

Values may use `${VAR}` and `${VAR:-default}` (resolved when the config is loaded). Use this for **CDS keys**, SMTP passwords, etc.—never commit secrets.

### Filesystem-first operation

Point `storages.*.filesystem.base_path` at directories you control. Each run writes under **`{artifacts_base}/{run_id}/...`**. No S3 configuration is required for a full local run.

### GeoJSON inputs

Case parsing (non-standardized lat/lon path) expects region geometries under a configurable base directory with subfolders such as `zones/` and `corps/`. Pre-aggregated case CSVs can skip spatial joins when configured appropriately.

---

## Operations

| Topic | Where to configure |
|--------|-------------------|
| **Early exit if case data are too thin** | `data.case_sufficiency` (rows, regions, date span). |
| **Run notifications** | Top-level `email:` — see commented examples in YAML under `configs/`. **Success:** `notify_run` step (end of DAG). **Failure:** CLI (`acestor.run`) after the run; body includes the exception summary when enabled. |
| **Report artifacts** | Always: JSON + summary `.tex` + LaTeX bundle zip (`results/{bundle_prefix}_*`, default prefix `Report`: `main.tex`, bib, images) + maps zip. **PDF** only if `report.compile_pdf: true` and `pdflatex` is on `PATH`. Titles/captions: set `pipeline.title` and optional `report.document_title`, `report.caption_*_scope`, `report.bundle_prefix`, `maps.figure_title`. |
| **Matplotlib in threaded runs** | Map generation uses a non-interactive backend so rendering is safe when stages run in parallel. |

---

## Security & compliance

- **Do not commit** API keys, CDS credentials, or SMTP passwords.  
- Treat **prediction outputs and linelist-derived data** according to your institutional data policy.  
- Historical LaTeX templates may include confidential boilerplate; adapt branding and legal footers for your deployment.

---

## Development

- **Lint / format / tests**: `make lint`, `make format`, `make test`.

---

## Acknowledgements

acestor is built for **config-driven, testable, production-ready** dengue risk operations.

---

## Contact

For **ARTPARK** deployments and collaboration, see [artpark.in](https://www.artpark.in/).  
GitHub repository: [dsih-artpark/acestor](https://github.com/dsih-artpark/acestor).  
Issue tracker: [github.com/dsih-artpark/acestor/issues](https://github.com/dsih-artpark/acestor/issues).
