# Config Reference

Complete reference for every key in the pipeline YAML config.
All keys are optional unless marked **required**.

---

## `pipeline`

```yaml
pipeline:
  name: dengue          # used in logger names and artifact paths
  title: "Dengue Intelligence — GBA"  # used in report document title
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | `"pipeline"` | Short identifier, no spaces |
| `title` | string | `""` | Appears in PDF report header |

---

## `run`

```yaml
run:
  run_date: "2026-03-18"   # leave blank to use today
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `run_date` | YYYY-MM-DD | today | Reference date for cutoff and prediction window |

---

## `data.prepared_data` *(dengue pipeline)*

Location of prepared case and weather data produced by the dengue_prep pipeline.

```yaml
data:
  prepared_data:
    base_dir: "prepared_data"
    region_type: "mandal"
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `base_dir` | string | `"prepared_data"` | Root directory; files resolved as `{base_dir}/{region_type}/cases_daily.csv` etc. |
| `region_type` | string | `"district"` | Region granularity to load |

---

## `data.region_type` *(dengue_prep pipeline)*

Top-level region type for data preparation. Propagates into `case_parse`, `weather_download`, and `weather_parse` if those sections don't override it.

```yaml
data:
  region_type: "mandal"
```

---

## `data.date_range` *(dengue_prep pipeline)*

Date range for data preparation. Propagates into `case_parse` (as `date_start`/`date_end`) and `weather_download` (as `start_date`/`end_date`) if those sections don't set their own values.

```yaml
data:
  date_range:
    start: "2022-01-01"
    end: "2026-03-28"   # empty → today (OpenMeteo capped at ERA5 5-day lag)
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `start` | YYYY-MM-DD | — | Earliest date to include |
| `end` | YYYY-MM-DD | today | Latest date; empty → today |

---

## `data.case_download`

Controls where raw case CSV files are read from.

```yaml
data:
  case_download:
    enabled: true
    source_backend: "filesystem"   # or "s3"
    source_path: "datasets/raw_linelist_data/AP_IHIP"
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Set `true` to activate |
| `source_backend` | `"filesystem"` \| `"s3"` | `"filesystem"` | |
| `source_path` | string | `""` | Filesystem dir or `s3://bucket/prefix` |
| `source_paths` | list[string] | `[]` | Explicit file list; if empty, all files under `source_path` are used |
| `cache_enabled` | bool | `true` | Cache S3 downloads locally |
| `cache_dir` | string | `"./cache/raw_case"` | Local cache directory |
| `cache_strategy` | string | `"local_first"` | `"local_first"` \| `"local"` \| `"cloud_first"` |

---

## `data.case_parse`

**Required section.**

```yaml
data:
  case_parse:
    region_types:
      - "district"
    date_start: "2021-09-01"
    date_end: ""
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `region_types` | **required** list[string] | — | e.g. `["corp", "zone"]` or `["district"]`. Last entry drives downstream steps |
| `date_start` | **required** YYYY-MM-DD | — | Earliest case date to include |
| `date_end` | YYYY-MM-DD | run_date | Latest case date; empty → run_date |

**Accepted region types:** `corp`, `zone`, `ward`, `district`, `subdistrict`, `mandal`

---

## `data.case_sufficiency`

Early gate that aborts the run if case data is too sparse.

```yaml
data:
  case_sufficiency:
    enabled: true
    min_total_rows: 30
    min_distinct_regions: 2
    min_date_span_days: 14
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `true` | |
| `min_total_rows` | int | `30` | Minimum case rows across all regions |
| `min_distinct_regions` | int | `2` | Minimum distinct region IDs |
| `min_date_span_days` | int | `14` | Minimum date range in days |

---

## `data.geojson`

```yaml
data:
  geojson:
    base_path: "datasets/geojsons/geojsons_GBA"
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `base_path` | string | `"geojsons"` | Root folder; files resolved as `{base_path}/{region_type}s/{region_id}.geojson` |

---

## `data.weather_download`

```yaml
data:
  weather_download:
    enabled: true
    source_mode: "openmeteo"   # openmeteo | cds | filesystem
    parsed_output_path: "datasets/openmeteo_ap"
    convert_units: true        # convert OpenMeteo °C→K and mm→m to match ERA5 units
    region_type: "district"
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Must be `true` for weather data to flow into the pipeline |
| `source_mode` | `"openmeteo"` \| `"cds"` \| `"filesystem"` | `"filesystem"` | `"openmeteo"` — free ERA5 via Open-Meteo archive API; `"cds"` — ERA5-Land via Copernicus (requires API key); `"filesystem"` — read pre-downloaded CSVs |
| `source_backend` | `"filesystem"` \| `"s3"` | `"filesystem"` | Backend for `"filesystem"` mode |
| `source_path` | string | `""` | Directory of pre-parsed CSVs, or `s3://bucket/prefix` |
| `parsed_output_path` | string | `""` | Root directory where monthly CSVs are written. For openmeteo/CDS modes, files land at `{parsed_output_path}/{region_type}/{year}/{year}_{mm}.csv` |
| `convert_units` | bool | `false` | OpenMeteo mode only: convert °C→K for temperature/dew-point and mm→m for precipitation to match ERA5 units. Set `true` when mixing with CDS data |
| `chunk_months` | int | `12` | OpenMeteo mode only: months of data per API call. Reduce if hitting rate limits |
| `region_type` | string | `"zone"` | Region type to download weather for; must match `data.region_type` |
| `netcdf_cache_path` | string | `""` | CDS mode: path to local `.zip`/`.nc` cache |
| `w_params` | list[string] | `["t2m","d2m","tp"]` | CDS/NetCDF mode: variable short names to extract |
| `threshold_km` | float | `25.0` | CDS mode: max distance from region boundary for ERA5 grid-point filtering |
| `bounds_resolution_deg` | float | `0.1` | CDS mode: grid snap resolution for auto-computed region bounds |
| `start_date` | YYYY-MM-DD | `"2015-01-01"` | Download start date |
| `end_date` | YYYY-MM-DD | run_date | Download end date; empty → today (OpenMeteo is capped at ERA5 lag of 5 days) |

