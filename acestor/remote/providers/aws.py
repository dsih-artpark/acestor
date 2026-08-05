"""AWS EC2 provider — spot + on-demand, guaranteed termination.

Slice 2 of issue #111. Uses ``RunInstances`` for both lifecycles (on-demand
is the default; spot rides the ``InstanceMarketOptions`` block, which is
simpler than the separate ``RequestSpotInstances`` API and returns an
instance-id synchronously).

Design notes
------------
* **AMI resolution** — if the caller doesn't pass ``ami``, we resolve the
  latest Ubuntu 24.04 LTS x86_64 AMI for the region via SSM Parameter Store
  (Canonical publishes ``/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id``).
  Removes the "which AMI is this region on today" babysitting.
* **Wait strategy** — after ``RunInstances`` we (1) block on the
  ``instance_running`` waiter (state), then (2) poll TCP 22 open with a
  short timeout (reachability). ``instance-status-ok`` takes 2-3 min extra
  and we don't need it — SSH readiness is the actual gate.
* **Termination** — ``terminate_instances`` is idempotent per AWS docs, but
  can raise ``InvalidInstanceID.NotFound`` for very stale IDs. Swallowed
  and logged — the runner's finally-block must never re-raise here.

Credentials
-----------
Resolved via the standard boto3 chain (env vars, ``~/.aws/credentials``,
instance profile). No new secret handling in acestor.
"""

from __future__ import annotations

import base64
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from acestor.remote.providers.base import CloudProvider, Lifecycle, RemoteHost

log = logging.getLogger(__name__)


_UBUNTU_2404_SSM_PARAM = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
)
_SSH_READY_TIMEOUT_SECONDS = 300  # 5 min from state=running to port 22 open
_SSH_POLL_INTERVAL_SECONDS = 5


@dataclass
class AWSProviderConfig:
    key_name: str  # existing EC2 key pair name in the target region
    key_path: str  # absolute path to the matching private key (~/.ssh/foo.pem)
    security_group_ids: list[str] = field(default_factory=list)
    subnet_id: str = ""  # optional — falls back to default subnet in the AZ
    ami: str = ""  # optional — auto-resolved via SSM if empty
    iam_instance_profile: str = ""  # optional
    ssh_user: str = "ubuntu"
    profile: str = ""  # optional boto3 profile name
    # cloud-init user-data placeholder — the bootstrap slice will fill this
    user_data: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    # Root EBS volume size. Ubuntu 24.04 AMI defaults to 8 GB which fills up
    # once uv sync stages the wheel cache — 20 GB is enough headroom for
    # every extra including the dengue geospatial + xgboost / sklearn stack.
    root_volume_gb: int = 20


