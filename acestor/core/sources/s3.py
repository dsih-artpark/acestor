from __future__ import annotations

from pathlib import Path

from acestor.io import S3Source as RawS3Source


class S3Source:
    """S3 source with optional local cache strategy.

    strategy:
    - local: read local cache only
    - cloud: read S3 only
    - local_first: try local cache, fallback S3
    - cloud_first: try S3, fallback local cache
    """

    def __init__(
        self,
        *,
        bucket: str,
        base_prefix: str = "",
        aws_profile: str | None = None,
        region: str | None = None,
        cache_enabled: bool = False,
        cache_dir: str = "",
        strategy: str = "local_first",
    ) -> None:
        self.remote = RawS3Source(
            bucket=bucket,
            base_prefix=base_prefix,
            profile=aws_profile,
            region=region,
        )
        self.cache_enabled = cache_enabled
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.strategy = strategy

    def _cache_path(self, relpath: str) -> Path:
        if self.cache_dir is None:
            raise ValueError("cache_dir is not configured for this source")
        return self.cache_dir / relpath.lstrip("/")

    def _read_cache(self, relpath: str) -> bytes:
        if self.cache_dir is None:
            raise FileNotFoundError(relpath)
        p = self._cache_path(relpath)
        if not p.is_file():
            raise FileNotFoundError(str(p))
        return p.read_bytes()

    def _write_cache(self, relpath: str, data: bytes) -> None:
        if self.cache_dir is None or not self.cache_enabled:
            return
        p = self._cache_path(relpath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read(self, relpath: str) -> bytes:
        rel = relpath.lstrip("/")
        if self.strategy == "local":
            return self._read_cache(rel)
        if self.strategy == "cloud":
            data = self.remote.read(rel)
            self._write_cache(rel, data)
            return data

        if self.strategy == "local_first":
            try:
                return self._read_cache(rel)
            except FileNotFoundError:
                data = self.remote.read(rel)
                self._write_cache(rel, data)
                return data

        try:
            data = self.remote.read(rel)
            self._write_cache(rel, data)
            return data
        except Exception:
            return self._read_cache(rel)

    def list_objects(self, prefix: str = "") -> list[str]:
        pref = prefix.lstrip("/")
        if self.strategy in ("local", "local_first") and self.cache_dir:
            base = self.cache_dir / pref if pref else self.cache_dir
            if base.exists():
                return sorted(
                    str(p.relative_to(self.cache_dir))
                    for p in base.rglob("*")
                    if p.is_file()
                )
        return self.remote.list_objects(pref)
