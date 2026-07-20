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
2. ``GET {base_url}/api/cases/export.xlsx?from=<date>&to=<date>`` with
   ``Authorization: Bearer <token>`` → binary XLSX response body.

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

log = logging.getLogger(__name__)


_LOGIN_PATH = "/api/auth/login"
_EXPORT_PATH = "/api/cases/export.xlsx"

_STAGED_FILENAME_RE = re.compile(
    r"^dashboard_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})\.xlsx$"
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.staging_dir = staging_dir
        self.date_start = date_start
        self.date_end = date_end
        self.backfill_days = max(0, int(backfill_days))
        self.disease = disease
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
        date_start = str(config.get("date_start", "")).strip()
        if not date_start:
            raise ValueError(
                "case_sources.dashboard: data.case_download.date_start is required "
                "(the /api/cases/export.xlsx endpoint requires `from`)."
            )
        date_end = str(config.get("date_end", "")).strip() or date.today().isoformat()
        backfill_days_raw = config.get("backfill_days", 30)
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
        """GET /api/cases/export.xlsx?from=..&to=.. and return the raw bytes."""
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        params: dict[str, str] = {"from": from_date, "to": to_date}
        if self.disease:
            params["disease"] = self.disease
        resp = requests.get(
            self.export_url, headers=headers, params=params, timeout=300, stream=False
        )
        resp.raise_for_status()
        content = resp.content
        if not content:
            raise RuntimeError(
                f"case_sources.dashboard: export endpoint {self.export_url!r} "
                f"returned zero bytes for from={from_date} to={to_date}."
            )
        # Quick sniff — XLSX is a ZIP archive; its first two bytes are 'PK'.
        if not content.startswith(b"PK"):
            raise RuntimeError(
                f"case_sources.dashboard: export endpoint returned non-XLSX bytes "
                f"(first 64: {content[:64]!r}). Auth or endpoint issue?"
            )
        return content

    # ------------------------------------------------------------------
    # CaseSource interface
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Incremental logic
    # ------------------------------------------------------------------

    def _compute_fetch_window(self) -> tuple[str, str]:
        """Decide which sub-range to fetch this run.

        - First run (nothing staged): fetch the full [date_start, date_end].
        - Subsequent runs: fetch from ``max(date_start, latest_end - backfill_days + 1)``
          through ``date_end``. This re-pulls the last N days to catch late-
          arriving cases in the reporting lag window while leaving older files
          untouched.
        """
        end_d = _parse_date(self.date_end)
        start_d = _parse_date(self.date_start)
        staged = _scan_staged_files(self.staging_dir)
        if not staged:
            return self.date_start, self.date_end
        latest_end = max(rng[1] for rng in staged)
        backfill_from = latest_end - timedelta(days=max(0, self.backfill_days - 1))
        fetch_from = max(start_d, backfill_from)
        # Clamp: never fetch a from > date_end.
        if fetch_from > end_d:
            fetch_from = end_d
        return fetch_from.isoformat(), self.date_end

    def _delete_overlapping_staged(self, from_date: str, to_date: str) -> int:
        """Remove any existing staged file whose date range overlaps the new fetch.

        Overlap = ``existing_end >= fetch_from AND existing_start <= fetch_to``.
        Prevents parse_case_data from double-counting cases that live in both
        files.
        """
        fetch_from_d = _parse_date(from_date)
        fetch_to_d = _parse_date(to_date)
        removed = 0
        for existing_start, existing_end, path in _scan_staged_files(self.staging_dir):
            if existing_end >= fetch_from_d and existing_start <= fetch_to_d:
                try:
                    path.unlink()
                    removed += 1
                    log.info(
                        "case_sources.dashboard: removed overlapping staged file %s "
                        "(covers %s → %s)",
                        path.name,
                        existing_start.isoformat(),
                        existing_end.isoformat(),
                    )
                except OSError as exc:
                    log.warning(
                        "case_sources.dashboard: could not delete %s: %s", path, exc
                    )
        return removed

    def _ensure_staged(self) -> list[str]:
        """Fetch (possibly just the backfill window), stage the XLSX, remember it.

        Behavior on repeat runs: only re-fetches the last ``backfill_days`` days
        of the range (+ any dates past the last staged file's end). Existing
        files that cover older, out-of-backfill dates stay untouched — the
        parse_case_data step will read every ``dashboard_*.xlsx`` in the dir.
        """
        if self._staged_paths is not None:
            return self._staged_paths
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        from_date, to_date = self._compute_fetch_window()
        log.info(
            "case_sources.dashboard: fetch window %s → %s (backfill_days=%d)",
            from_date,
            to_date,
            self.backfill_days,
        )
        removed = self._delete_overlapping_staged(from_date, to_date)
        if removed:
            log.info(
                "case_sources.dashboard: removed %d overlapping staged file(s) "
                "before writing the new fetch",
                removed,
            )

        content = self._download_xlsx(from_date, to_date)
        filename = f"dashboard_{from_date}_to_{to_date}.xlsx"
        out_path = self.staging_dir / filename
        out_path.write_bytes(content)
        log.info(
            "case_sources.dashboard: wrote %d bytes → %s",
            len(content),
            out_path,
        )
        # Return every dashboard_*.xlsx now on disk — the parse step reads them
        # all, so older-untouched files stay in the download step's copied_files list.
        self._staged_paths = sorted(
            p.name for _, _, p in _scan_staged_files(self.staging_dir)
        )
        return self._staged_paths

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
