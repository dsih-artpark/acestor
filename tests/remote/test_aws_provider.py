"""AWSProvider unit tests — boto3 mocked, no real API calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from acestor.remote.providers.aws import AWSProvider, AWSProviderConfig
from acestor.remote.providers.base import RemoteHost


@pytest.fixture
def cfg() -> AWSProviderConfig:
    return AWSProviderConfig(
        key_name="acestor-key",
        key_path="/home/u/.ssh/acestor.pem",
        security_group_ids=["sg-abc"],
        subnet_id="subnet-xyz",
    )


def _fake_ec2_client(
    instance_id: str = "i-0abc123", public_ip: str = "1.2.3.4"
) -> MagicMock:
    """A MagicMock EC2 client that satisfies the AWSProvider happy path."""
    ec2 = MagicMock(name="ec2_client")
    ec2.run_instances.return_value = {"Instances": [{"InstanceId": instance_id}]}
    waiter = MagicMock(name="instance_running_waiter")
    ec2.get_waiter.return_value = waiter
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": instance_id,
                        "PublicIpAddress": public_ip,
                        "Placement": {"AvailabilityZone": "ap-south-1a"},
                    }
                ]
            }
        ]
    }
    return ec2


def _fake_ssm_client(ami: str = "ami-ubuntu2404") -> MagicMock:
    ssm = MagicMock(name="ssm_client")
    ssm.get_parameter.return_value = {"Parameter": {"Value": ami}}
    return ssm


def test_provision_ondemand_calls_run_instances_and_waits_for_ssh(cfg):
    ec2 = _fake_ec2_client()
    ssm = _fake_ssm_client(ami="ami-ubuntu")

    p = AWSProvider(cfg)
    p._client = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda service, region: ssm if service == "ssm" else ec2
    )

    with patch.object(AWSProvider, "_wait_for_ssh", return_value=None) as wait:
        host = p.provision("c7i.2xlarge", "on-demand", "ap-south-1")

    # RunInstances was called with the resolved AMI, without spot options
    kwargs = ec2.run_instances.call_args.kwargs
    assert kwargs["ImageId"] == "ami-ubuntu"
    assert kwargs["InstanceType"] == "c7i.2xlarge"
    assert kwargs["KeyName"] == "acestor-key"
    assert kwargs["SecurityGroupIds"] == ["sg-abc"]
    assert kwargs["SubnetId"] == "subnet-xyz"
    assert "InstanceMarketOptions" not in kwargs

    # Waiter used, then SSH poll invoked
    ec2.get_waiter.assert_called_once_with("instance_running")
    wait.assert_called_once_with("1.2.3.4")

    # Returned host is wired up
    assert isinstance(host, RemoteHost)
    assert host.id == "i-0abc123"
    assert host.ip == "1.2.3.4"
    assert host.lifecycle == "on-demand"
    assert host.metadata["ami"] == "ami-ubuntu"
    assert host.metadata["az"] == "ap-south-1a"


def test_provision_spot_adds_market_options(cfg):
    ec2 = _fake_ec2_client()
    ssm = _fake_ssm_client()
    p = AWSProvider(cfg)
    p._client = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda service, region: ssm if service == "ssm" else ec2
    )
    with patch.object(AWSProvider, "_wait_for_ssh", return_value=None):
        p.provision("c7i.2xlarge", "spot", "ap-south-1")

    kwargs = ec2.run_instances.call_args.kwargs
    assert kwargs["InstanceMarketOptions"]["MarketType"] == "spot"
    assert (
        kwargs["InstanceMarketOptions"]["SpotOptions"]["InstanceInterruptionBehavior"]
        == "terminate"
    )


def test_provision_uses_explicit_ami_and_skips_ssm(cfg):
    cfg.ami = "ami-fixed-123"
    ec2 = _fake_ec2_client()
    ssm = _fake_ssm_client()
    p = AWSProvider(cfg)
    p._client = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda service, region: ssm if service == "ssm" else ec2
    )
    with patch.object(AWSProvider, "_wait_for_ssh", return_value=None):
        host = p.provision("c7i.2xlarge", "on-demand", "ap-south-1")

    ssm.get_parameter.assert_not_called()
    assert host.metadata["ami"] == "ami-fixed-123"


def test_provision_terminates_orphan_when_no_ip(cfg):
    """If describe_instances returns no IP, the orphan must be terminated."""
    ec2 = _fake_ec2_client()
    # No public + no private → provider must clean up
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-0abc123",
                        "Placement": {"AvailabilityZone": "ap-south-1a"},
                    }
                ]
            }
        ]
    }
    ssm = _fake_ssm_client()
    p = AWSProvider(cfg)
    p._client = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda service, region: ssm if service == "ssm" else ec2
    )
    with pytest.raises(RuntimeError, match="no public or private IP"):
        p.provision("c7i.2xlarge", "on-demand", "ap-south-1")
    ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-0abc123"])


def test_provision_terminates_orphan_when_ssh_never_comes_up(cfg):
    """If _wait_for_ssh raises (e.g. security group blocks 22), the instance
    is already running and billing. provision() must terminate before
    re-raising — the runner's outer finally-block can't help because
    provision hasn't returned a RemoteHost yet."""
    ec2 = _fake_ec2_client()
    ssm = _fake_ssm_client()
    p = AWSProvider(cfg)
    p._client = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda service, region: ssm if service == "ssm" else ec2
    )

    with patch.object(
        AWSProvider,
        "_wait_for_ssh",
        side_effect=TimeoutError("SSH port 22 on 1.2.3.4 not reachable within 300s"),
    ):
        with pytest.raises(TimeoutError, match="not reachable"):
            p.provision("c7i.2xlarge", "on-demand", "ap-south-1")

    ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-0abc123"])


