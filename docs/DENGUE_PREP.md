# dengue_prep Pipeline

`dengue_prep` is the **data preparation pipeline**. It runs independently of the
forecast and its only job is to keep `prepared_data/` current. The downstream
`dengue` pipeline reads from there and does not care how the data arrived.

See [RUNNING_PIPELINES.md](RUNNING_PIPELINES.md) for how to run and schedule both
pipelines together.

---

## DAG

```
download_case_data    ──> parse_case_data
download_weather_data ──> parse_weather_data
```

The two branches run in parallel. Each `download_*` step fetches or locates raw
files; each `parse_*` step aggregates them and upserts into `prepared_data/`.

---

## Steps

| Step | Class | What it does |
|---|---|---|
| `download_case_data` | `PrepDownloadCaseDataStep` | Locates raw case files (filesystem or S3) and returns a list of file references |
| `parse_case_data` | `PrepParseCaseDataStep` | Reads IHIP-format files, maps rows to `region_id` via LGD code or spatial join, aggregates to daily case counts, upserts into `prepared_data/{region_type}/cases_daily.csv` |
| `download_weather_data` | `PrepDownloadWeatherDataStep` | Downloads weather data from OpenMeteo, CDS, or a pre-parsed filesystem/S3 source |
| `parse_weather_data` | `PrepParseWeatherDataStep` | Aggregates hourly weather CSVs to daily totals, applies date filter, upserts into `prepared_data/{region_type}/weather_daily.csv` |

---

## Output contract

Both files are append-deduped on `(region_id, date)` — re-running the same date
range is safe and idempotent. A re-uploaded corrected file overwrites the
previous count for that `(region_id, date)`.

### `prepared_data/{region_type}/cases_daily.csv`

| Column | Type | Description |
|---|---|---|
| `region_id` | string | Admin region identifier (e.g. `district_502`) |
| `date` | YYYY-MM-DD | Reporting date |
| `case` | int | Daily confirmed case count |

Only dates with at least one case are stored. Missing rows mean zero cases, not
missing data.

### `prepared_data/{region_type}/weather_daily.csv`

| Column | Type | Description |
|---|---|---|
| `region_id` | string | Admin region identifier |
| `date` | YYYY-MM-DD | Date |
| `2mTemperature_mean` | float | Daily mean 2 m temperature |
| `2mTemperature_max` | float | Daily max 2 m temperature |
| `2mTemperature_min` | float | Daily min 2 m temperature |
| `2mDewpointTemperature_mean` | float | Daily mean 2 m dewpoint temperature |
| `totalPrecipitation_sum` | float | Daily total precipitation |

Weather columns use ERA5 naming so the `dengue` pipeline can apply rolling
aggregation before renaming to `t2m_mean`, `tp_sum`, etc.

**Units:** if `data.weather_download.convert_units: true`, values are in ERA5
units (K for temperature/dewpoint, m for precipitation). Otherwise OpenMeteo
native units (°C, mm) are stored. Set `convert_units: true` when mixing with
CDS data so units are consistent.

---

## Weather source modes

Controlled by `data.weather_download.source_mode`:

| Mode | Description | Requires |
|---|---|---|
| `openmeteo` | Free ERA5 reanalysis via Open-Meteo archive API | Internet access |
| `cds` | ERA5-Land via Copernicus CDS | CDS API key + licence agreement |
| `filesystem` | Read pre-downloaded CSVs from a local directory or S3 | Pre-parsed files |

OpenMeteo downloads are batched (50 regions per API call) and run sequentially
to stay within the 600 API-unit/min rate limit.

---

## Case region ID resolution

Three strategies, tried in order of priority:

1. **LGD code column** — if `data.case_parse.lgd_code_column` is configured and
   the column is present in the file, the integer code maps directly to
   `{region_type}_{code}` (e.g. District Code 502 → `district_502`). Fast and
   exact.

2. **Geocode → spatial join** — if `data.case_parse.geocoding.enabled` is `true`
   and the source has neither an LGD code column nor lat/lon, each row's
   `address_fields` are composed and sent to Google's Geocoding API (with a
   persistent JSON cache). Results are validated against the source's named area
   (fuzzy ≤ 1 char, generic stopwords filtered) and against
   `restrict_admin_area_tokens` (e.g. require `"Karnataka"` to drop cross-state
   hits). Validated coords are then PIP'd against the region geojson layer.
   Rows that don't resolve on `address_fields` are retried with
   `fallback_address_fields` (typically a facility-name column).
   `bounds` viewport-biases the geocoder toward the admin region.

   See `data.case_parse.geocoding` in the [Config Reference](CONFIG_REFERENCE.md#datacaseparsegeocoding).

3. **Spatial join (direct)** — if a file already has trustworthy
   `(Latitude, Longitude)` columns, those are matched against GeoJSON polygons
   for the configured `region_type`. Requires `data.geojson.base_path` to be set.

---

## Artifacts

`dengue_prep` writes minimal artifacts (run metadata, logs). The substantive
output is the `prepared_data/` directory, which is written to the path configured
in `data.prepared_data.base_dir` — **not** inside the per-run artifact folder.
This is intentional: `prepared_data/` is a shared, persistent store that
accumulates across runs.

---

## Config reference

All configuration keys are documented in [CONFIG_REFERENCE.md](CONFIG_REFERENCE.md):

- [`data.region_type`](CONFIG_REFERENCE.md#dataregion_type-dengue_prep-pipeline)
- [`data.date_range`](CONFIG_REFERENCE.md#datadate_range-dengue_prep-pipeline)
- [`data.case_download`](CONFIG_REFERENCE.md#datacasedownload)
- [`data.case_parse`](CONFIG_REFERENCE.md#datacaseparse)
- [`data.weather_download`](CONFIG_REFERENCE.md#dataweatherdownload)
- [`data.weather_parse`](CONFIG_REFERENCE.md#dataweatherparse)
- [`data.prepared_data`](CONFIG_REFERENCE.md#dataprepared_data-dengue-pipeline)
