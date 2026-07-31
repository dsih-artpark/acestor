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


def _resolve_provider(name: str) -> CloudProvider:
    """Look up a provider by name.

    Slice 1 ships only the mock. Real providers (aws, gcp) land in later
    slices and just plug in here.
    """
    if name == "mock":
        from acestor.remote.providers.mock import MockProvider

        return MockProvider()
    if name == "aws":
        raise NotImplementedError(
            "aws provider not implemented yet — coming in slice 2. "
            "Use --remote-provider mock to exercise the CLI + ledger."
        )
    raise ValueError(
        f"Unknown --remote-provider {name!r}. Available: mock (aws lands next)."
    )


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
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)

    try:
        provider = _resolve_provider(args.remote_provider)
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