---

## `data.weather_parse`

```yaml
data:
  weather_parse:
    region_type: "district"
    weather_variables:
      - "2mTemperature"
      - "totalPrecipitation"
      - "2mDewpointTemperature"
    daily_agg:
      - {name: "2mTemperature", op: "max", output_name: "2mTemperature_max"}
      - {name: "2mTemperature", op: "min", output_name: "2mTemperature_min"}
      - {name: "2mTemperature", op: "mean"}
      - {name: "2mDewpointTemperature", op: "mean"}
      - {name: "totalPrecipitation", op: "sum"}
    rolling_n_days: 7
    rolling_agg:
      - {name: "2mTemperature", op: "mean"}
      - {name: "2mDewpointTemperature", op: "mean"}
      - {name: "totalPrecipitation", op: "sum"}
    sampling_rate: 7
    intermediate_col_rename:
      "2mTemperature_max": "t2m_max"
      "2mTemperature_min": "t2m_min"
      "2mTemperature": "t2m_mean"
      "2mDewpointTemperature": "d2m_mean"
      "totalPrecipitation": "tp_sum"
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `region_type` | string | `"zone"` | Must match `case_parse.region_types` |
| `weather_variables` | list[string] | `["2mTemperature","totalPrecipitation","2mDewpointTemperature"]` | Variables to read from parsed CSVs |
| `daily_agg` | list[{name, op, output_name?}] | mean/sum defaults | Aggregations applied per day. `op`: `"mean"`, `"max"`, `"min"`, `"sum"` |
| `rolling_n_days` | int | `7` | Rolling window size in days |
| `rolling_agg` | list[{name, op}] | mean/sum defaults | Aggregations applied over rolling window |
| `sampling_rate` | int | `7` | Sample every N days (7 = weekly) |
| `intermediate_col_rename` | dict | see above | Renames columns in intermediate files |
| `write_agg_daily` | bool | `true` | Write `agg_daily` intermediate CSVs to artifacts. Set `false` to skip (e.g. hindcast batch runs) |
| `write_agg_ndays` | bool | `true` | Write `agg_Ndays` rolling-aggregate intermediate CSVs to artifacts. Set `false` to skip |

---

## `cutoff`

```yaml
cutoff:
  case_min_regions: 2
  weather_min_regions: 5
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `case_min_regions` | int | `2` | Minimum regions with recent case data to determine cutoff |
| `weather_min_regions` | int | `5` | Minimum regions with recent weather data |

---

## `thresholds`

```yaml
thresholds:
  region_type: "district"
  n_weeks: 4
  historical_n_years: 4
  excluded_years: []
  included_years: []
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `region_type` | string | `"zone"` | Must match `case_parse.region_types` |
| `n_weeks` | int | `4` | Number of future weeks to predict |
| `historical_n_years` | int \| null | `null` (all years) | Limit historical data to last N years |
| `excluded_years` | list[int] | `[2020, 2021]` | Years to skip (e.g. COVID anomaly) |
| `included_years` | list[int] | `[]` | If non-empty, only use these years |

---

## `model`

```yaml
model:
  spatial_res: "district"
  data_features:
    - "case"
    - "recordDate"
    - "recordYear"
    - "recordMonth"
    - "ISOWeek"
    - "t2m_mean"
    - "tp_sum"
    - "d2m_mean"
  lag:
    lag_temp: [12]
    lag_rf: [4]
  years_to_exclude: []
  years_to_include: []
  list_alpha: [1.0, 2.0]
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `spatial_res` | string | `"zone"` | Must match `case_parse.region_types` |
| `data_features` | list[string] | temperature/rain/case defaults | Feature columns fed to the model |
| `lag.lag_temp` | list[int] | `[12]` | Temperature lag in weeks |
| `lag.lag_rf` | list[int] | `[4]` | Rainfall lag in weeks |
| `years_to_exclude` | list[int] | `[2020, 2021]` | Exclude from model training |
| `years_to_include` | list[int] | `[]` | If non-empty, train only on these years |
| `list_alpha` | list[float] | `[1.0, 2.0]` | Ridge regression alpha values to try |

