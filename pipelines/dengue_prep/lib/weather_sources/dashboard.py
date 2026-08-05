"""Dashboard weather source — GET /api/weather/observed?state=X&from_date=&to_date=.

Returns per-region daily records with rainfall_mm / temp_c / dewpoint_c. The
step's normalizer converts to Kelvin/metres before writing CSVs to disk, so
this source declares its native units as ``celsius`` + ``mm``.

Why use this instead of Open-Meteo:

- Open-Meteo throttles hard on historical bulk-fetches. The dashboard already
  has all the state's history loaded (it syncs from ERA5 via the admin sync
  configs) and hands it to us in one request with no rate limit.
- The dashboard's weather stays fresh daily via the admin sync workers, so
  we don't have to worry about ERA5's ~5-day publication lag either.

Auth reuses the same dashboard credentials as the case + geojson sources
(``DASHBOARD_URL``, ``DASHBOARD_CLIENT_ID``, ``DASHBOARD_CLIENT_SECRET``).

Configuration
-------------
YAML (``data.weather_download``):

.. code-block:: yaml

    data:
      weather_download:
        enabled: true
        source_mode: dashboard
        scope_id: "karnataka"                 # matches case + geojson dashboard sources
        base_url: "https://apps.artpark.ai/disease-dashboard"  # or DASHBOARD_URL env
        temperature_unit: "celsius"           # what THIS source returns natively
        precipitation_unit: "mm"              # what THIS source returns natively

Pass the scope_id lowercase — the dashboard's ``/api/weather/observed?state=``
query param accepts scope_id verbatim (``karnataka``, ``odisha``, ``gulb_gba``).
Earlier versions of this docstring flagged a title-case quirk for KA; the
dashboard has since normalised and lowercase now works across scopes.
Title-case (``"Karnataka"``, ``"Odisha"``) currently returns an empty list.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Mapping

import requests

from pipelines.dengue_prep.lib.weather_sources import WeatherSource

# See case_sources/dashboard.py for the reparenting rationale.
log = logging.getLogger("acestor.dengue_prep.weather_sources.dashboard")


_LOGIN_PATH = "/api/auth/login"
_OBSERVED_PATH = "/api/weather/observed"


class Source(WeatherSource):
    should_persist = True

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        scope_id: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope_id = scope_id
        self._access_token: str | None = None

    @classmethod
    def build(cls, config: Mapping[str, Any]) -> "Source":
        base_url = (
            str(config.get("base_url", "")).strip()
            or os.getenv("DASHBOARD_URL", "").strip()
        )
        if not base_url:
            raise ValueError(
                "weather_sources.dashboard: no base_url configured. Set "
                "data.weather_download.base_url or DASHBOARD_URL env var."
            )
        client_id = os.getenv("DASHBOARD_CLIENT_ID", "").strip()
        client_secret = os.getenv("DASHBOARD_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ValueError(
                "weather_sources.dashboard: DASHBOARD_CLIENT_ID and "
                "DASHBOARD_CLIENT_SECRET env vars are required."
            )
        scope_id = (
            str(config.get("scope_id", "")).strip()
            or os.getenv("DASHBOARD_SELECTED_REGION_ID", "").strip()
        )
        if not scope_id:
            raise ValueError(
                "weather_sources.dashboard: scope_id is required. Set "
                "data.weather_download.scope_id or DASHBOARD_SELECTED_REGION_ID "
                "env var (same value used by the case + geojson dashboard sources)."
            )
        return cls(
            base_url=base_url,
            client_id=client_id,
            client_secret=client_secret,
            scope_id=scope_id,
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        if self._access_token is not None:
            return self._access_token
        resp = requests.post(
            f"{self.base_url}{_LOGIN_PATH}",
            json={"email": self.client_id, "password": self.client_secret},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError(
                f"weather_sources.dashboard: login did not return an access "
                f"token (body: {body!r})"
            )
        self._access_token = token
        return token

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def get_weather_all_regions(
        self,
        start_date: str,
        end_date: str,
        region_type: str,  # noqa: ARG002 — endpoint is state-wide, not level-scoped
        config: Mapping[str, Any],  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """GET /api/weather/observed and flatten into per-day-per-region records."""
        url = f"{self.base_url}{_OBSERVED_PATH}"
        headers = {"Authorization": f"Bearer {self._get_access_token()}"}
        # The endpoint's query param name was `state=` originally, later
        # renamed to `scope_id=` (dashboard normalising around a single
        # canonical name across all endpoints). Empirically as of
        # 2026-08-05 the endpoint returns HTTP 422 for `state=` and only
        # accepts `scope_id=`. Value is the same lowercase scope id used
        # by the case + geojson dashboard sources.
        params = {
            "scope_id": self.scope_id,
            "from_date": start_date,
            "to_date": end_date,
            "bucket": "daily",
        }
        log.info(
            "weather_sources.dashboard: fetching scope_id=%r %s → %s from %s",
            self.scope_id,
            start_date,
            end_date,
            url,
        )
        started = time.monotonic()
        resp = requests.get(url, headers=headers, params=params, timeout=300)
        resp.raise_for_status()
        payload = resp.json()

        if not isinstance(payload, list):
            raise RuntimeError(
                f"weather_sources.dashboard: expected a list from {url}, got "
                f"{type(payload).__name__}"
            )

        # Flatten [{region_id, region_name, points: [...]}, ...] into per-day
        # records with the schema declared in weather_sources/__init__.py.
        records: list[dict[str, Any]] = []
        for region in payload:
            rid = region.get("region_id")
            rname = region.get("region_name")
            if not rid:
                continue
            for p in region.get("points") or []:
                # period_start == period_end for bucket=daily
                date_str = p.get("period_start") or p.get("period_end")
                if not date_str:
                    continue
                records.append(
                    {
                        "date": str(date_str),
                        "region_id": rid,
                        "t2m": p.get("temp_c"),
                        "d2m": p.get("dewpoint_c"),
                        "tp": p.get("rainfall_mm"),
                        "name": rname or "",
                        "parent": "",  # not returned by this endpoint
                        "parent_name": "",
                    }
                )

        elapsed = time.monotonic() - started
        log.info(
            "weather_sources.dashboard: scope_id=%r %s → %s — %d regions, "
            "%d daily records in %.1fs",
            self.scope_id,
            start_date,
            end_date,
            len(payload),
            len(records),
            elapsed,
        )
        return records
