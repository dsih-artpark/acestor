"""Unified storage abstraction for acestor.

This is intentionally generic: the same interface is used for
reading "raw" inputs and writing per-run artifacts. Different
logical storages (e.g. "raw", "runs") are exposed via keys on
``PipelineContext.storages``.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any


class Storage(ABC):
    """Abstract base for all storage backends."""

    @abstractmethod
    def read(self, path: str) -> bytes: ...

    @abstractmethod
    def write(self, data: bytes | str, path: str) -> str: ...

    @abstractmethod
    def read_text(self, path: str, encoding: str = "utf-8") -> str: ...

    @abstractmethod
    def write_text(self, text: str, path: str, encoding: str = "utf-8") -> str: ...

    @abstractmethod
    def list_objects(self, prefix: str = "") -> list[str]: ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``. Returns the count removed.

        Idempotent — a prefix that doesn't exist returns 0. Used by the CLI
        ``--clean`` flag to wipe a run-id's artifact subtree before the DAG
        executes so files from a previous run with the same run-id can't
        shadow the current one (e.g. an orphan ``predictions_<model>.csv``
        from a model that's since been removed from the config).
        """
        ...

    def write_json(self, data: Any, path: str) -> str:
        return self.write(json.dumps(data, indent=2, sort_keys=True) + "\n", path)

    def read_json(self, path: str) -> Any:
        return json.loads(self.read_text(path))


@dataclass
class FileStorage(Storage):
    """Filesystem-backed storage rooted at a base directory."""

    base_path: Path

    def read(self, path: str) -> bytes:
        full_path = self.base_path / path
        with full_path.open("rb") as f:
            return f.read()

    def write(self, data: bytes | str, path: str) -> str:
        full_path = self.base_path / path
        full_path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(data, (bytes, bytearray)):
            with full_path.open("wb") as f:
                f.write(data)
        elif isinstance(data, str):
            with full_path.open("w", encoding="utf-8") as f:
                f.write(data)
        else:
            raise TypeError(
                f"Unsupported data type for FileStorage.write: {type(data)!r}"
            )

        return str(full_path)

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        full_path = self.base_path / path
        with full_path.open("r", encoding=encoding) as f:
            return f.read()

    def write_text(self, text: str, path: str, encoding: str = "utf-8") -> str:
        return self.write(text, path)

    def list_objects(self, prefix: str = "") -> list[str]:
        search_dir = self.base_path / prefix if prefix else self.base_path
        if not search_dir.exists():
            return []
        base = self.base_path
        return sorted(
            str(p.relative_to(base)) for p in search_dir.rglob("*") if p.is_file()
        )

    def delete_prefix(self, prefix: str) -> int:
        import shutil

        target = self.base_path / prefix if prefix else self.base_path
        if not target.exists():
            return 0
        if target.is_file():
            target.unlink()
            return 1
        n = sum(1 for p in target.rglob("*") if p.is_file())
        shutil.rmtree(target)
        return n


@dataclass
class S3Storage(Storage):
    """S3-backed storage rooted at a bucket/prefix."""

    bucket: str
    base_prefix: str = ""
    profile: str | None = None
    region: str | None = None

    @cached_property
    def _client(self):
        import boto3
        from botocore.config import Config as BotoConfig

        session_kwargs: dict[str, Any] = {}
        if self.profile:
            session_kwargs["profile_name"] = self.profile

        session = boto3.Session(**session_kwargs)
        return session.client(
            "s3",
            region_name=self.region,
            config=BotoConfig(s3={"addressing_style": "virtual"}),
        )

    def _full_key(self, path: str) -> str:
        prefix = (self.base_prefix or "").rstrip("/")
        path = path.lstrip("/")
        return f"{prefix}/{path}" if prefix else path

    def read(self, path: str) -> bytes:
        key = self._full_key(path)
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def write(self, data: bytes | str, path: str) -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        elif not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                f"Unsupported data type for S3Storage.write: {type(data)!r}"
            )

        key = self._full_key(path)
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return f"s3://{self.bucket}/{key}"

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read(path).decode(encoding)

    def write_text(self, text: str, path: str, encoding: str = "utf-8") -> str:
        return self.write(text.encode(encoding), path)

    def list_objects(self, prefix: str = "") -> list[str]:
        full_prefix = self._full_key(prefix) if prefix else (self.base_prefix or "")
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix += "/"

        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                raw_key = obj["Key"]
                base = (self.base_prefix or "").rstrip("/")
                rel = raw_key[len(base) :].lstrip("/") if base else raw_key
                keys.append(rel)
        return keys

    def delete_prefix(self, prefix: str) -> int:
        full_prefix = self._full_key(prefix) if prefix else (self.base_prefix or "")
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix += "/"

        total = 0
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            contents = page.get("Contents", [])
            if not contents:
                continue
            # S3 delete_objects caps at 1000 keys per request.
            for i in range(0, len(contents), 1000):
                chunk = contents[i : i + 1000]
                self._client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in chunk]},
                )
                total += len(chunk)
        return total