def test_terminate_is_idempotent_on_not_found(cfg):
    ec2 = _fake_ec2_client()
    ec2.terminate_instances.side_effect = RuntimeError(
        "An error occurred (InvalidInstanceID.NotFound) when calling..."
    )
    p = AWSProvider(cfg)
    p._client = MagicMock(return_value=ec2)  # type: ignore[method-assign]

    host = RemoteHost(
        id="i-gone",
        ip="1.2.3.4",
        ssh_user="ubuntu",
        ssh_key_path="/tmp/k",
        instance_type="t3.nano",
        lifecycle="spot",
        region="ap-south-1",
    )
    # Must not raise
    p.terminate(host)
    ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-gone"])


def test_terminate_swallows_unknown_errors(cfg):
    """The runner's finally-block relies on terminate() never re-raising."""
    ec2 = _fake_ec2_client()
    ec2.terminate_instances.side_effect = RuntimeError("some other AWS error")
    p = AWSProvider(cfg)
    p._client = MagicMock(return_value=ec2)  # type: ignore[method-assign]

    host = RemoteHost(
        id="i-still-there",
        ip="1.2.3.4",
        ssh_user="ubuntu",
        ssh_key_path="/tmp/k",
        instance_type="t3.nano",
        lifecycle="spot",
        region="ap-south-1",
    )
    p.terminate(host)  # must not raise


def test_wait_for_ssh_returns_on_open_port():
    """First successful create_connection returns cleanly."""
    with patch("acestor.remote.providers.aws.socket") as mock_socket:
        mock_socket.create_connection.return_value.__enter__ = lambda s: s
        mock_socket.create_connection.return_value.__exit__ = lambda s, t, v, tb: None
        AWSProvider._wait_for_ssh("1.2.3.4", timeout=2, interval=1)
        mock_socket.create_connection.assert_called_with(("1.2.3.4", 22), timeout=5)


def test_wait_for_ssh_times_out():
    with patch("acestor.remote.providers.aws.socket") as mock_socket:
        mock_socket.create_connection.side_effect = OSError("refused")
        with pytest.raises(TimeoutError, match="not reachable"):
            AWSProvider._wait_for_ssh("127.0.0.1", timeout=1, interval=1)
