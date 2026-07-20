"""Dashboard backend case source.

Fetches raw case rows from an authenticated dashboard API using the OAuth
client-credentials flow, and writes them as CSV files into a local staging
directory. The rest of the prep pipeline reads those CSVs unchanged, so the
parser's contract is preserved.

Configuration
-------------
YAML (data.case_download):
    source_mode:  dashboard
    source_path:  "./cache/dashboard_cases"   # staging dir for downloaded CSVs
    api_url:      "https://dashboard.example.com/api/cases"
    token_url:    "https://dashboard.example.com/oauth/token"  # optional
    date_start:   "2024-01-01"                # optional; empty → source's default
    date_end:     ""                           # empty → today's date

Environment variables (all required unless noted):
    DASHBOARD_URL          — API base URL (fallback if api_url not in YAML)
    DASHBOARD_TOKEN_URL    — OAuth token endpoint (fallback if token_url not in YAML)
    DASHBOARD_CLIENT_ID    — OAuth client_id
    DASHBOARD_CLIENT_SECRET — OAuth client_secret
    DASHBOARD_SCOPE        — optional OAuth scope string (space-separated)

TODOs before first real run
---------------------------
The API contract below is a placeholder — fill in the real bits for your
dashboard:
- ``_fetch_all_rows`` assumes ``?page=N`` + JSON ``{"results": [...]}``. Adjust
  to your dashboard's pagination (cursor, offset, none, ...).
- ``_rename_for_parser`` maps dashboard JSON keys → the column names the
  parse_case_data step expects (Sample Collected Date, Date Of Onset,
  Region Id, ...). Update the mapping to match your dashboard's payload.
- If your dashboard already returns pre-resolved region IDs, keep the
  region_id column as-is. If it returns lat/lon, add a geocoding step.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import requests

from pipelines.dengue_prep.lib.case_sources import CaseSource

log = logging.getLogger(__name__)


# Placeholder — replace keys with your dashboard's real JSON field names.
# Values are the column names parse_case_data reads.
_DASHBOARD_TO_PARSER_COLUMNS: dict[str, str] = {
    "sample_collection_date": "Sample Collected Date",
    "date_of_onset": "Date Of Onset",
    "region_id": "Region Id",
    "test_result": "Test Result",
    "patient_id": "Patient Id",
}


class Source(CaseSource):
    def __init__(
        self,
        api_url: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        staging_dir: Path,
        scope: str | None = None,
        date_start: str | None = None,
        date_end: str | None = None,
    ) -> None:
        self.api_url = api_url
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.staging_dir = staging_dir
        self.date_start = date_start
        self.date_end = date_end or date.today().isoformat()
        self._staged_paths: list[str] | None = None
        self._access_token: str | None = None

    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "Source":
        def _required(config_key: str, env_var: str) -> str:
            value = (
                str(config.get(config_key, "")).strip()
                or os.getenv(env_var, "").strip()
            )
            if not value:
                raise ValueError(
                    f"case_sources.dashboard: missing {config_key!r} in config or "
                    f"{env_var} env var."
                )
            return value

        api_url = _required("api_url", "DASHBOARD_URL")
        token_url = _required("token_url", "DASHBOARD_TOKEN_URL")
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
        return cls(
            api_url=api_url,
            token_url=token_url,
            client_id=client_id,
            client_secret=client_secret,
            staging_dir=staging,
            scope=os.getenv("DASHBOARD_SCOPE", "").strip() or None,
            date_start=str(config.get("date_start", "")).strip() or None,
            date_end=str(config.get("date_end", "")).strip() or None,
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """OAuth 2.0 client-credentials grant."""
        if self._access_token is not None:
            return self._access_token
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            payload["scope"] = self.scope
        resp = requests.post(self.token_url, data=payload, timeout=30)
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise RuntimeError(
                f"case_sources.dashboard: token endpoint {self.token_url!r} did not "
                f"return access_token in response body ({resp.json()!r})"
            )
        self._access_token = str(token)
        return self._access_token

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def _fetch_all_rows(self) -> list[dict[str, Any]]:
        """Paginate the API until the server returns an empty batch.

        TODO: adjust pagination to match your dashboard. Common shapes:
        - ``?page=N`` with ``results`` array (assumed here)
        - ``?offset=N&limit=M``
        - cursor-based ``?cursor=<opaque>`` following ``next_cursor`` in each response
        - no pagination — single call returns everything
        """
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = requests.get(
                self.api_url,
                headers=headers,
                params={
                    "start_date": self.date_start,
                    "end_date": self.date_end,
                    "page": page,
                },
                timeout=60,
            )
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("results", [])
            if not batch:
                break
            rows.extend(batch)
            log.debug(
                "case_sources.dashboard: page=%d rows_this_page=%d cumulative=%d",
                page,
                len(batch),
                len(rows),
            )
            page += 1
        return rows

    # ------------------------------------------------------------------
    # Column mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _rename_for_parser(df: pd.DataFrame) -> pd.DataFrame:
        """Rename dashboard-native JSON keys to the columns parse_case_data expects.

        TODO: update _DASHBOARD_TO_PARSER_COLUMNS at the top of this module to
        match your dashboard's payload keys.
        """
        renamed = df.rename(columns=_DASHBOARD_TO_PARSER_COLUMNS)
        missing_required = [
            parser_col
            for parser_col in ("Date Of Onset", "Region Id")
            if parser_col not in renamed.columns
        ]
        if missing_required:
            log.warning(
                "case_sources.dashboard: staged CSV is missing parser-required "
                "columns %s — parse_case_data will drop these rows. Check the "
                "_DASHBOARD_TO_PARSER_COLUMNS mapping in dashboard.py.",
                missing_required,
            )
        return renamed

    # ------------------------------------------------------------------
    # CaseSource interface
    # ------------------------------------------------------------------

    def _ensure_staged(self) -> list[str]:
        """Fetch once (per Source instance), stage a CSV, remember its path."""
        if self._staged_paths is not None:
            return self._staged_paths
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        rows = self._fetch_all_rows()
        log.info(
            "case_sources.dashboard: fetched %d rows from %s (start=%s end=%s)",
            len(rows),
            self.api_url,
            self.date_start or "-",
            self.date_end,
        )
        if not rows:
            # Emit an empty staged file to keep the downstream sanity check happy;
            # parse_case_data will report zero cases and the run will fail early
            # with a clear message rather than silent success.
            raise RuntimeError(
                "case_sources.dashboard: API returned zero rows — refusing to "
                "stage an empty file. Check date range and auth."
            )

        df = self._rename_for_parser(pd.DataFrame(rows))
        out_path = self.staging_dir / f"dashboard_{self.date_end}.csv"
        df.to_csv(out_path, index=False)
        log.info("case_sources.dashboard: wrote %d rows → %s", len(df), out_path)
        self._staged_paths = [out_path.name]
        return self._staged_paths

    def list_objects(self, prefix: str = "") -> list[str]:
        staged = self._ensure_staged()
        return [p for p in staged if p.startswith(prefix)]

    def read(self, path: str) -> bytes:
        self._ensure_staged()
        target = self.staging_dir / path
        if not target.exists():
            raise FileNotFoundError(
                f"case_sources.dashboard: staged file not found: {target}"
            )
        return target.read_bytes()
