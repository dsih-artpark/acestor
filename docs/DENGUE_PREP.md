# dengue_prep Pipeline

`dengue_prep` is the **data preparation pipeline**. It runs independently of the
forecast pipeline; its only job is to keep `prepared_data/` current. The
downstream `dengue` pipeline reads from that directory and does not care how the
data got there.

See [SOURCES.md](SOURCES.md) for the case & weather source plugin architecture,
and [RUNNING_PIPELINES.md](RUNNING_PIPELINES.md) for how prep + forecast are
run together in production.

---

## What it does

Two independent branches, executed in parallel:

1. **Case branch** — pulls raw IHIP-shaped case files from a configurable
   source (local directory, live dashboard API, or a custom plugin), parses
   each row into `(region_id, date)`, aggregates to a daily count, and upserts
   into `prepared_data/{region_type}/cases_daily.csv`.
2. **Weather branch** — fetches daily weather records for every region in
   the target geojson layer (via Open-Meteo, ERA5-Land CDS, filesystem, or a
   custom plugin), normalises units to Kelvin / metres on disk, aggregates
   hourly → daily where needed, and upserts into
   `prepared_data/{region_type}/weather_daily.csv`.

Both outputs are append-deduped on `(region_id, date)` — re-running the same
window is idempotent; a corrected re-fetch overwrites the prior value.

---

## DAG

```
download_case_data    ──> parse_case_data
download_weather_data ──> parse_weather_data
```

The two branches are unconnected — either can be disabled independently via
`data.case_download.enabled` / `data.weather_download.enabled` when you want to
refresh only one side.

Steps live in `pipelines/dengue_prep/steps/` and are wired in
`pipelines/dengue_prep/pipeline.py:build_pipeline`.

---

## Steps

| Step | Class | Responsibility |
|---|---|---|
| `download_case_data` | `PrepDownloadCaseDataStep` | Resolve the configured case-source plugin (`filesystem`, `dashboard`, or a custom module), list objects it exposes, validate each is readable. |
| `parse_case_data` | `PrepParseCaseDataStep` | Read every IHIP file, resolve `region_id` (via `region_id_column` → LGD code → geocode/PIP → lat/lon spatial join), apply date + column filters, aggregate to daily counts, upsert into `cases_daily.csv`. |
| `download_weather_data` | `PrepDownloadWeatherDataStep` | Resolve the weather source plugin, incrementally fetch missing months, normalise units, write per-month CSVs under `parsed_output_path/{region_type}/{year}/{year}_{month}.csv`. |
| `parse_weather_data` | `PrepParseWeatherDataStep` | Aggregate hourly / raw weather CSVs to daily via `daily_agg`, upsert into `weather_daily.csv`. |

---

## Config surface

Every top-level section lives under `data:` in the YAML. Full schema is in
`pipelines/dengue_prep/configs.py`.

### `data.region_type`
The admin level being prepared — `district`, `ward`, `mandal`, `zone`, `corp`.
Selects which geojson layer under `data.geojson.base_path` is used for the
region_id allowlist and centroid lookups.

### `data.prepared_data.base_dir`
Where `cases_daily.csv` and `weather_daily.csv` are written. This is a
persistent, shared store — **not** inside the per-run artifact folder.

```yaml
prepared_data:
  base_dir: "ap_datasets/prepared_data"
```

### `data.date_range`
Global date window (`start` / `end`). If `end` is empty, it defaults to the
current run date. Both branches honour this — case rows outside are dropped;
weather fetches are clipped.

### `data.geojson.base_path`
Root directory holding `<region_type>/*.geojson` or `<region_type>s/*.geojson`
files. Used to (a) build the region_id allowlist, (b) compute centroids for
Open-Meteo fetches, (c) provide polygons for the geocode → PIP fallback.

