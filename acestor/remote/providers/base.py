"""Cloud provider contract — the seam that keeps GCP + others a drop-in later.

Everything downstream of provisioning (SSH, sync, run, artifact download,
ledger) talks to a :class:`RemoteHost`, not to boto3 / google-cloud-compute
directly. Adding a new backend is one module implementing the three abstract
methods below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

Lifecycle = Literal["spot", "on-demand"]


@dataclass
class RemoteHost:
    """A live cloud instance ready to receive SSH.

    ``id`` is provider-native (AWS instance-id, GCP instance-name, etc.) and
    is what :meth:`CloudProvider.terminate` uses to shut the host down. The
    rest is enough for the ssh/sync layer to connect without knowing which
    cloud produced the host.
    """

    id: str
    ip: str
    ssh_user: str
    ssh_key_path: str
    instance_type: str
    lifecycle: Lifecycle
    region: str
    # Provider-native extras (spot request id, zone, etc.) for the ledger.
    metadata: dict = field(default_factory=dict)


class CloudProvider(ABC):
    """Minimal cloud contract the runner depends on.

    Implementations live under ``acestor.remote.providers.<name>``. The runner
    resolves them by name via the ``--remote-provider`` CLI flag (default:
    ``aws``).
    """

    name: str  # e.g. "aws", "gcp", "mock"

    @abstractmethod
    def provision(
        self,
        instance_type: str,
        lifecycle: Lifecycle,
        region: str,
        run_id: str = "",
    ) -> RemoteHost:
        """Allocate an instance and return once it's SSH-ready.

        Blocks until the instance is reachable — the runner's next step is
        immediately to open an SSH connection, so a returned :class:`RemoteHost`
        must actually accept connections.
        """

    @abstractmethod
    def terminate(self, host: RemoteHost) -> None:
        """Tear the instance down. Must be idempotent and never raise on
        \"already gone\" — the runner calls this in a finally-block."""
