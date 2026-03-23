"""I/O related primitives for acestor (storage backends)."""

from .storage import Storage, FileStorage, S3Storage  # noqa: F401

# Backward/semantic aliases for source-oriented integrations.
FileSource = FileStorage
S3Source = S3Storage
