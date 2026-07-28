# Case & Weather Source Plugins

`dengue_prep` treats both raw case files and raw weather records as coming from
**pluggable sources**. Each source is a Python module exposing a class named
`Source`; a config value (`source_mode`) selects which module to load. This
lets the same pipeline pull cases from a local directory today and a REST API
tomorrow without touching the steps themselves.

See [DENGUE_PREP.md](DENGUE_PREP.md) for how the prep pipeline consumes what
these sources return.

---

## Plugin architecture

Both `data.case_download.source_mode` and `data.weather_download.source_mode`
are resolved by the same three-step rule (`load_source` in
`pipelines/dengue_prep/lib/case_sources/__init__.py` and
`.../weather_sources/__init__.py`):

1. **Path-like** — value contains a path separator (`/`, `\`) or starts with
   `.` → loaded as a filesystem path (absolute or relative to cwd).
2. **Dotted module path** — value contains `.` (e.g.
   `ap_datasets.ihip_api`) → converted to `ap_datasets/ihip_api.py` relative
   to cwd and loaded as a file.
3. **Bare name** — looked up as a built-in at
   `pipelines/dengue_prep/lib/{case,weather}_sources/<name>.py`.

The resolved module must expose a top-level class named `Source`. It must be
a subclass of the appropriate ABC (`CaseSource` / `WeatherSource`).

```yaml
# built-in
source_mode: dashboard

# custom, sitting next to the config
source_mode: ./ap_datasets/my_source.py

# custom, importable via dotted path
source_mode: ap_datasets.ihip_api
```

---

## Case sources

### Contract — `CaseSource`

Defined in `pipelines/dengue_prep/lib/case_sources/__init__.py`.

```python
class CaseSource(ABC):
    @classmethod
    @abstractmethod
    def build(cls, config: Mapping[str, Any]) -> "CaseSource":
        """Construct from the data.case_download config dict."""

    @abstractmethod
    def list_objects(self, prefix: str = "") -> list[str]:
        """Return source-relative paths / IDs of raw case files."""

    @abstractmethod
    def read(self, path: str) -> bytes:
        """Return raw bytes for one path returned by list_objects."""
```

Why bytes and not records: cases are file-oriented downstream — the parse step
reads xlsx / csv from disk. API-backed sources fetch data, cache it on disk,
and hand the step the path — same code path as filesystem, no per-source
special-casing in the step itself.

### Built-in: `filesystem`

Source: `pipelines/dengue_prep/lib/case_sources/filesystem.py`.

Reads raw IHIP files from a local directory. Any file listed there is fair
game — `.xlsx`, `.xls`, `.csv`. `source_path` (preferred) or
`filesystem_base_path` names the directory.

```yaml
case_download:
  enabled: true
  source_mode: filesystem
  source_path: "ka_datasets/raw_case"
```

### Built-in: `dashboard`

Source: `pipelines/dengue_prep/lib/case_sources/dashboard.py`. Fetches
IHIP-shaped XLSX from a running **dengue-dashboard** instance. The
dashboard's `/api/cases/export.xlsx` endpoint already emits the 30 canonical
IHIP L-form headers plus a `Region Id` column — same shape the filesystem
source would produce.

**Auth flow.** Two calls per fetch:

1. `POST {base_url}/api/auth/login`
   Body `{"email": DASHBOARD_CLIENT_ID, "password": DASHBOARD_CLIENT_SECRET}`
   → response `{"access_token": "..."}`.
2. `GET {base_url}/api/cases/export.xlsx?from=<date>&to=<date>` with
   `Authorization: Bearer <token>` → binary XLSX body.

Env-var names use generic `CLIENT_ID` / `CLIENT_SECRET` semantics
(email/password under the hood) so the same source can front any
dashboard-compatible backend without renaming secrets.

**`chunk_days` — chunked fetch.** The full `[date_start, date_end]` window is
split into `chunk_days`-day chunks (default 365) and each chunk is a separate
HTTP call, staged as `dashboard_<from>_to_<to>.xlsx` in `source_path`. On
`Timeout` / `502` / `503` / `504` the source halves the current chunk and
retries — down to a floor of 30 days, below which the failure is re-raised as
a genuine backend problem.

**`backfill_days` — late-arriving cases.** Case reporting has a real lag: a
patient with onset two weeks ago may be entered into IHIP today. On every
run the source re-fetches the last `backfill_days` days (default 30) even if
they're already staged. Set to 0 to disable; set very high to force full
re-download (or just delete the staging directory).

**Incremental gap-fill.** Beyond the tail backfill, the source scans staged
files (both its own `dashboard_*.xlsx` and any external xlsx an operator
dropped in) and computes **missing sub-ranges** in the config window. This
catches the silent-hole case where an old mid-timeline file was deleted or
never fetched — a bug where the previous "start from latest staged end"
heuristic quietly skipped anything before it. See `_compute_fetch_ranges` in
the source for the full logic.

**Overlap handling.** When a new fetch range overlaps an existing staged
file:
- **Fully superseded** → the old file is deleted.
- **Partially overlapping** → the old file's rows on/after `fetch_from` are
  trimmed in place and it's renamed to reflect its new shorter range.
- **External (operator-provided) xlsx** → rows on/after `fetch_from` are
  trimmed but the file is never deleted or renamed.

Together these guarantee `parse_case_data` never sees an
`(region_id, date)` covered by two files.

**Config.**

```yaml
case_download:
  enabled: true
  source_mode: dashboard
  source_path: "./cache/dashboard_ap_cases"    # staging directory
  base_url: "https://apps.artpark.ai/disease-dashboard"  # or DASHBOARD_URL env
  date_start: "2021-01-01"                     # required — passed as `?from=`
  date_end: ""                                 # empty → today
  backfill_days: 30                            # re-fetch trailing window
  chunk_days: 365                              # split into N-day HTTP calls
  disease: "Dengue"                            # ?disease=… filter
  selected_region_id: "gulb_gba"               # scope to a subtree
```

---

## Weather sources

### Contract — `WeatherSource`

Defined in `pipelines/dengue_prep/lib/weather_sources/__init__.py`.

```python
class WeatherSource(ABC):
    should_persist: bool = True

    @abstractmethod
    def get_weather_all_regions(
        self,
        start_date: str,
        end_date: str,
        region_type: str,
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Return daily records for every region of the given type."""

    def get_weather_for_regions(self, locations, start_date, end_date,
                                region_type, config):
        """Default: filter get_weather_all_regions() by region_id. Override
        for APIs that support efficient per-location queries."""
```

`should_persist=False` (as on the filesystem source) tells the step to skip
its incremental fetch/write loop and reference source files in place —
useful when the source files already ARE the canonical daily CSVs.

### Record schema

Each returned dict must be one region × one day, shaped as:

| Field | Type | Notes |
|---|---|---|
| `date` | str `YYYY-MM-DD` | required |
| `region_id` | str | LGD code or equivalent identifier, required |
| `t2m` | float | 2 m temperature, in the source's native unit |
| `d2m` | float | 2 m dewpoint temperature |
| `tp` | float | total precipitation |
| `name` | str | region name (optional) |
| `parent` | str | parent region identifier (optional) |
| `parent_name` | str | parent region name (optional) |

### Units

Sources return values in **their native units** and declare what those are
via `temperature_unit` (`celsius`|`kelvin`) and `precipitation_unit`
(`mm`|`m`) in the config. The step passes every record through
`normalize_records()` before writing CSVs; **files on disk are always Kelvin
and metres** so the downstream `dengue` pipeline sees a single unit
convention regardless of source.

The normaliser also sanity-checks temperature values (mean > 150 ⇒ Kelvin)
and logs a warning + falls back to the detected unit if the declared unit
would produce a physically impossible double-conversion.

### Built-in: `openmeteo`

Source: `pipelines/dengue_prep/lib/weather_sources/openmeteo.py`. Fetches
ERA5 reanalysis via the free Open-Meteo archive API — no key required, but
subject to a rate limit (600 API-units/min).

- Native units: **°C** for temperature, **mm** for precipitation (declare
  these in config; the parser converts to K / m on disk).
- Batching: 50 regions per HTTP call, sequential.
- ERA5 archive lag: the source silently caps `end_date` at `today - 5 days`
  because Open-Meteo's ERA5 archive isn't populated for the trailing few
  days.
- `get_weather_for_regions` is overridden: if the caller supplies lat/lon
  pairs, it fetches those coords directly instead of loading the full
  geojson layer.

```yaml
weather_download:
  enabled: true
  source_mode: openmeteo
  parsed_output_path: "ap_datasets/weather"
  temperature_unit: "celsius"
  precipitation_unit: "mm"
```

### Built-in: `filesystem`

Source: `pipelines/dengue_prep/lib/weather_sources/filesystem.py`. Reads
pre-prepared CSVs from a local directory (or Docker mount). Files are
combined, `time` → `date` if needed, and filtered to the requested date
window.

- `should_persist = False` — the step references the source files directly;
  no fetch loop runs.
- **No unit normalisation is applied** — files are expected to already be in
  the pipeline's canonical units (K / m).

```yaml
weather_download:
  enabled: true
  source_mode: filesystem
  source_path: "ka_datasets/weather"
  parsed_output_path: "ka_datasets/weather"
```

### Built-in (legacy): `cds`

Uses the Copernicus CDS API (`reanalysis-era5-land`) via a NetCDF cache. Not
plugin-shaped — dispatched by the step directly when `source_mode: cds` and
requires `CDS_API_URL` / `CDS_API_KEY` env vars.

---

## Writing a custom source

A minimal case source in one file. Drop it anywhere on disk and point
`source_mode` at it.

```python
# ap_datasets/my_api_source.py
from typing import Any, Mapping
from pipelines.dengue_prep.lib.case_sources import CaseSource

class Source(CaseSource):
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "Source":
        return cls(endpoint=str(config["source_path"]))

    def list_objects(self, prefix: str = "") -> list[str]:
        # Return whatever opaque IDs describe the batches you can fetch.
        return ["batch-2024-01.xlsx"]

    def read(self, path: str) -> bytes:
        # Fetch (from an API, S3, wherever) and return raw XLSX bytes.
        return _http_get(f"{self.endpoint}/{path}")
```

```yaml
case_download:
  enabled: true
  source_mode: ./ap_datasets/my_api_source.py
  source_path: "https://internal.example.com/cases"
```

A minimal weather source is the same shape with `WeatherSource` and a
single `get_weather_all_regions` method returning the [record
schema](#record-schema) above.

---

## Environment variables

The `dashboard` case source is fully env-configurable — YAML wins, env vars
fill anything left blank.

| Variable | Purpose | Falls back from |
|---|---|---|
| `DASHBOARD_URL` | base URL for the dashboard backend | `data.case_download.base_url` |
| `DASHBOARD_CLIENT_ID` | login `email` field (required) | — |
| `DASHBOARD_CLIENT_SECRET` | login `password` field (required) | — |
| `DASHBOARD_DATE_START` | first day to fetch | `data.case_download.date_start` |
| `DASHBOARD_DATE_END` | last day to fetch | `data.case_download.date_end` (default: today) |
| `DASHBOARD_BACKFILL_DAYS` | trailing window to always re-fetch | `data.case_download.backfill_days` (default: 30) |
| `DASHBOARD_SELECTED_REGION_ID` | scope filter passed as `?selected_region_id=` | `data.case_download.selected_region_id` |
| `DASHBOARD_CHUNK_DAYS` | max days per HTTP call | `data.case_download.chunk_days` (default: 365) |

Also honoured by the prep pipeline more broadly:

| Variable | Purpose |
|---|---|
| `DENGUE_PREP_CASE_SOURCE` | fallback for `data.case_download.source_path` |
| `CDS_API_URL`, `CDS_API_KEY` | required for `source_mode: cds` |
| `GBA_CDS_DATASET`, `GBA_CDS_VARIABLES`, `GBA_CDS_CACHE_PATH`, `GBA_CDS_PARSED_OUTPUT_PATH` | override CDS defaults |
