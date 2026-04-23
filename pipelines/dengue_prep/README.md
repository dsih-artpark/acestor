# dengue_prep pipeline

A scheduled data preparation pipeline. Its sole job is to ensure that
`prepared_data/{region_type}/` contains up-to-date daily aggregated case and
weather data for a given date range.

**The output is the contract. The input is pluggable.**

---

## Purpose

Run this pipeline daily (or on demand) to keep the prepared data current.
The downstream dengue pipeline reads from `prepared_data/` and does not care
how or from where the data was collected.

---

## Output contract

Both files are **append-deduped on `(region_id, date)`** — re-running the same
date range is safe and idempotent.

### `prepared_data/{region_type}/cases_daily.csv`

| column      | type   | description                  |
|-------------|--------|------------------------------|
| `region_id` | string | admin region identifier      |
| `date`      | date   | reporting date (YYYY-MM-DD)  |
| `case`      | int    | daily confirmed case count   |

### `prepared_data/{region_type}/weather_daily.csv`

| column                      | type  | description                        |
|-----------------------------|-------|------------------------------------|
| `region_id`                 | string| admin region identifier            |
| `date`                      | date  | date (YYYY-MM-DD)                  |
| `2mTemperature_mean`        | float | daily mean 2m temperature          |
| `2mTemperature_max`         | float | daily max 2m temperature           |
| `2mTemperature_min`         | float | daily min 2m temperature           |
| `2mDewpointTemperature_mean`| float | daily mean 2m dewpoint temperature |
| `totalPrecipitation_sum`    | float | daily total precipitation          |

Weather columns use ERA5 naming so the dengue pipeline can apply rolling
aggregation before renaming.

---

## Minimal config

```yaml
pipeline:
  name: dengue_prep

data:
  region_type: "district"

  date_range:
    start: "2021-09-01"
    # end: ""   # omit → defaults to today

  geojson:
    base_path: "path/to/geojsons"

  case_download:
    enabled: true
    # source path → DENGUE_PREP_CASE_SOURCE env variable (see below)

  weather_download:
    enabled: true
    source_mode: "openmeteo"   # openmeteo | cds | filesystem
```

---

## Environment variables

| variable                  | required | description                                      |
|---------------------------|----------|--------------------------------------------------|
| `DENGUE_PREP_CASE_SOURCE` | yes*     | path to directory containing raw case CSV files  |

*Not required if `data.case_download.source_path` is set in config, or if a
custom `download_case_data` step implementation is used.

---

## Extension points

The pipeline defines two extension points where you plug in your data source:

### Case data (`download_case_data` step)

The default implementation reads CSV files from a local directory or S3
bucket. To use a different source (API call, database, webhook, etc.),
replace `PrepDownloadCaseDataStep` in `pipeline.py` with your own step that
returns a `PrepCaseDownloadResult` with a list of file references.

### Weather data (`download_weather_data` step)

Three built-in modes controlled by `data.weather_download.source_mode`:

| mode          | description                                              |
|---------------|----------------------------------------------------------|
| `openmeteo`   | Free ERA5 reanalysis via Open-Meteo archive API          |
| `cds`         | ERA5-Land via Copernicus CDS (requires API key + licence)|
| `filesystem`  | Read pre-downloaded CSVs from a local directory or S3    |

To add a new weather source, add a `_download_from_<name>` method to
`PrepDownloadWeatherDataStep` and route to it in `run()`.

---

## DAG

```
download_case_data    ──> parse_case_data
download_weather_data ──> parse_weather_data
```

The two branches run in parallel.

---

## Pipeline stages

Steps execute in DAG order. Names match logs and code under `pipelines/dengue_prep/steps/`.

| Step name | Class | What it does |
|---|---|---|
| `download_case_data` | `PrepDownloadCaseDataStep` | Locates raw case files (filesystem or S3) and returns a list of file references |
| `parse_case_data` | `PrepParseCaseDataStep` | Reads IHIP-format files, maps rows to `region_id` via LGD code or spatial join, aggregates to daily case counts, upserts into `prepared_data/{region_type}/cases_daily.csv` |
| `download_weather_data` | `PrepDownloadWeatherDataStep` | Downloads weather from OpenMeteo, CDS, or pre-parsed filesystem/S3 source |
| `parse_weather_data` | `PrepParseWeatherDataStep` | Aggregates hourly weather CSVs to daily totals, applies date filter, upserts into `prepared_data/{region_type}/weather_daily.csv` |

---

## Running

```bash
# Set case source (if not in config)
export DENGUE_PREP_CASE_SOURCE="ap_datasets/raw_linelist_data/AP_IHIP"

python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ap_district_prep.yaml
```
