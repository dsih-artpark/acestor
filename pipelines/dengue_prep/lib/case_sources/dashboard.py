"""Dengue-dashboard backend case source.

Fetches IHIP-shaped case data from a running dengue-dashboard instance and
writes it as an XLSX file into a local staging directory. The rest of the
prep pipeline reads the XLSX unchanged — the dashboard's ``/api/cases/export.xlsx``
endpoint already emits the 30 canonical IHIP L-form headers plus a ``Region Id``
column, matching what ``parse_case_data`` expects.

Auth flow
---------
1. ``POST {base_url}/api/auth/login`` with ``{"email": <client_id>, "password": <client_secret>}``
   → response body ``{"access_token": "..."}``.
2. ``GET {base_url}/api/cases/export/stream?from=<date>&to=<date>`` with
   ``Authorization: Bearer <token>`` → ``application/x-ndjson`` response body,
   one IHIP record per line. Records are collected and materialised locally
   into an XLSX file so the rest of the pipeline (filename pattern, parser)
   stays unchanged.

The older ``/api/cases/export.xlsx`` endpoint is deprecated on the dashboard
side and used to 504 at the ingress on multi-year windows because the server
rendered the entire xlsx before the first byte. The streaming endpoint has
no such render latency.

Env-var names use the generic ``DASHBOARD_CLIENT_ID`` / ``DASHBOARD_CLIENT_SECRET``
convention (email/password under the hood) so the same source can front any
dashboard-compatible backend without renaming secrets.

Configuration
-------------
YAML (data.case_download):
    source_mode:   dashboard
    source_path:   "./cache/dashboard_cases"   # staging dir for downloaded XLSX
    base_url:      "https://dashboard.example.com"  # falls back to DASHBOARD_URL env
    date_start:    "2024-01-01"                 # sent as `?from=`
    date_end:      ""                            # empty → today; sent as `?to=`
    backfill_days: 30                            # on repeat runs, re-fetch last N days
    disease:       "Dengue"                      # optional filter passed through
    chunk_days:    90                            # split fetch window into N-day chunks; caps per-HTTP RAM

Environment variables:
    DASHBOARD_URL           — base URL (fallback if base_url not in YAML)
    DASHBOARD_CLIENT_ID     — sent as `email` on /api/auth/login
    DASHBOARD_CLIENT_SECRET — sent as `password` on /api/auth/login

Incremental / backfill behaviour
--------------------------------
The source stages one XLSX per fetch, named
``dashboard_<from>_to_<to>.xlsx``, in ``source_path``. On each run:

- First run (empty staging dir): fetch the full ``[date_start, date_end]``.
- Repeat run: fetch ``[max(date_start, latest_staged_end - backfill_days + 1),
  date_end]`` — only the last ``backfill_days`` days plus anything past the
  last staged file's end. Older staged files are left in place.
- Files whose date range overlaps the new fetch window are deleted before the
  new file is written, so ``parse_case_data`` doesn't double-count cases
  present in both.

The reason backfill exists: case reporting lag. A patient with onset date two
weeks ago can be entered into IHIP today, so we cannot assume "dates older
than today are set in stone." ``backfill_days`` is the operator's estimate of
how far back late-arriving cases can appear. To force a full re-download,
delete the staging directory (or set ``backfill_days`` >= the full range).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import requests

from pipelines.dengue_prep.lib.case_sources import CaseSource

# Reparent under the acestor.dengue_prep tree so INFO logs actually reach
# stdout + run.log (acestor's create_logger only attaches handlers to
# ``acestor.*``; module-level loggers under ``pipelines.*`` propagate to root
# where the default level is WARNING and records are silently dropped).
log = logging.getLogger("acestor.dengue_prep.case_sources.dashboard")


_LOGIN_PATH = "/api/auth/login"
# NDJSON streaming endpoint — one IHIP record per line, no server-side render,
# no ingress timeout on multi-year windows (the older ``/api/cases/export.xlsx``
# repeatedly 504'd because it rendered the entire xlsx before the first byte).
_EXPORT_PATH = "/api/cases/export/stream"

_STAGED_FILENAME_RE = re.compile(
    r"^dashboard_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.(?:csv|xlsx)$"
)
# Year-sharded staging (introduced Aug 2026 to replace single-consolidated-
# file staging). Peak memory during daily refresh is capped at one year's
# worth of rows regardless of how many years accumulate — the historical
# year files are cold storage, never re-read after the year ends.
_YEAR_SHARD_RE = re.compile(r"^dashboard_(\d{4})\.csv$")
# New writes are ``.csv``. openpyxl's cost of materialising a big xlsx in RAM
# (~1 KB/cell, so a 200k-row × 30-col linelist peaks at 6-8 GB) OOM'd t3.micro
# and even bigger caller instances during the 2026-08-27 backfill of a
# 217k-record chunk. csv streams row-by-row so peak memory is O(1 row)
# regardless of file size. The ihip parser already reads both formats
# (pipelines/dengue_prep/lib/ihip.py::_read_file), so this is purely a
# staging-format change.
_STAGED_FILE_SUFFIX = ".csv"

# Default candidate date columns for reading max-date out of pre-existing
# xlsx files. Ordered so the most-populated column in dashboard exports comes
# first. Overridden per-run when the step forwards ``case_parse.date_column``.
_DEFAULT_DATE_CANDIDATE_COLS: tuple[str, ...] = (
    "Test Performed Date",
    "Date Of Onset",
    "Sample Collected Date",
)

# Canonical IHIP header set — used only for empty-window fetches to write a
# parseable header-only csv (empty DataFrame → pd.to_csv writes zero bytes,
# and pd.read_csv on that raises EmptyDataError which the parser can't
# distinguish from a real failure). Not authoritative for schema — the actual
# fetch always yields whatever columns the dashboard's export emits.
_IHIP_HEADERS: tuple[str, ...] = (
    "Patient Name",
    "Contact Number",
    "Gender",
    "Age",
    "Village Or Ward",
    "Sub District",
    "Ulb",
    "District",
    "State",
    "Patient Address",
    "Patient Health Id",
    "Patient Transaction Id",
    "Opd Ipd",
    "Provisional Diagnosis",
    "Date Of Onset",
    "Sample Type",
    "Patient Specimen Id",
    "Provisional Diagnosis Name",
    "Test Suspected For",
    "Test Performed",
    "Sample Collected Date",
    "Test Result",
    "Confirmed Diagnosis",
    "Pathogen Name",
    "Pathogen Subtype",
    "Test Performed Date",
    "Facility Name Pform",
    "Facility Name Lform",
    "Address Geocoded Longitude",
    "Address Geocoded Latitude",
    "Region Id",
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _scan_staged_files(staging_dir: Path) -> list[tuple[date, date, Path]]:
    """Return (start_date, end_date, path) for every dashboard_*.xlsx already staged."""
    if not staging_dir.exists():
        return []
    out: list[tuple[date, date, Path]] = []
    for p in staging_dir.iterdir():
        m = _STAGED_FILENAME_RE.match(p.name)
        if m is None:
            continue
        try:
            out.append((_parse_date(m.group(1)), _parse_date(m.group(2)), p))
        except ValueError:
            continue
    return out


def _scan_year_shards(staging_dir: Path) -> list[tuple[int, Path]]:
    """Return (year, path) for every ``dashboard_YYYY.csv`` in the staging dir,
    sorted ascending by year."""
    if not staging_dir.exists():
        return []
    out: list[tuple[int, Path]] = []
    for p in sorted(staging_dir.iterdir()):
        m = _YEAR_SHARD_RE.match(p.name)
        if m is None:
            continue
        try:
            out.append((int(m.group(1)), p))
        except ValueError:
            continue
    return sorted(out)


def _read_staged_file(path: Path):
    """Read a staged case file into a DataFrame. Format-agnostic — picks
    ``pd.read_csv`` or ``pd.read_excel`` based on file suffix so the trim /
    consolidate paths work uniformly across the transition from xlsx to
    csv staging.

    Uses ``path.suffixes`` (not ``.suffix``) so atomic staging paths like
    ``<name>.csv.tmp`` are treated as csv rather than falling through to
    the xlsx reader on the ``.tmp`` suffix.
    """
    import pandas as pd  # noqa: PLC0415

    suffixes = [s.lower() for s in path.suffixes]
    if ".csv" in suffixes:
        return pd.read_csv(path)
    return pd.read_excel(path)


def _write_staged_file(df, path: Path) -> None:
    """Write a DataFrame to a staged case file. csv writes stream row-by-row
    (bounded memory) — always preferred. xlsx is retained only so callers
    that explicitly ask for ``.xlsx`` (existing on-disk files being trimmed
    in place before their eventual conversion) still work.

    Format is picked from the first meaningful suffix in ``path.suffixes``
    so writes to atomic staging paths like ``<name>.csv.tmp`` still land as
    csv rather than falling through to xlsx.
    """
    suffixes = [s.lower() for s in path.suffixes]
    if ".csv" in suffixes:
        df.to_csv(path, index=False)
    else:
        df.to_excel(path, index=False)


def _read_max_date_from_file(
    path: Path, date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS
) -> date | None:
    """Return the max date across candidate date columns, or None. Works for
    both csv- and xlsx-staged files (see ``_read_staged_file``)."""
    import pandas as pd  # noqa: PLC0415

    try:
        df = _read_staged_file(path)
    except Exception as exc:
        log.debug(
            "case_sources.dashboard: could not read %s to detect max date: %s",
            path,
            exc,
        )
        return None
    best: date | None = None
    for col in date_cols:
        if col not in df.columns:
            continue
        parsed = pd.to_datetime(df[col], errors="coerce")
        m = parsed.max()
        if pd.isna(m):
            continue
        d = m.date() if hasattr(m, "date") else _parse_date(str(m)[:10])
        if best is None or d > best:
            best = d
    return best


# Preserve the old name for callers that haven't migrated yet.
_read_max_date_from_xlsx = _read_max_date_from_file


def _scan_external_xlsx_max_dates(
    staging_dir: Path, date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS
) -> list[tuple[date, Path]]:
    """Return (max_date, path) for every non-dashboard-pattern xlsx in staging.

    These are typically files an operator dropped into the dir manually
    (e.g. an existing raw_case export). We use their content max-date to
    anchor the next fetch window, but we NEVER delete them.
    """
    if not staging_dir.exists():
        return []
    out: list[tuple[date, Path]] = []
    for p in staging_dir.iterdir():
        if not p.is_file() or p.suffix.lower() != ".xlsx":
            continue
        if _STAGED_FILENAME_RE.match(p.name):
            continue  # owned-by-us files use the fast filename path
        max_d = _read_max_date_from_xlsx(p, date_cols=date_cols)
        if max_d is not None:
            out.append((max_d, p))
    return out


class Source(CaseSource):
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        staging_dir: Path,
        date_start: str,
        date_end: str,
        backfill_days: int = 30,
        disease: str | None = None,
        selected_region_id: str | None = None,
        date_cols: tuple[str, ...] = _DEFAULT_DATE_CANDIDATE_COLS,
        chunk_days: int = 90,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.staging_dir = staging_dir
        self.date_start = date_start
        self.date_end = date_end
        self.backfill_days = max(0, int(backfill_days))
        self.disease = disease
        self.selected_region_id = selected_region_id
        self.date_cols = tuple(date_cols) if date_cols else _DEFAULT_DATE_CANDIDATE_COLS
        self.chunk_days = max(1, int(chunk_days))
        self._staged_paths: list[str] | None = None
        self._access_token: str | None = None

    @property
    def login_url(self) -> str:
        return f"{self.base_url}{_LOGIN_PATH}"

    @property
    def export_url(self) -> str:
        return f"{self.base_url}{_EXPORT_PATH}"

    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "Source":
        base_url = (
            str(config.get("base_url", "")).strip()
            or os.getenv("DASHBOARD_URL", "").strip()
        )
        if not base_url:
            raise ValueError(
                "case_sources.dashboard: no base_url configured. Set "
                "data.case_download.base_url or DASHBOARD_URL env var."
            )
        client_id = os.getenv("DASHBOARD_CLIENT_ID", "").strip()
        client_secret = os.getenv("DASHBOARD_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ValueError(
                "case_sources.dashboard: DASHBOARD_CLIENT_ID and "
                "DASHBOARD_CLIENT_SECRET env vars are required."
            )
        staging = Path(
            str(config.get("source_path", "")).strip() or "./cache/dashboard_cases"
        )
        # Env fallbacks for fields the current step doesn't forward from YAML —
        # temporary local patch pending upstream fix in _dict_from_cfg (PR #100).
        date_start = (
            str(config.get("date_start", "")).strip()
            or os.getenv("DASHBOARD_DATE_START", "").strip()
        )
        if not date_start:
            raise ValueError(
                "case_sources.dashboard: data.case_download.date_start is required "
                "(the /api/cases/export.xlsx endpoint requires `from`)."
            )
        date_end = (
            str(config.get("date_end", "")).strip()
            or os.getenv("DASHBOARD_DATE_END", "").strip()
            or date.today().isoformat()
        )
        # `or` chain would silently drop backfill_days=0 (falsy). Explicit
        # None/empty check preserves 0 as a legitimate value.
        backfill_days_raw = config.get("backfill_days")
        if backfill_days_raw is None:
            backfill_days_raw = os.getenv("DASHBOARD_BACKFILL_DAYS") or None
        if backfill_days_raw is None or backfill_days_raw == "":
            backfill_days_raw = 30
        try:
            backfill_days = int(backfill_days_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"case_sources.dashboard: backfill_days must be an integer, got "
                f"{backfill_days_raw!r}"
            ) from exc
        return cls(
            base_url=base_url,
            client_id=client_id,
            client_secret=client_secret,
            staging_dir=staging,
            date_start=date_start,
            date_end=date_end,
            backfill_days=backfill_days,
            disease=str(config.get("disease", "")).strip() or None,
            selected_region_id=(
                str(config.get("selected_region_id", "")).strip()
                or os.getenv("DASHBOARD_SELECTED_REGION_ID", "").strip()
                or None
            ),
            date_cols=tuple(config.get("date_column") or _DEFAULT_DATE_CANDIDATE_COLS),
            # Default 90 days per HTTP fetch (was 365). ``_download_xlsx``
            # accumulates the full chunk's records in a Python list before
            # yielding a DataFrame, so peak RAM scales linearly with
            # chunk_days × records/day. A 365-day chunk during a peak-season
            # year (e.g. KA 2023: ~134k rows) allocated ~500-700 MB, pushing
            # t3.micro (1 GB) into swap and making SSH unresponsive
            # (2026-08-31 ka-district-prep). 90-day chunks cap allocation at
            # ~60-80 MB — safe on t3.micro. Trade-off: ~4x more HTTP round
            # trips during a full-history seed (~30-60 s added). Configs and
            # DASHBOARD_CHUNK_DAYS still override.
            chunk_days=int(
                config.get("chunk_days") or os.getenv("DASHBOARD_CHUNK_DAYS") or 90
            ),
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """POST email/password to /api/auth/login, cache the returned token."""
        if self._access_token is not None:
            return self._access_token
        resp = requests.post(
            self.login_url,
            json={"email": self.client_id, "password": self.client_secret},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError(
                f"case_sources.dashboard: login endpoint {self.login_url!r} did not "
                f"return an access token in response body ({body!r})"
            )
        self._access_token = str(token)
        return self._access_token

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _download_xlsx(self, from_date: str, to_date: str) -> bytes:
        """Stream NDJSON from the dashboard, materialise as XLSX bytes.

        Historical/naming context: this method is still called
        ``_download_xlsx`` and still returns XLSX bytes because the rest of
        the source (filename pattern ``dashboard_..._to_....xlsx``, external
        file trim, parser) all expect XLSX on disk. Only the wire format
        changed: we now hit ``/api/cases/export/stream`` (NDJSON), which
        avoids the 60-second Cloudflare ingress timeout that the old
        ``/api/cases/export.xlsx`` used to trip on multi-year windows.
        """
        import io  # noqa: PLC0415
        import json  # noqa: PLC0415

        import pandas as pd  # noqa: PLC0415

        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        params: dict[str, str] = {"from": from_date, "to": to_date}
        if self.disease:
            params["disease"] = self.disease
        if self.selected_region_id:
            params["selected_region_id"] = self.selected_region_id

        import sys  # noqa: PLC0415
        import time  # noqa: PLC0415

        from tqdm import tqdm  # noqa: PLC0415

        # TTY-adaptive progress. Interactive terminal → live tqdm bar with
        # rate + elapsed. Captured logs (run.log / CI / systemd journal) →
        # tqdm is disabled and we fall back to a periodic log heartbeat so
        # the operator sees forward motion in the file too.
        is_tty = sys.stderr.isatty()
        _LOG_EVERY_N = 10_000
        _LOG_EVERY_S = 15.0
        started = time.monotonic()
        last_log = started

        records: list[dict[str, Any]] = []
        bar = tqdm(
            desc=f"cases {from_date}→{to_date}",
            unit=" rec",
            unit_scale=False,
            disable=not is_tty,
            file=sys.stderr,
            leave=False,
        )
        try:
            with requests.get(
                self.export_url,
                headers=headers,
                params=params,
                timeout=600,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if not raw_line:
                        continue
                    try:
                        records.append(json.loads(raw_line))
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"case_sources.dashboard: streaming endpoint "
                            f"{self.export_url!r} returned unparseable NDJSON "
                            f"line for from={from_date} to={to_date}: {exc}. "
                            f"First 200 bytes: {raw_line[:200]!r}"
                        ) from exc
                    bar.update(1)
                    if not is_tty:
                        n = len(records)
                        now = time.monotonic()
                        if n % _LOG_EVERY_N == 0 or (now - last_log) >= _LOG_EVERY_S:
                            elapsed = now - started
                            rate = n / elapsed if elapsed > 0 else 0.0
                            log.info(
                                "case_sources.dashboard: streaming %s → %s — "
                                "%d records so far (%.0f rec/s, %.1fs elapsed)",
                                from_date,
                                to_date,
                                n,
                                rate,
                                elapsed,
                            )
                            last_log = now
        finally:
            bar.close()

        elapsed = time.monotonic() - started
        log.info(
            "case_sources.dashboard: stream complete %s → %s — " "%d records in %.1fs",
            from_date,
            to_date,
            len(records),
            elapsed,
        )

        # Empty windows are valid (nothing reported in the range). Return a
        # header-only csv so downstream can distinguish "no data" from
        # "fetch failed"; the ihip parser already skips empty files cleanly.
        # csv (not xlsx) because openpyxl serialisation is O(cells) memory —
        # a 217k-record chunk peaks at ~6-8 GB and OOM'd production callers
        # on 2026-08-27. csv streams row-by-row, peak RAM is O(1 row).
        #
        # For empty windows we still need SOMETHING serialisable: a totally
        # empty csv has no columns, and pd.read_csv on it raises
        # EmptyDataError which the parser doesn't distinguish from real
        # failure. Emit a canonical placeholder header row for empty windows
        # so the file is always parseable-as-header-only.
        df = pd.DataFrame(records)
        if df.empty:
            df = pd.DataFrame(columns=list(_IHIP_HEADERS))
        buf = io.BytesIO()
        df.to_csv(buf, index=False)
        return buf.getvalue()

    _CHUNK_FLOOR_DAYS = 30  # don't halve below this — deeper indicates a real problem

    # ── Year-sharded staging ────────────────────────────────────────────
    #
    # Design (replaces the trim/consolidate/rename dance):
    #
    # * The staging dir contains one ``dashboard_YYYY.csv`` per year of data.
    # * Each daily run:
    #     1. Fetch [cutoff, today] where cutoff = today - backfill_days.
    #     2. For each year the fetch touched (usually the current year; near
    #        Jan 1 it may span two years):
    #        - Read that year's file (if it exists).
    #        - Drop rows whose date is >= cutoff (the window we just refetched).
    #        - Concat the fetch's rows for that year.
    #        - Atomic write back.
    # * Historical year files are immutable after the year ends. Peak RAM
    #   is bounded by the size of a single year, never by cumulative history.
    #
    # This replaces the previous single-consolidated-file design, whose
    # daily read-into-pandas of the whole history OOM'd t3.micro once the
    # file passed ~50 MB. Year-sharding caps the working set at one year
    # (~25-30 MB on disk → ~150 MB DataFrame) regardless of how many years
    # accumulate.

    def _iter_fetch_chunks(self, from_date: str, to_date: str):
        """Generator: yield one ``pd.DataFrame`` per ``chunk_days``-sized
        fetch chunk. Caller is expected to process each chunk immediately
        (merge into shards, etc.) and let it be GC'd before pulling the next.

        This is the memory-critical replacement for the previous
        ``_download_fresh`` which accumulated every chunk into a list and
        returned the concat — that path OOM'd on multi-year seeds (~500k+
        rows collapsed into one DataFrame ≈ 300-500 MB). Iterating instead
        caps peak RAM at one chunk (typically 5-100 MB).
        """
        import io  # noqa: PLC0415

        import pandas as pd  # noqa: PLC0415

        cur = _parse_date(from_date)
        end = _parse_date(to_date)
        n_chunks = 0
        log.info(
            "case_sources.dashboard: fetching %s → %s (%d days, chunk_days=%d)",
            from_date,
            to_date,
            (end - cur).days + 1,
            self.chunk_days,
        )
        while cur <= end:
            chunk_days = self.chunk_days
            n_chunks += 1
            while True:
                chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
                c_from = cur.isoformat()
                c_to = chunk_end.isoformat()
                log.info(
                    "case_sources.dashboard: [chunk %d] fetching %s → %s (%d days)…",
                    n_chunks,
                    c_from,
                    c_to,
                    chunk_days,
                )
                try:
                    content = self._download_xlsx(c_from, c_to)
                except (requests.Timeout, requests.HTTPError) as exc:
                    transient = isinstance(exc, requests.Timeout) or getattr(
                        exc.response, "status_code", 0
                    ) in (502, 503, 504)
                    if not transient or chunk_days <= self._CHUNK_FLOOR_DAYS:
                        raise
                    chunk_days = max(self._CHUNK_FLOOR_DAYS, chunk_days // 2)
                    log.warning(
                        "case_sources.dashboard: chunk %s → %s failed (%s) — "
                        "halving to %d days and retrying.",
                        c_from,
                        c_to,
                        exc,
                        chunk_days,
                    )
                    continue
                df = pd.read_csv(io.BytesIO(content))
                log.info(
                    "case_sources.dashboard: [chunk %d] %d rows",
                    n_chunks,
                    len(df),
                )
                yield df
                cur = chunk_end + timedelta(days=1)
                break

    def _pick_date_col(self, df: "pd.DataFrame") -> str | None:  # noqa: F821
        """Pick the first ``self.date_cols`` candidate that's populated in
        ``df``. Same walk order the parser uses so we shard on the column
        the parser will resolve dates from downstream."""
        for col in self.date_cols:
            if col in df.columns and df[col].notna().any():
                return col
        return None

    # Streaming migration + streaming merge — memory bounded to one chunk.
    #
    # The naive versions (v1 of #159) OOM'd on t3.micro for two reasons:
    #   1. Migration read the whole legacy file via ``pd.read_csv(path)`` →
    #      a 88 MB CSV became a ~500 MB DataFrame, and per-year groups were
    #      kept in memory until end of migration.
    #   2. Fetch accumulated every ``chunk_days``-sized chunk into a list
    #      and did ``pd.concat`` before merging — a 5-year seed at ~100k
    #      rows/yr concat'd to ~300-500 MB DataFrame.
    #
    # Both paths now stream: CSV migration via ``pd.read_csv(chunksize=)``,
    # fetch via ``_iter_fetch_chunks`` (generator). Each chunk is grouped
    # by year and appended to its shard file, then dropped so the GC can
    # reclaim it before the next chunk lands.

    _CSV_STREAM_CHUNKSIZE = 20_000

    def _migrate_legacy_to_year_shards(self) -> None:
        """One-shot: convert any legacy ``dashboard_<from>_to_<to>.{csv,xlsx}``
        files present in the staging dir into ``dashboard_YYYY.csv`` shards.

        Streaming implementation — CSV legacy files are read in chunks of
        ``_CSV_STREAM_CHUNKSIZE`` rows so peak RAM stays at ~1 chunk (~5-10
        MB DataFrame) regardless of legacy file size. XLSX legacy files must
        be read whole because ``pd.read_excel`` has no ``chunksize`` support
        (openpyxl builds the workbook in memory anyway); a warning is logged
        so the operator can bump the instance size for that one-time
        migration if the xlsx is huge.

        Each year's shard is reset (deleted) on the first chunk that lands
        in it during a given migration run — that way a mid-migration crash
        followed by a retry produces a clean shard rather than doubled rows.

        Runs at the top of every ``_ensure_staged`` — is a no-op once
        migration has succeeded and legacy files have been deleted.
        """

        legacy = [p for _s, _e, p in _scan_staged_files(self.staging_dir)]
        if not legacy:
            return

        log.info(
            "case_sources.dashboard: migrating %d legacy staged file(s) → "
            "year shards (streaming, chunksize=%d)",
            len(legacy),
            self._CSV_STREAM_CHUNKSIZE,
        )

        # Reset any shard the first time this migration writes to it, so a
        # crashed previous attempt can't double-count. Set is per-invocation.
        reset_years: set[int] = set()
        rows_written_by_year: dict[int, int] = {}
        ok_to_delete: list[Path] = []

        for path in legacy:
            try:
                self._stream_migrate_one_file(path, reset_years, rows_written_by_year)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "case_sources.dashboard: migration failed for %s (%s) — "
                    "leaving in place; will retry on next run",
                    path.name,
                    exc,
                )
                # Don't delete this legacy file, but subsequent files can
                # still be migrated (they're independent).
                continue
            ok_to_delete.append(path)

        for year in sorted(rows_written_by_year):
            log.info(
                "case_sources.dashboard: migrated → dashboard_%d.csv (%d rows)",
                year,
                rows_written_by_year[year],
            )

        # Delete only the legacy files that fully migrated.
        for path in ok_to_delete:
            try:
                path.unlink()
            except OSError as exc:
                log.warning(
                    "case_sources.dashboard: could not delete migrated legacy "
                    "file %s: %s",
                    path,
                    exc,
                )

    def _stream_migrate_one_file(
        self,
        path: Path,
        reset_years: set,
        rows_written_by_year: dict,
    ) -> None:
        """Migrate one legacy file to year shards, chunk by chunk. Raises
        if the file can't be read at all (caller decides what to do)."""
        import pandas as pd  # noqa: PLC0415

        suffix = path.suffix.lower()
        if suffix == ".csv":
            iterator = pd.read_csv(path, chunksize=self._CSV_STREAM_CHUNKSIZE)
        elif suffix in (".xlsx", ".xls"):
            log.warning(
                "case_sources.dashboard: xlsx legacy file %s cannot be read "
                "in chunks (openpyxl loads whole workbook in memory). Reading "
                "whole file — bump the compute instance if this OOMs.",
                path.name,
            )
            iterator = iter([_read_staged_file(path)])
        else:
            raise ValueError(f"unsupported legacy file suffix: {suffix!r}")

        for chunk in iterator:
            self._append_chunk_to_shards(chunk, reset_years, rows_written_by_year)

    def _append_chunk_to_shards(
        self,
        chunk: "pd.DataFrame",  # noqa: F821
        reset_years: set,
        rows_written_by_year: dict,
    ) -> None:
        """Split one chunk into per-year groups and append to shard files.
        Shards mentioned in ``reset_years`` for the first time are wiped
        before their first write so a fresh migration run starts from
        empty rather than doubling any leftover partial rows."""
        import pandas as pd  # noqa: PLC0415

        if chunk.empty:
            return
        date_col = self._pick_date_col(chunk)
        if date_col is None:
            log.warning(
                "case_sources.dashboard: chunk of %d rows has no populated "
                "date column — skipping (rows are unshardable)",
                len(chunk),
            )
            return
        years = pd.to_datetime(chunk[date_col], errors="coerce").dt.year
        for year_value, group in chunk.groupby(years, dropna=True):
            year = int(year_value)
            shard_path = self.staging_dir / f"dashboard_{year}.csv"
            if year not in reset_years:
                shard_path.unlink(missing_ok=True)
                reset_years.add(year)
            self._append_group_to_shard(group, shard_path)
            rows_written_by_year[year] = rows_written_by_year.get(year, 0) + len(group)

    def _append_group_to_shard(
        self,
        group: "pd.DataFrame",  # noqa: F821
        shard_path: Path,
    ) -> None:
        """Append ``group`` to ``shard_path`` (csv), tolerating column-set
        drift between the existing shard and the new group.

        Fast path (~99% of calls): shard exists and has matching columns →
        one line-append, peak RAM = ``group`` size only.

        Slow path (column-set mismatch on first append after a drop-stale,
        or when a fetch chunk's schema differs from historical shard's):
        full-rewrite with union columns. Peak RAM = ``shard`` + ``group``,
        bounded by one year's size. Happens at most once per year per run.
        """
        import pandas as pd  # noqa: PLC0415

        if not shard_path.exists() or shard_path.stat().st_size == 0:
            # Fresh shard — write with header.
            group.to_csv(shard_path, mode="w", header=True, index=False)
            return

        existing_cols = list(pd.read_csv(shard_path, nrows=0).columns)
        chunk_cols = list(group.columns)
        if existing_cols == chunk_cols:
            # Fast path: identical column order + set.
            group.to_csv(shard_path, mode="a", header=False, index=False)
            return
        if set(existing_cols) == set(chunk_cols):
            # Same columns, different order — reindex the chunk cheaply.
            group.reindex(columns=existing_cols).to_csv(
                shard_path, mode="a", header=False, index=False
            )
            return

        # Slow path: column set differs. Merge via union columns and rewrite.
        existing = pd.read_csv(shard_path)
        all_cols = list(dict.fromkeys(existing_cols + chunk_cols))
        merged = pd.concat(
            [existing.reindex(columns=all_cols), group.reindex(columns=all_cols)],
            ignore_index=True,
        )
        self._atomic_write_shard(shard_path, merged)

    def _atomic_write_shard(self, path: Path, df: "pd.DataFrame") -> None:  # noqa: F821
        """Write ``df`` to ``path`` via ``.tmp`` + ``os.replace``. A crash
        mid-write leaves the previous shard intact.

        Used by the drop-stale step of the daily refresh (peak memory ≈ one
        year's shard). NOT used for chunk appends during migration or fetch
        merge — those use append-mode writes to keep memory bounded.
        """
        tmp = path.with_suffix(".csv.tmp")
        df.to_csv(tmp, index=False)
        os.replace(tmp, path)

    def _drop_stale_from_shard(
        self,
        shard_path: Path,
        cutoff: date,
        date_col: str,
    ) -> int:
        """Read ``shard_path``, drop rows dated ``>= cutoff``, atomic-write
        back. Returns the number of rows kept (the rest are dropped and
        will be re-supplied by the fresh fetch).

        Peak memory ≈ one year's shard (~25-30 MB CSV → ~150 MB DataFrame
        for a normal year at KA scale). This is bounded by year-size, not
        cumulative history.
        """
        import pandas as pd  # noqa: PLC0415

        if not shard_path.is_file():
            return 0
        try:
            df = pd.read_csv(shard_path)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "case_sources.dashboard: shard %s unreadable (%s) — leaving "
                "it in place; fresh fetch rows will be appended to it as-is",
                shard_path.name,
                exc,
            )
            return 0
        if date_col not in df.columns:
            # Shard doesn't have the date col we're filtering on — safest
            # to keep everything.
            return len(df)
        parsed = pd.to_datetime(df[date_col], errors="coerce")
        keep_mask = parsed.isna() | (parsed < pd.Timestamp(cutoff))
        kept = df[keep_mask]
        dropped = len(df) - len(kept)
        if dropped == 0:
            return len(df)
        self._atomic_write_shard(shard_path, kept)
        log.info(
            "case_sources.dashboard: %s → dropped %d rows >= %s (kept %d)",
            shard_path.name,
            dropped,
            cutoff.isoformat(),
            len(kept),
        )
        return len(kept)

    def _fetch_and_merge_streaming(
        self, from_date: str, to_date: str, cutoff: date
    ) -> None:
        """Fetch ``[from_date, to_date]`` chunk-by-chunk and append each
        chunk directly into per-year shard files.

        The first chunk that lands in a given year triggers a one-time
        ``_drop_stale_from_shard`` for that year (dropping rows dated
        ``>= cutoff``). Subsequent chunks for the same year are just
        appended. Never accumulates chunks in memory.

        Peak RAM per chunk: ~5-100 MB depending on ``chunk_days``. Peak
        RAM for a stale-drop: one year's shard (~150 MB DataFrame). Overall
        cap: ~150-200 MB regardless of total data volume, fits on t3.micro.
        """
        import pandas as pd  # noqa: PLC0415

        dropped_years: set[int] = set()
        rows_appended_by_year: dict[int, int] = {}
        chunk_count = 0

        for chunk_df in self._iter_fetch_chunks(from_date, to_date):
            chunk_count += 1
            if chunk_df.empty:
                continue
            date_col = self._pick_date_col(chunk_df)
            if date_col is None:
                log.warning(
                    "case_sources.dashboard: fetch chunk %d has no populated "
                    "date column; skipping (%d rows)",
                    chunk_count,
                    len(chunk_df),
                )
                continue
            years = pd.to_datetime(chunk_df[date_col], errors="coerce").dt.year
            for year_value, group in chunk_df.groupby(years, dropna=True):
                year = int(year_value)
                shard_path = self.staging_dir / f"dashboard_{year}.csv"

                # First chunk that touches this year — drop stale rows now
                # so the append below can't produce duplicates in the
                # [cutoff, today] window.
                if year not in dropped_years:
                    self._drop_stale_from_shard(shard_path, cutoff, date_col)
                    dropped_years.add(year)

                self._append_group_to_shard(group, shard_path)
                rows_appended_by_year[year] = rows_appended_by_year.get(year, 0) + len(
                    group
                )

        for year in sorted(rows_appended_by_year):
            log.info(
                "case_sources.dashboard: fetched → dashboard_%d.csv " "(+%d rows)",
                year,
                rows_appended_by_year[year],
            )
        if not rows_appended_by_year:
            log.info(
                "case_sources.dashboard: fetch produced no shard-able rows "
                "(%d chunks, all empty or undated)",
                chunk_count,
            )

    def _ensure_staged(self) -> list[str]:
        """Year-sharded refresh.

        1. Migrate any legacy ``dashboard_<from>_to_<to>.{csv,xlsx}`` files
           to per-year shards (no-op after the first successful run).
        2. Compute fetch window ``[cutoff, today]``:
              - If no shards exist yet: full-history seed from ``date_start``.
              - Otherwise: ``today - backfill_days``.
        3. Fetch fresh rows into a DataFrame.
        4. For each year the fetch touched: drop existing rows dated
           ``>= cutoff`` from that year's shard, concat the fresh rows for
           that year, atomic write.
        5. Return the list of shards + any external xlsx files.

        No trim, no rename, no consolidate. Peak memory is bounded by one
        year's rows.
        """
        if self._staged_paths is not None:
            return self._staged_paths
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        self._migrate_legacy_to_year_shards()

        end_d = _parse_date(self.date_end)
        start_d = _parse_date(self.date_start)
        existing_shards = _scan_year_shards(self.staging_dir)

        if existing_shards and self.backfill_days > 0:
            # Daily refresh: refetch the tail window.
            cutoff = end_d - timedelta(days=max(0, self.backfill_days - 1))
            cutoff = max(cutoff, start_d)
        elif existing_shards and self.backfill_days == 0:
            # Explicit no-backfill: fetch only whatever hasn't been seen.
            # Use max shard year to estimate "seen through end of that year";
            # anything from the next year onwards is fetched.
            max_year = existing_shards[-1][0]
            cutoff = max(start_d, date(max_year + 1, 1, 1))
            if cutoff > end_d:
                log.info(
                    "case_sources.dashboard: backfill_days=0 and all shards "
                    "up-to-date — nothing to fetch"
                )
                self._staged_paths = self._collect_staged_paths()
                return self._staged_paths
        else:
            # First-ever run: full-history seed.
            cutoff = start_d

        log.info(
            "case_sources.dashboard: refresh window %s → %s (backfill_days=%d, "
            "%d shard(s) already on disk)",
            cutoff.isoformat(),
            end_d.isoformat(),
            self.backfill_days,
            len(existing_shards),
        )
        self._fetch_and_merge_streaming(
            cutoff.isoformat(), end_d.isoformat(), cutoff=cutoff
        )

        self._staged_paths = self._collect_staged_paths()
        return self._staged_paths

    def _collect_staged_paths(self) -> list[str]:
        """List every file the parser should read: year shards + any
        operator-provided external xlsx files that survived migration."""
        owned = [p.name for _y, p in _scan_year_shards(self.staging_dir)]
        legacy = [p.name for _s, _e, p in _scan_staged_files(self.staging_dir)]
        external = [
            p.name
            for _, p in _scan_external_xlsx_max_dates(
                self.staging_dir, date_cols=self.date_cols
            )
        ]
        return sorted(set(owned + legacy + external))

    def list_objects(self, prefix: str = "") -> list[str]:
        return [p for p in self._ensure_staged() if p.startswith(prefix)]

    def read(self, path: str) -> bytes:
        self._ensure_staged()
        target = self.staging_dir / path
        if not target.exists():
            raise FileNotFoundError(
                f"case_sources.dashboard: staged file not found: {target}"
            )
        return target.read_bytes()
