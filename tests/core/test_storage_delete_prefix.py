"""Tests for Storage.delete_prefix — the primitive backing the CLI --clean flag."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


from acestor.io.storage import FileStorage, S3Storage


# ---------------------------------------------------------------------------
# FileStorage
# ---------------------------------------------------------------------------


def test_delete_prefix_removes_files_under_prefix(tmp_path: Path):
    store = FileStorage(base_path=tmp_path)
    store.write_text("a", "run-123/outputs/predictions.csv")
    store.write_text("b", "run-123/outputs/per_model/predictions_nbr.csv")
    store.write_text("c", "run-123/outputs/per_model/predictions_tse.csv")
    # Sibling run-id must not be touched.
    store.write_text("d", "run-456/outputs/predictions.csv")

    removed = store.delete_prefix("run-123")

    assert removed == 3
    assert not (tmp_path / "run-123").exists()
    assert (tmp_path / "run-456/outputs/predictions.csv").exists()


def test_delete_prefix_idempotent_for_nonexistent(tmp_path: Path):
    store = FileStorage(base_path=tmp_path)
    assert store.delete_prefix("never-existed") == 0


def test_delete_prefix_handles_single_file(tmp_path: Path):
    store = FileStorage(base_path=tmp_path)
    store.write_text("x", "lone_file.txt")
    assert store.delete_prefix("lone_file.txt") == 1
    assert not (tmp_path / "lone_file.txt").exists()


def test_delete_prefix_removes_nested_subtree(tmp_path: Path):
    store = FileStorage(base_path=tmp_path)
    store.write_text("1", "run-x/a/b/c/deep.csv")
    store.write_text("2", "run-x/a/b/shallow.csv")
    assert store.delete_prefix("run-x") == 2


# ---------------------------------------------------------------------------
# S3Storage — mocked boto client
# ---------------------------------------------------------------------------


def _stub_s3_storage(pages: list[list[str]]) -> tuple[S3Storage, MagicMock]:
    """Build an S3Storage whose paginator yields the given pages of keys.

    Pages are lists of S3 keys (already-prefixed). Each page becomes one
    list_objects_v2 response.
    """
    store = S3Storage(bucket="b", base_prefix="prefix")
    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {"Contents": [{"Key": k} for k in page]} for page in pages
    ]
    client.get_paginator.return_value = paginator
    store.__dict__["_client"] = client  # bypass cached_property
    return store, client


def test_s3_delete_prefix_calls_delete_objects():
    store, client = _stub_s3_storage([["prefix/run-1/a.csv", "prefix/run-1/b.csv"]])

    removed = store.delete_prefix("run-1")

    assert removed == 2
    client.delete_objects.assert_called_once_with(
        Bucket="b",
        Delete={
            "Objects": [
                {"Key": "prefix/run-1/a.csv"},
                {"Key": "prefix/run-1/b.csv"},
            ]
        },
    )


def test_s3_delete_prefix_returns_zero_on_empty_listing():
    store, client = _stub_s3_storage([[]])
    assert store.delete_prefix("run-empty") == 0
    client.delete_objects.assert_not_called()


def test_s3_delete_prefix_batches_at_1000_keys():
    """S3 delete_objects caps at 1000 keys per call."""
    keys = [f"prefix/run-1/f{i}.csv" for i in range(1500)]
    store, client = _stub_s3_storage([keys])  # one page, 1500 keys

    removed = store.delete_prefix("run-1")

    assert removed == 1500
    assert client.delete_objects.call_count == 2
