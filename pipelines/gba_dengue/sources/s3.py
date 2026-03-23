from __future__ import annotations

import os

from acestor.core.sources import S3Source


def _required_env(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise ValueError(f"Environment variable {name} is required for S3 source")
    return val


def _optional_env(name: str) -> str | None:
    val = os.getenv(name, "").strip()
    return val or None


_AWS_PROFILE = _optional_env("AWS_PROFILE")
_AWS_REGION = _optional_env("AWS_REGION")


def _make_source(
    *, bucket: str, base_prefix: str, cache_dir: str, strategy: str
) -> S3Source:
    cache_enabled = bool(int(os.getenv("GBA_S3_CACHE_ENABLED", "1")))
    # Allow per-source strategy/enable overrides while keeping a single shared "credentials" setup.
    return S3Source(
        bucket=bucket,
        base_prefix=base_prefix,
        aws_profile=_AWS_PROFILE,
        region=_AWS_REGION,
        cache_enabled=cache_enabled,
        cache_dir=cache_dir,
        strategy=strategy,
    )


def get_case_source() -> S3Source:
    return _make_source(
        bucket=_required_env("RAW_CASE_BUCKET"),
        base_prefix=os.getenv("RAW_CASE_PREFIX", "").strip(),
        cache_dir=os.getenv("GBA_S3_CASE_CACHE_DIR", "./cache/raw_case"),
        strategy=os.getenv("GBA_S3_CASE_STRATEGY", "local_first"),
    )


def get_weather_source() -> S3Source:
    return _make_source(
        bucket=_required_env("RAW_WEATHER_BUCKET"),
        base_prefix=os.getenv("RAW_WEATHER_PREFIX", "").strip(),
        cache_dir=os.getenv("GBA_S3_WEATHER_CACHE_DIR", "./cache/raw_weather"),
        strategy=os.getenv("GBA_S3_WEATHER_STRATEGY", "local_first"),
    )
