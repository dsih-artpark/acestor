"""CLI for the remote runner.

Usage::

    python -m acestor.remote \\
      --pipeline pipelines.dengue.pipeline:build_pipeline \\
      --config configs/ka_district.yaml \\
      --run-id my-run \\
      --remote-lifecycle spot \\
      --remote-instance c7i.2xlarge \\
      --remote-region ap-south-1

Mirrors the flag surface of ``python -m acestor.run`` so the same command
line works locally or remotely — only the ``--remote-*`` knobs differ.
"""

from __future__ import annotations

import argparse
import logging
import sys

from acestor.remote.providers.base import CloudProvider
from acestor.remote.runner import (
    RemoteRunOptions,
    generate_run_id,
    run_remote,
)

log = logging.getLogger("acestor.remote")


def _resolve_provider(name: str, args: argparse.Namespace) -> CloudProvider:
    """Look up a provider by name.

    Providers requiring per-cloud config (aws key pair, security group, ...)
    read it out of the CLI args here. Adding gcp later means one more branch.
    """
    if name == "mock":
        from acestor.remote.providers.mock import MockProvider

        return MockProvider()
    if name == "aws":
        from acestor.remote.providers.aws import AWSProvider, AWSProviderConfig

        if not args.aws_key_name or not args.aws_key_path:
            raise ValueError(
                "aws provider: --aws-key-name and --aws-key-path are required "
                "(existing EC2 key pair + local private key)."
            )
        cfg = AWSProviderConfig(
            key_name=args.aws_key_name,
            key_path=args.aws_key_path,
            security_group_ids=(
                [s.strip() for s in args.aws_security_groups.split(",") if s.strip()]
                if args.aws_security_groups
                else []
            ),
            subnet_id=args.aws_subnet or "",
            ami=args.aws_ami or "",
            iam_instance_profile=args.aws_instance_profile or "",
            profile=args.aws_profile or "",
            root_volume_gb=int(args.aws_root_volume_gb),
        )
        return AWSProvider(cfg)
    raise ValueError(f"Unknown --remote-provider {name!r}. Available: aws, mock.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m acestor.remote",
        description="Run an acestor pipeline on a cloud instance and stream "
        "artifacts back.",
    )

    # ---- Pipeline-level flags — mirror acestor.run ---------------------
    p.add_argument(
        "--pipeline",
        required=True,
        help="Pipeline builder 'module.path:callable_name' (same as acestor.run).",
    )
    p.add_argument(
        "--config",
        required=True,
        help="Path to YAML config (same as acestor.run).",
    )
    p.add_argument(
        "--run-id",
        default=None,
        help="Run identifier. Auto-generated if omitted.",
    )
    p.add_argument(
        "--set",
        dest="overrides",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        help="Config override, forwarded to the remote acestor.run invocation. "
        "Repeatable.",
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="Forwarded to remote acestor.run — wipe the run-id's artifact "
        "subtree before executing.",
    )

    # ---- Remote-runner-specific flags ---------------------------------
    p.add_argument(
        "--remote-provider",
        default="aws",
        help="Cloud backend (aws | mock). Default: aws. Additional providers "
        "(gcp, ...) plug in behind the CloudProvider interface.",
    )
    p.add_argument(
        "--remote-lifecycle",
        choices=("spot", "on-demand"),
        default="spot",
        help="Spot (cheap, may be reclaimed) or on-demand (stable). Default: spot.",
    )
    p.add_argument(
        "--remote-instance",
        default="c7i.2xlarge",
        help="Instance type. Default: c7i.2xlarge — 8 vCPU / 16 GB, "
        "MacBook-comparable for the training path.",
    )
    p.add_argument(
        "--remote-region",
        default="ap-south-1",
        help="Cloud region. Default: ap-south-1 (Mumbai).",
    )
    p.add_argument(
        "--ledger",
        default=None,
        help="Override the ledger path (default: ~/.acestor/remote_runs.jsonl).",
    )
    p.add_argument(
        "--uv-extras",
        default="all",
        help='Remote uv sync extras — "all" (default), "none", or a specific '
        'extra name (e.g. "dengue").',
    )
    p.add_argument(
        "--skip-run",
        action="store_true",
        help="Provision + bootstrap + sync only, don't run the pipeline. "
        "Useful for testing the bootstrap phase without spending compute time.",
    )
    p.add_argument(
        "--keep-alive-on-failure",
        action="store_true",
        help="Leave the remote host RUNNING if the run fails, so you can ssh in "
        "and debug. WARNING: you are billed until you manually terminate.",
    )
    p.add_argument(
        "--forward-env",
        action="append",
        default=None,
        metavar="NAME",
        help="Local env var name to forward into the remote pipeline (repeatable). "
        "Values live only in the remote process — never written to disk. "
        "Defaults to DASHBOARD_* + CDSAPI_* — pass this flag one or more times "
        "to override the default set.",
    )
    p.add_argument(
        "--skip-input-sync",
        action="store_true",
        help="Skip the config-walked local dataset rsync. Use when every "
        "data source is dashboard/API-based and there's nothing local to push.",
    )

    # ---- AWS-provider knobs (only read when --remote-provider aws) -----
    aws = p.add_argument_group("AWS provider (--remote-provider aws)")
    aws.add_argument(
        "--aws-key-name",
        help="Name of an existing EC2 key pair in --remote-region.",
    )
    aws.add_argument(
        "--aws-key-path",
        help="Local path to the matching private key (e.g. ~/.ssh/acestor.pem).",
    )
    aws.add_argument(
        "--aws-security-groups",
        default="",
        help="Comma-separated security group IDs (must allow SSH from your IP).",
    )
    aws.add_argument(
        "--aws-subnet",
        default="",
        help="Subnet ID. Omit to use default VPC subnet (needs auto-assign public IP).",
    )
    aws.add_argument(
        "--aws-ami",
        default="",
        help="AMI ID. Omit to auto-resolve latest Ubuntu 24.04 LTS via SSM.",
    )
    aws.add_argument(
        "--aws-instance-profile",
        default="",
        help="Optional IAM instance profile to attach (for S3 access etc.).",
    )
    aws.add_argument(
        "--aws-profile",
        default="",
        help="Optional boto3 profile name (defaults to standard credential chain).",
    )
    aws.add_argument(
        "--aws-root-volume-gb",
        type=int,
        default=20,
        help="Root EBS volume size in GB (default: 20). Bump if uv sync fills "
        "the disk on large --uv-extras choices.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)

    try:
        provider = _resolve_provider(args.remote_provider, args)
    except (ValueError, NotImplementedError) as exc:
        log.error("%s", exc)
        return 2

    run_id = args.run_id or generate_run_id()

    from pathlib import Path

    ledger_path = Path(args.ledger) if args.ledger else None
    opts = RemoteRunOptions(
        pipeline=args.pipeline,
        config=args.config,
        run_id=run_id,
        provider=provider,
        instance_type=args.remote_instance,
        lifecycle=args.remote_lifecycle,
        region=args.remote_region,
        overrides=list(args.overrides),
        clean=bool(args.clean),
        ledger_path=(
            ledger_path
            if ledger_path is not None
            else RemoteRunOptions.__dataclass_fields__["ledger_path"].default
        ),
        uv_extras=args.uv_extras,
        skip_run=bool(args.skip_run),
        keep_alive_on_failure=bool(args.keep_alive_on_failure),
        forward_env=(
            tuple(args.forward_env)
            if args.forward_env is not None
            else RemoteRunOptions.__dataclass_fields__["forward_env"].default
        ),
        skip_input_sync=bool(args.skip_input_sync),
    )

    outcome = run_remote(opts)

    log.info(
        "remote runner: run_id=%s exit_status=%s wall=%.1fs",
        run_id,
        outcome.exit_status,
        outcome.wall_seconds,
    )
    if outcome.error:
        log.error("remote runner: %s", outcome.error)

    return 0 if outcome.exit_status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
