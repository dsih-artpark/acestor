# GBA Dengue Pipeline — Architecture

## Diagram

![Architecture Diagram](architecture.svg)

```
╔══════════════════════════════════════════════════════════════════════════════════╗
║                          ACESTOR-V2 PIPELINE ARCHITECTURE                       ║
╚══════════════════════════════════════════════════════════════════════════════════╝

┌─────────────────────────────────────────────────────────────────────────────────┐
│                               DATA SOURCES                                      │
│                                                                                 │
│  ┌───────────────────┐   ┌──────────────────────┐   ┌───────────────────────┐  │
│  │  Case Data (CSV)  │   │  Weather (CDS / CDS   │   │   Geospatial Data     │  │
│  │                   │   │  NetCDF Cache)        │   │  (Shapefiles, GeoJSON)│  │
│  │ • Pre-aggregated  │   │                       │   │                       │  │
│  │ • Raw linelist    │   │ ERA5-Land Reanalysis:  │   │ • Region boundaries   │  │
│  │ • Standardized    │   │ • 2m Temperature      │   │ • Corp / Zone / Ward  │  │
│  │   linelist        │   │ • Dewpoint Temp        │   │   / Subdistrict       │  │
│  │                   │   │ • Total Precipitation  │   │                       │  │
│  └────────┬──────────┘   └──────────┬────────────┘   └──────────┬────────────┘  │
│           │ Filesystem / S3          │ CDS API / local NetCDF    │ local FS      │
└───────────┼──────────────────────────┼───────────────────────────┼───────────────┘
            │                          │                           │
            ▼                          ▼                           │
┌───────────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATION LAYER (acestor core)                    │
│                                                                               │
│   CLI entrypoint: python -m acestor.run --pipeline ... --config ... --run-id │
│   DAG Engine: Kahn's topological sort + ThreadPoolExecutor (parallel steps)   │
│   Config: YAML → PipelineConfig dataclasses (env-var substitution)            │
│   State: PipelineContext (shared: config, run_id, artifacts, storages, logger)│
└───────────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                             PIPELINE DAG (14 steps)                           │
│                                                                               │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  STAGE 1: INGEST                                                         │ │
│  │                                                                          │ │
│  │   identify_sampling_day  ──────────────────────────────────┐             │ │
│  │          (M / W / F based on run_date)                     │             │ │
│  │                                                            │             │ │
│  │   download_case_data ──────────────────────────────────────┤             │ │
│  │     (validate CSV presence on FS/S3)                       │             │ │
│  │                                                            │             │ │
│  │   download_weather_data ──────────────────────────────────-┤             │ │
│  │     (CDS API → NetCDF → per-region CSVs, or cached)        │             │ │
│  └────────────────────────────────────────────────────────────┼─────────────┘ │
│                                                               │               │
│  ┌────────────────────────────────────────────────────────────┼─────────────┐ │
│  │  STAGE 2: PARSE & VALIDATE                                 │             │ │
│  │                                                            ▼             │ │
│  │   parse_case_data ◄──────────────── (sampling_day + case download)       │ │
│  │     • detect format (pre-agg / raw / standardized)                       │ │
│  │     • spatial join (lat/lon → region_id) if raw linelist                 │ │
│  │     • daily aggregation + 7-day rolling window                           │ │
│  │     • sample by day-of-week                                              │ │
│  │            │                                                             │ │
│  │            ▼                                                             │ │
│  │   validate_case_data_sufficiency  (gate check)                           │ │
│  │     • min rows, min regions, min date span                               │ │
│  │            │                                                             │ │
│  │   parse_weather_data ◄──── (sampling_day + weather download)             │ │
│  │     • merge per-region CSVs                                              │ │
│  │     • daily + rolling aggregation                                        │ │
│  │     • sample by day-of-week                                              │ │
│  └────────────────────────────────────────────────────────────┬─────────────┘ │
│                                                               │               │
│  ┌────────────────────────────────────────────────────────────┼─────────────┐ │
│  │  STAGE 3: CUTOFFS & THRESHOLDS                             │             │ │
│  │                                                            ▼             │ │
│  │   identify_cutoff_dates ◄─────── (case + weather sampled data)           │ │
│  │     • cutoff = min(last case date, last weather date)                    │ │
│  │     • pred_upto = cutoff + 14 days                                       │ │
│  │     • prediction_dates = date_range(cutoff, pred_upto, freq=sampling)    │ │
│  │            │                                                             │ │
│  │            ▼                                                             │ │
│  │   generate_thresholds                                                    │ │
│  │     • 4-week recent threshold                                            │ │
│  │     • 5-year historical threshold (excluding pandemic years)             │ │
│  └────────────────────────────────────────────────────────────┬─────────────┘ │
│                                                               │               │
│  ┌────────────────────────────────────────────────────────────┼─────────────┐ │
│  │  STAGE 4: MODELING                                         │             │ │
│  │                                                            ▼             │ │
│  │   train_and_predict                                                      │ │
│  │     ┌──────────────────────────┐  ┌──────────────────────────┐          │ │
│  │     │  NBR (Negative Binomial  │  │  TSE (Time Series        │          │ │
│  │     │  Regression)             │  │  Extrapolation)          │          │ │
│  │     │  Features: case + weather│  │  Linear extrapolation    │          │ │
│  │     │  lags (temp: 12wk,       │  │  to cutoff_case + 14d    │          │ │
│  │     │  rainfall: 4wk)          │  │                          │          │ │
│  │     └───────────┬──────────────┘  └───────────┬──────────────┘          │ │
│  │                 └──────────┬───────────────────┘                         │ │
│  │                            ▼                                             │ │
│  │                     Ensemble predictions                                 │ │
│  │                     Zone classification (low/med/high/very-high)         │ │
│  │                            │                                             │ │
│  │                            ▼                                             │ │
│  │   combine_predictions  (aggregate across models/regions/dates)           │ │
│  │            │                                                             │ │
│  │            ▼                                                             │ │
│  │   assess_thresholds   (best method per region vs. thresholds)            │ │
│  └────────────────────────────────────────────────────────────┬─────────────┘ │
│                                                               │               │
│  ┌────────────────────────────────────────────────────────────┼─────────────┐ │
│  │  STAGE 5: OUTPUTS                                          │             │ │
│  │                                                            ▼             │ │
│  │   generate_maps                                                          │ │
│  │     • Choropleth PNGs per region/date/model/threshold                    │ │
│  │     • geopandas + matplotlib                                             │ │
│  │            │                                                             │ │
│  │            ▼                                                             │ │
│  │   generate_report                                                        │ │
│  │     • rep_dict JSON (metadata, captions, filenames)                      │ │
│  │     • LaTeX source → optional PDF via pdflatex                           │ │
│  │     • ZIP bundle (maps + LaTeX)                                          │ │
│  │            │                                                             │ │
│  │            ▼                                                             │ │
│  │   send_report  (SMTP email: PDF + maps ZIP attachment)                   │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## Intermediates (per run, under `artifacts/{run_id}/`)

| Stage | Artifact | Format |
|---|---|---|
| Ingest | `sampling_day.json` | JSON |
| Parse | `datasets/cases_{region}_sampled.csv` | CSV |
| Parse | `datasets/weather_{region}_sampled.csv` | CSV |
| Cutoff | `cutoffs.json` | JSON |
| Threshold | `datasets/thresholds/{region}_all_thresholds.csv` | CSV |
| Modeling | `results/Predictions_{month}_{region}_{date}.csv` | CSV |
| Modeling | `dumps/best_method_{region}_{date}.csv` | CSV |
| Maps | `plots/{region}_{model}_{threshold}_{date}.png` | PNG |
| Maps | `results/AllMaps_{month}_{date}.zip` | ZIP |

---

## Final Outputs

| Output | Format | Destination |
|---|---|---|
| Risk maps | PNG choropleth | Artifacts dir + ZIP |
| Report dictionary | JSON | Artifacts dir |
| LaTeX report | `.tex` + PDF | Artifacts dir |
| LaTeX bundle | ZIP | Artifacts dir |
| Email notification | HTML + attachments | SMTP recipients |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| DAG/Orchestration | Custom (Kahn's algo + `ThreadPoolExecutor`) |
| Spatial | `geopandas`, `shapely` |
| Data | `pandas` |
| ML / Stats | `statsmodels` (NBR), `scikit-learn` |
| Weather API | `cdsapi` (Copernicus ERA5-Land) |
| Storage | Filesystem or `boto3` (S3) |
| Visualization | `matplotlib` + `geopandas` |
| Reporting | LaTeX + `pdflatex`, Python `smtplib` |
| Config | YAML + dataclasses, env-var substitution |
| Containerization | Docker |
