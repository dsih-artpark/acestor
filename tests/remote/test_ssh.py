"""SSH + rsync command shape — subprocess mocked, no real network."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from acestor.remote.providers.base import RemoteHost
from acestor.remote.ssh import rsync_down, rsync_up, ssh_exec


@pytest.fixture
def host() -> RemoteHost:
    return RemoteHost(
        id="i-abc",
        ip="1.2.3.4",
        ssh_user="ubuntu",
        ssh_key_path="/tmp/k.pem",
        instance_type="t3.nano",
        lifecycle="on-demand",
        region="ap-south-1",
    )


def test_ssh_exec_streaming_wires_stdin_stdout(host):
    fake_proc = MagicMock()
    fake_proc.stdout = iter(["hello\n", "world\n"])
    fake_proc.wait.return_value = None
    fake_proc.returncode = 0

    with patch("acestor.remote.ssh.subprocess.Popen", return_value=fake_proc) as popen:
        rc = ssh_exec(host, "echo hi", stream=True)

    assert rc == 0
    argv = popen.call_args.args[0]
    assert argv[0] == "ssh"
    assert argv[1] == "-i" and argv[2] == "/tmp/k.pem"
    assert "-o" in argv and "StrictHostKeyChecking=no" in argv
    assert argv[-2] == "ubuntu@1.2.3.4"
    assert argv[-1] == "echo hi"


def test_ssh_exec_non_streaming_returns_captured_rc(host):
    fake_proc = MagicMock(returncode=7, stdout="", stderr="boom")
    with patch("acestor.remote.ssh.subprocess.run", return_value=fake_proc):
        rc = ssh_exec(host, "false", stream=False)
    assert rc == 7


def test_rsync_up_shape_and_ssh_e_option(host):
    with patch("acestor.remote.ssh.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rsync_up(
            host,
            local_dir="/tmp/repo",
            remote_dir="/home/ubuntu/acestor",
            exclude=[".venv", "artifacts"],
        )

    argv = run.call_args.args[0]
    assert argv[0] == "rsync"
    assert "-a" in argv
    assert "--delete" in argv
    e_idx = argv.index("-e")
    assert "ssh -i /tmp/k.pem" in argv[e_idx + 1]
    assert "--exclude=.venv" in argv
    assert "--exclude=artifacts" in argv
    # Trailing slash on source so contents (not the dir itself) are pushed
    assert argv[-2] == "/tmp/repo/"
    assert argv[-1] == "ubuntu@1.2.3.4:/home/ubuntu/acestor/"


def test_rsync_up_raises_on_nonzero_exit(host):
    with patch("acestor.remote.ssh.subprocess.run") as run:
        run.return_value = MagicMock(returncode=23, stdout="", stderr="perm denied")
        with pytest.raises(RuntimeError, match="rsync_up failed"):
            rsync_up(host, "/tmp/x", "/tmp/y")


def test_rsync_down_no_delete_ever(host, tmp_path):
    """Artifact sync must never wipe local state — --delete must be absent."""
    with patch("acestor.remote.ssh.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rsync_down(host, remote_dir="/remote/artifacts/r1", local_dir=tmp_path / "art")

    argv = run.call_args.args[0]
    assert "--delete" not in argv
    assert argv[-2] == "ubuntu@1.2.3.4:/remote/artifacts/r1/"
    assert argv[-1] == str(tmp_path / "art") + "/"


def test_rsync_down_creates_local_dir_before_transfer(host, tmp_path):
    target = tmp_path / "nested" / "artifacts" / "r1"
    with patch("acestor.remote.ssh.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rsync_down(host, remote_dir="/x", local_dir=target)
    assert target.exists()