class AWSProvider(CloudProvider):
    name = "aws"

    def __init__(self, cfg: AWSProviderConfig) -> None:
        self.cfg = cfg
        self._session = None  # lazy — imports boto3 only when used
        self._region = ""  # captured on first provision() call

    # ------------------------------------------------------------------
    # boto3 wiring — lazy import so the mock provider path works without it
    # ------------------------------------------------------------------

    def _client(self, service: str, region: str) -> Any:
        import boto3  # imported lazily so unrelated CLI paths don't require it

        if self._session is None:
            self._session = boto3.Session(profile_name=self.cfg.profile or None)
        return self._session.client(service, region_name=region)

    # ------------------------------------------------------------------
    # AMI resolution
    # ------------------------------------------------------------------

    def _resolve_ami(self, region: str) -> str:
        if self.cfg.ami:
            return self.cfg.ami
        ssm = self._client("ssm", region)
        resp = ssm.get_parameter(Name=_UBUNTU_2404_SSM_PARAM)
        ami = resp["Parameter"]["Value"]
        log.info(
            "aws provider: resolved Ubuntu 24.04 AMI in %s → %s (SSM)", region, ami
        )
        return ami

    # ------------------------------------------------------------------
    # Provision
    # ------------------------------------------------------------------

    def provision(
        self,
        instance_type: str,
        lifecycle: Lifecycle,
        region: str,
        run_id: str = "",
    ) -> RemoteHost:
        self._region = region
        ec2 = self._client("ec2", region)
        ami = self._resolve_ami(region)

        # Tags: acestor:run-id lets the webui + operator tools correlate a
        # running compute box back to its scheduler log / ledger row.
        tags = [
            {"Key": "acestor:remote-runner", "Value": "true"},
            {"Key": "acestor:lifecycle", "Value": lifecycle},
        ]
        if run_id:
            tags.append({"Key": "acestor:run-id", "Value": run_id})
            # Also set the Name tag for readability in the AWS console.
            tags.append({"Key": "Name", "Value": f"acestor:{run_id}"})
        tags.extend({"Key": k, "Value": v} for k, v in self.cfg.tags.items())
        run_kwargs: dict[str, Any] = {
            "ImageId": ami,
            "InstanceType": instance_type,
            "KeyName": self.cfg.key_name,
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": [{"ResourceType": "instance", "Tags": tags}],
        }
        if self.cfg.root_volume_gb:
            run_kwargs["BlockDeviceMappings"] = [
                {
                    "DeviceName": "/dev/sda1",  # Ubuntu 24.04 AMI root device
                    "Ebs": {
                        "VolumeSize": int(self.cfg.root_volume_gb),
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                }
            ]
        if self.cfg.security_group_ids:
            run_kwargs["SecurityGroupIds"] = self.cfg.security_group_ids
        if self.cfg.subnet_id:
            run_kwargs["SubnetId"] = self.cfg.subnet_id
        if self.cfg.iam_instance_profile:
            run_kwargs["IamInstanceProfile"] = {"Name": self.cfg.iam_instance_profile}
        if self.cfg.user_data:
            run_kwargs["UserData"] = base64.b64encode(
                self.cfg.user_data.encode("utf-8")
            ).decode("ascii")
        if lifecycle == "spot":
            run_kwargs["InstanceMarketOptions"] = {
                "MarketType": "spot",
                "SpotOptions": {
                    "SpotInstanceType": "one-time",
                    "InstanceInterruptionBehavior": "terminate",
                },
            }

        log.info(
            "aws provider: RunInstances type=%s lifecycle=%s region=%s ami=%s",
            instance_type,
            lifecycle,
            region,
            ami,
        )
        resp = ec2.run_instances(**run_kwargs)
        instance = resp["Instances"][0]
        instance_id = instance["InstanceId"]
        log.info(
            "aws provider: instance launched — id=%s (waiting for running)", instance_id
        )

        # Wait for state=running
        waiter = ec2.get_waiter("instance_running")
        waiter.wait(
            InstanceIds=[instance_id],
            WaiterConfig={"Delay": 5, "MaxAttempts": 60},  # up to 5 min
        )

        # Re-describe to get the assigned public IP
        desc = ec2.describe_instances(InstanceIds=[instance_id])
        running = desc["Reservations"][0]["Instances"][0]
        public_ip = running.get("PublicIpAddress") or running.get("PrivateIpAddress")
        if not public_ip:
            # Terminate the orphan and surface loudly — the runner won't be
            # able to SSH into a host with no reachable IP.
            self._terminate(ec2, instance_id)
            raise RuntimeError(
                f"aws provider: instance {instance_id} has no public or private "
                f"IP after reaching running state — check subnet auto-assign-ip."
            )

        log.info(
            "aws provider: instance %s running at %s — waiting for SSH",
            instance_id,
            public_ip,
        )
        # If SSH never comes up, the instance is still billing — terminate
        # before re-raising so a failed provision doesn't leak compute.
        # (The runner's outer finally-block can't do this: provision hasn't
        # returned a RemoteHost yet, so runner.host is still None.)
        try:
            self._wait_for_ssh(public_ip)
        except Exception:
            log.warning(
                "aws provider: SSH never came up on %s — terminating instance %s "
                "to avoid orphan compute charges",
                public_ip,
                instance_id,
            )
            self._terminate(ec2, instance_id)
            raise

        return RemoteHost(
            id=instance_id,
            ip=public_ip,
            ssh_user=self.cfg.ssh_user,
            ssh_key_path=self.cfg.key_path,
            instance_type=instance_type,
            lifecycle=lifecycle,
            region=region,
            metadata={
                "ami": ami,
                "az": running.get("Placement", {}).get("AvailabilityZone", ""),
                "spot_request_id": running.get("SpotInstanceRequestId", ""),
            },
        )

    # ------------------------------------------------------------------
    # Terminate
    # ------------------------------------------------------------------

    def terminate(self, host: RemoteHost) -> None:
        ec2 = self._client("ec2", host.region)
        self._terminate(ec2, host.id)

    @staticmethod
    def _terminate(ec2: Any, instance_id: str) -> None:
        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
            log.info("aws provider: terminate_instances sent for %s", instance_id)
        except Exception as exc:
            # botocore.exceptions.ClientError with 'InvalidInstanceID.NotFound'
            # means the instance was already gone — perfectly fine.
            if "InvalidInstanceID.NotFound" in str(exc):
                log.info(
                    "aws provider: instance %s already gone (NotFound) — nothing to do",
                    instance_id,
                )
                return
            # Anything else is unusual but not fatal for the runner — log and
            # move on. Runner's finally-block must not re-raise.
            log.exception(
                "aws provider: terminate_instances failed for %s — check the "
                "console to avoid dangling spend",
                instance_id,
            )

    # ------------------------------------------------------------------
    # SSH readiness
    # ------------------------------------------------------------------

    @staticmethod
    def _wait_for_ssh(
        ip: str,
        timeout: int = _SSH_READY_TIMEOUT_SECONDS,
        interval: int = _SSH_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Poll TCP 22 open. Cheap, no auth — SSH banner grab is enough."""
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((ip, 22), timeout=5):
                    log.info("aws provider: SSH port 22 open on %s", ip)
                    return
            except OSError as exc:
                last_err = exc
                time.sleep(interval)
        raise TimeoutError(
            f"aws provider: SSH port 22 on {ip} not reachable within {timeout}s "
            f"(last error: {last_err})"
        )