---

## `assess`

```yaml
assess:
  total_district_regions: 26
```

Set `total_{region_type}_regions` for each region type present in your data.

| Key | Type | Default | Notes |
|---|---|---|---|
| `total_corp_regions` | int | — | Total number of corp regions in the geography |
| `total_zone_regions` | int | — | Total number of zone regions |
| `total_ward_regions` | int | — | Total number of ward regions |
| `total_district_regions` | int | — | Total number of districts |
| `total_subdistrict_regions` | int | — | Total number of subdistricts |

Used to compute coverage ratios for threshold method assessment. If omitted for a region type, defaults to `10`.

---

## `maps`

```yaml
maps:
  output_dir: "plots"
  figure_title: "Andhra Pradesh Dengue Risk Map"
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `output_dir` | string | `"plots"` | Relative path under artifacts for map PNGs |
| `figure_title` | string | `"Dengue risk map"` | Map title (first line); prediction date is added as second line |

---

## `report`

```yaml
report:
  output_dir: "reports"
  compile_pdf: true
  caption_primary: "AP districts"
  caption_secondary: ""
  bundle_prefix: "Report"
  document_title: ""
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `output_dir` | string | `"reports"` | Relative path under artifacts for report files |
| `compile_pdf` | bool | `false` | Compile LaTeX bundle to PDF (requires `pdflatex` in PATH) |
| `caption_primary` | string | `"corporations"` | Region label for primary region type in captions |
| `caption_secondary` | string | `"zones"` | Region label for secondary region type |
| `bundle_prefix` | string | `"Report"` | Prefix for the output zip filename |
| `document_title` | string | derived from `pipeline.title` | PDF document title; auto-set if blank |

---

## `report_distribution`

Metadata embedded in the report and used in email notifications.

```yaml
report_distribution:
  system_name: "Dengue Early Warning System"
  organization: "ARTPARK, IISc Bengaluru"
  state: "Andhra Pradesh"
  region: "Andhra Pradesh (26 Districts)"
  department: "Directorate of Public Health, GoAP"
  contact_email: ""
  footer_note: ""
```

---

## `email`

```yaml
email:
  enabled: false
  on: ["success", "failed"]
  to: ["recipient@example.com"]
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | |
| `on` | list[string] | `["success"]` | When to send: `"success"`, `"failed"`, or both |
| `to` | list[string] | `[]` | Recipient addresses |

SMTP credentials are set via environment variables — see `.env.example`.

---

## `logging`

```yaml
logging:
  level: INFO
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `level` | string | — | `DEBUG`, `INFO`, `WARNING`, `ERROR`. **If this section is omitted, all pipeline logs are silently discarded.** |

---

## `storages`

```yaml
storages:
  artifacts:
    kind: filesystem
    filesystem:
      base_path: "./artifacts"
```

**Filesystem:**

| Key | Type | Notes |
|---|---|---|
| `kind` | `"filesystem"` | |
| `filesystem.base_path` | string | Root directory for all run artifacts |

**S3:**

```yaml
storages:
  artifacts:
    kind: s3
    s3:
      bucket: your-bucket
      base_prefix: artifacts/
      region: ap-south-1
```

| Key | Type | Notes |
|---|---|---|
| `kind` | `"s3"` | |
| `s3.bucket` | string | S3 bucket name |
| `s3.base_prefix` | string | Key prefix (folder) within the bucket |
| `s3.region` | string | AWS region |

---

## `run.source_run_id` *(dengue_downscale pipeline)*

```yaml
run:
  source_run_id: "march-10-run"
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `source_run_id` | string | **required** | Run ID of the `dengue` forecast run whose predictions to disaggregate. Must exist under `storages.artifacts.filesystem.base_path`. |

---

## `downscale` *(dengue_downscale pipeline)*

```yaml
downscale:
  parent_level: district
  child_level: mandal
  window_weeks: 4
  cases_csv: "prepared_data/mandal/cases_daily.csv"
  geojson_base_path: "ap_datasets/geojsons/geojsons_AP"
```

| Key | Type | Default | Notes |
|---|---|---|---|
| `parent_level` | string | **required** | Singular geography of the source predictions (e.g. `district`, `state`) |
| `child_level` | string | **required** | Singular geography to disaggregate into (e.g. `mandal`, `block`). Must differ from `parent_level`. |
| `window_weeks` | int | `4` | How many recent weeks of child-level case history to use when computing each child's share. Must be > 0. |
| `cases_csv` | string | `"prepared_data/mandal/cases_daily.csv"` | Path to child-level daily case counts produced by `dengue_prep` at the child spatial level. |
| `geojson_base_path` | string | `"ap_datasets/geojsons/geojsons_AP"` | Base directory containing `{child_level}s/` subdirectory with per-region GeoJSON files. Each file must have `region_id` and `parent` properties. |

