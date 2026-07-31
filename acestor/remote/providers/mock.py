"""Mock provider — used to wire the runner end-to-end before AWS lands.

Provisions nothing, terminates nothing, returns a fake host pointing at
localhost. Slice 1 uses this to verify CLI → runner → ledger without touching
any cloud API.
"""

from __future__ import annotations

import logging

from acestor.remote.providers.base import CloudProvider, Lifecycle, RemoteHost

log = logging.getLogger(__name__)


class MockProvider(CloudProvider):
    name = "mock"

    def provision(
        self, instance_type: str, lifecycle: Lifecycle, region: str
    ) -> RemoteHost:
        log.info(
            "mock provider: pretending to provision %s (%s) in %s",
            instance_type,
            lifecycle,
            region,
        )
        return RemoteHost(
            id="i-mock000000",
            ip="127.0.0.1",
            ssh_user="ubuntu",
            ssh_key_path="",
            instance_type=instance_type,
            lifecycle=lifecycle,
            region=region,
            metadata={"note": "mock provider — no real instance"},
        )

    def terminate(self, host: RemoteHost) -> None:
        log.info("mock provider: pretending to terminate %s", host.id)