### `data.case_download`
Selects and configures a **case-source plugin**. Full details in
[SOURCES.md](SOURCES.md#case-sources).

```yaml
case_download:
  enabled: true
  source_mode: dashboard            # filesystem | dashboard | dotted.path | ./path.py
  source_path: "./cache/dashboard_ap_cases"
  # dashboard-only:
  base_url: "https://apps.artpark.ai/disease-dashboard"
  date_start: "2021-01-01"
  backfill_days: 30
  disease: "Dengue"
  selected_region_id: "gulb_gba"    # scope filter passed to the API
  chunk_days: 365
```

### `data.case_parse`
Controls IHIP-file parsing.

```yaml
case_parse:
  region_types: ["district"]
  date_column:
    - "Date Of Onset"
    - "Test Performed Date"
    - "Sample Collected Date"
  region_id_column: "Region Id"       # if present, trusted verbatim
  lgd_code_column: "District Code"    # if present, LGD → region_id map
  lat_column: "Latitude"
  lon_column: "Longitude"
  header_row: 0
  filters:                            # AND across entries, OR within values
    - column: "Test Suspected For"
      values: ["Dengue"]
  geocoding:
    enabled: false                    # opt-in; see docs/GEOCODING.md
    address_fields: ["Patient Address"]
    fallback_address_fields: ["Facility"]
    restrict_admin_area_tokens: ["Karnataka"]
    bounds: [11.5, 74.0, 18.5, 78.7]
```

**`date_column` — candidate list with per-row fallback.** The parser accepts
either a string (back-compat) or a list. For each row, it walks the list in
order and uses the first column that yields a parseable date. This matters
for IHIP exports where `Date Of Onset` is sparse but `Sample Collected Date`
is universally populated — the row still gets counted on its most-authoritative
available date.

**Universal region_id allowlist.** Regardless of which resolver strategy runs,
the final row-level `region_id` values are intersected with the set of
`region_id`s parsed out of the geojson layer under
`{geojson.base_path}/{region_type}/`. Any row whose resolved `region_id` is not
in that allowlist is dropped. This is the single guarantee that no downstream
row can reference a region the forecast pipeline doesn't know about.

### `data.weather_download`
Selects and configures a **weather-source plugin**. Full details in
[SOURCES.md](SOURCES.md#weather-sources).

```yaml
weather_download:
  enabled: true
  source_mode: "openmeteo"           # openmeteo | filesystem | cds | dotted.path | ./path.py
  source_path: "od_datasets/weather" # required for source_mode=filesystem
  parsed_output_path: "od_datasets/weather"
  temperature_unit: "celsius"        # unit the SOURCE returns; parser converts to K
  precipitation_unit: "mm"           # unit the SOURCE returns; parser converts to m
  w_params: ["t2m", "d2m", "tp"]
  threshold_km: 25.0
```

### `data.weather_parse`
Controls daily aggregation of weather CSVs.

```yaml
weather_parse:
  region_type: "district"
  weather_variables: ["2mTemperature", "totalPrecipitation", "2mDewpointTemperature"]
  daily_agg:
    - {name: "2mTemperature", op: "mean"}
    - {name: "2mDewpointTemperature", op: "mean"}
    - {name: "totalPrecipitation", op: "sum"}
```

---

## Output artifacts

### `prepared_data/{region_type}/cases_daily.csv`

| Column | Type | Description |
|---|---|---|
| `region_id` | string | e.g. `district_502`, `ward_gba-63` |
| `date` | YYYY-MM-DD | Reporting date (resolved via `date_column` candidate list) |
| `case` | int | Daily confirmed case count |

Only `(region_id, date)` pairs with ≥1 case are stored. A missing row means
zero cases, not missing data. Region IDs are always inside the allowlist
derived from the geojson layer.

### `prepared_data/{region_type}/weather_daily.csv`

| Column | Type | Description |
|---|---|---|
| `region_id` | string | Admin region identifier |
| `date` | YYYY-MM-DD | Date |
| `2mTemperature_mean` | float | Daily mean (Kelvin) |
| `2mDewpointTemperature_mean` | float | Daily mean (Kelvin) |
| `totalPrecipitation_sum` | float | Daily total (metres) |

Column names use ERA5 conventions so the `dengue` pipeline can apply rolling
transforms uniformly regardless of which source produced the data. **Units on
disk are always Kelvin / metres** — the download step normalises based on
`temperature_unit` / `precipitation_unit` config (see [SOURCES.md](SOURCES.md#units)).

### `outputs/prep_summary.md`

Per-run markdown report written into the artifact folder, summarising:
- case files parsed, rows dropped by filter / region-allowlist / date-parse
- weather months fetched vs. skipped-complete vs. upstream-empty
- coverage: which target region_ids have zero cases / zero weather

---

## Common run recipes

```bash
# Andhra Pradesh (districts, dashboard-sourced cases, Open-Meteo weather)
uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ap_district_prep.yaml \
  --run-id ap-prep-$(date +%Y%m%d)

# Odisha (districts, dashboard cases, Open-Meteo weather)
uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/od_district_prep.yaml \
  --run-id od-prep-$(date +%Y%m%d)

# Greater Bengaluru — three geographies share the same case source
uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/gba_ward_prep.yaml \
  --run-id gba-ward-prep-$(date +%Y%m%d)

uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/gba_zone_prep.yaml \
  --run-id gba-zone-prep-$(date +%Y%m%d)

uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/gba_corp_prep.yaml \
  --run-id gba-corp-prep-$(date +%Y%m%d)

# Karnataka (districts, filesystem-sourced weather CSVs already on disk)
uv run python -m acestor.run \
  --pipeline pipelines.dengue_prep.pipeline:build_pipeline \
  --config configs/ka_district_prep.yaml \
  --run-id ka-prep-$(date +%Y%m%d)
```

Required env vars for the `dashboard` case source:

```bash
export DASHBOARD_URL="https://apps.artpark.ai/disease-dashboard"
export DASHBOARD_CLIENT_ID="…"
export DASHBOARD_CLIENT_SECRET="…"
```

See [SOURCES.md — Environment variables](SOURCES.md#environment-variables) for
the full list.
