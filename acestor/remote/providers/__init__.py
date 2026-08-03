"""Cloud provider implementations for the remote runner.

Each provider translates the abstract :class:`CloudProvider` contract to a
specific cloud's SDK. Adding a new backend means dropping a new module here
that implements the same three methods (``provision`` / ``terminate`` /
``describe``) — the runner, CLI, ledger, and SSH transport stay unchanged.
"""

from acestor.remote.providers.base import (
    CloudProvider,
    Lifecycle,
    RemoteHost,
)

__all__ = ["CloudProvider", "Lifecycle", "RemoteHost"]
