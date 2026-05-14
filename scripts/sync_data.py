"""
Data sync — upload/download pipeline data to/from S3
=====================================================
Supports two backends — boto3 (default, no CLI needed) and awscli.
Both use the same credential chain: ~/.aws/credentials, environment
variables, IAM role, or SSO — nothing is hardcoded.

SETUP (one-time, per machine)
------------------------------
1. Configure AWS credentials:
       aws configure          # if you have the CLI
   Or set environment variables:
       export AWS_ACCESS_KEY_ID=...
       export AWS_SECRET_ACCESS_KEY=...
       export AWS_DEFAULT_REGION=ap-south-1

2. Set the S3 bucket (add to ~/.bashrc or ~/.zshrc):
       export ACESTOR_S3_BUCKET=your-bucket-name

USAGE
-----
Download all data (what a new teammate runs after cloning):

    python scripts/sync_data.py download

Upload all data (run after adding new case files or GeoJSONs):

    python scripts/sync_data.py upload

Target a specific dataset only:

    python scripts/sync_data.py download --only cases
    python scripts/sync_data.py download --only geojsons
    python scripts/sync_data.py download --only weather

Use the AWS CLI backend instead of boto3:

    python scripts/sync_data.py download --backend awscli

Dry-run (shows what would be transferred without doing anything):

    python scripts/sync_data.py download --dry-run
    python scripts/sync_data.py upload --dry-run

Override the bucket for a single run:

    python scripts/sync_data.py download --bucket my-other-bucket

DATA LAYOUT
-----------
Local                               S3 prefix
──────────────────────────────────  ──────────────────────────────────────────
ap_datasets/raw_case/               <bucket>/ap/raw_case/
ap_datasets/geojsons/geojsons_AP/   <bucket>/ap/geojsons/geojsons_AP/
ap_datasets/weather/                <bucket>/ap/weather/
prepared_data/district/             <bucket>/ap/prepared_data/district/
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

DATASETS = {
    "cases": {
        "local": REPO_ROOT / "ap_datasets" / "raw_case",
        "s3_prefix": "ap/raw_case",
        "description": "Raw IHIP case files (.xlsx / .csv)",
    },
    "geojsons": {
        "local": REPO_ROOT / "ap_datasets" / "geojsons" / "geojsons_AP",
        "s3_prefix": "ap/geojsons/geojsons_AP",
        "description": "AP district GeoJSON boundary files",
    },
    "weather": {
        "local": REPO_ROOT / "ap_datasets" / "weather",
        "s3_prefix": "ap/weather",
        "description": "Raw weather CSVs by month (ap_datasets/weather/)",
    },
    "prepared": {
        "local": REPO_ROOT / "prepared_data" / "district",
        "s3_prefix": "ap/prepared_data/district",
        "description": "Prepared weather + cases CSVs (prepared_data/district/)",
    },
}


def get_bucket(args_bucket: str | None) -> str:
    bucket = args_bucket or os.environ.get("ACESTOR_S3_BUCKET")
    if not bucket:
        print(
            "Error: S3 bucket not set.\n"
            "  Set the environment variable:  export ACESTOR_S3_BUCKET=your-bucket-name\n"
            "  Or pass it explicitly:         --bucket your-bucket-name"
        )
        sys.exit(1)
    return bucket


# ---------------------------------------------------------------------------
# boto3 backend — no AWS CLI required
# ---------------------------------------------------------------------------


def _boto_sync_download(
    bucket: str, s3_prefix: str, local_dir: Path, dry_run: bool
) -> int:
    """Download all objects under s3_prefix into local_dir, skipping unchanged files."""
    import boto3

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    transferred = skipped = 0

    for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            relative = key[len(s3_prefix.rstrip("/") + "/") :]
            if not relative or relative.endswith("/"):
                continue

            local_path = local_dir / relative
            # Skip if local file exists with the same size
            if local_path.exists() and local_path.stat().st_size == obj["Size"]:
                skipped += 1
                continue

            if dry_run:
                print(f"  [dry-run] would download  {key}")
                transferred += 1
                continue

            local_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"  download  {key}")
            s3.download_file(bucket, key, str(local_path))
            transferred += 1

    print(f"  {transferred} transferred, {skipped} already up-to-date")
    return 0


def _boto_sync_upload(
    bucket: str, s3_prefix: str, local_dir: Path, dry_run: bool
) -> int:
    """Upload all files from local_dir to s3_prefix, skipping unchanged files."""
    import boto3

    s3 = boto3.client("s3")

    # Build index of existing S3 objects for fast size comparison
    paginator = s3.get_paginator("list_objects_v2")
    existing: dict[str, int] = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            existing[obj["Key"]] = obj["Size"]

    transferred = skipped = 0
    for local_path in sorted(local_dir.rglob("*")):
        if not local_path.is_file():
            continue
        relative = local_path.relative_to(local_dir)
        key = f"{s3_prefix.rstrip('/')}/{relative}"
        # Skip if S3 object exists with the same size
        if existing.get(key) == local_path.stat().st_size:
            skipped += 1
            continue

        if dry_run:
            print(f"  [dry-run] would upload  {relative}")
            transferred += 1
            continue

        print(f"  upload  {relative}")
        s3.upload_file(str(local_path), bucket, key)
        transferred += 1

    print(f"  {transferred} transferred, {skipped} already up-to-date")
    return 0


# ---------------------------------------------------------------------------
# AWS CLI backend
# ---------------------------------------------------------------------------


def _awscli_sync(source: str, destination: str, dry_run: bool) -> int:
    cmd = ["aws", "s3", "sync", source, destination, "--no-progress"]
    if dry_run:
        cmd.append("--dryrun")
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_download(args):
    bucket = get_bucket(args.bucket)
    targets = [args.only] if args.only else list(DATASETS.keys())
    errors = []

    for name in targets:
        ds = DATASETS[name]
        local_dir = ds["local"]
        s3_prefix = ds["s3_prefix"]
        local_dir.mkdir(parents=True, exist_ok=True)

        label = "[dry-run] " if args.dry_run else ""
        print(f"\n{label}Syncing {ds['description']}")
        print(f"  s3://{bucket}/{s3_prefix}/  →  {local_dir}")

        if args.backend == "boto3":
            rc = _boto_sync_download(bucket, s3_prefix, local_dir, args.dry_run)
        else:
            rc = _awscli_sync(
                f"s3://{bucket}/{s3_prefix}/", str(local_dir) + "/", args.dry_run
            )

        if rc != 0:
            errors.append(name)

    _print_result(errors, args.dry_run, "download")


def cmd_upload(args):
    bucket = get_bucket(args.bucket)
    targets = [args.only] if args.only else list(DATASETS.keys())
    errors = []

    for name in targets:
        ds = DATASETS[name]
        local_dir = ds["local"]
        s3_prefix = ds["s3_prefix"]

        if not local_dir.exists():
            print(f"\n  skip  {name}  ({local_dir} does not exist locally)")
            continue

        label = "[dry-run] " if args.dry_run else ""
        print(f"\n{label}Syncing {ds['description']}")
        print(f"  {local_dir}  →  s3://{bucket}/{s3_prefix}/")

        if args.backend == "boto3":
            rc = _boto_sync_upload(bucket, s3_prefix, local_dir, args.dry_run)
        else:
            rc = _awscli_sync(
                str(local_dir) + "/", f"s3://{bucket}/{s3_prefix}/", args.dry_run
            )

        if rc != 0:
            errors.append(name)

    _print_result(errors, args.dry_run, "upload")


def _print_result(errors: list, dry_run: bool, direction: str):
    if errors:
        print(f"\nFinished with errors in: {', '.join(errors)}")
        sys.exit(1)
    elif dry_run:
        print(f"\nDry run complete — no files were {direction}ed.")
        print("Remove --dry-run to actually transfer files.")
    else:
        print(f"\nAll datasets {direction}ed successfully.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Sync pipeline data between local machine and S3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
datasets:
  cases      Raw IHIP case files (ap_datasets/raw_case/)
  geojsons   AP district GeoJSON boundaries (ap_datasets/geojsons/geojsons_AP/)
  weather    Raw weather CSVs by month (ap_datasets/weather/)
  prepared   Prepared cases + weather CSVs (prepared_data/district/)

examples:
  # Download everything (new teammate setup)
  python scripts/sync_data.py download

  # Upload after adding new case files
  python scripts/sync_data.py upload

  # Only download case files
  python scripts/sync_data.py download --only cases

  # Preview what would be uploaded without transferring
  python scripts/sync_data.py upload --dry-run

  # Use AWS CLI instead of boto3
  python scripts/sync_data.py download --backend awscli

  # Override bucket for this run
  python scripts/sync_data.py download --bucket my-other-bucket
        """,
    )

    sub = parser.add_subparsers(dest="command", required=True)

    for cmd_name, cmd_fn, cmd_help in [
        ("download", cmd_download, "Pull data from S3 to local machine"),
        ("upload", cmd_upload, "Push local data to S3"),
    ]:
        p = sub.add_parser(cmd_name, help=cmd_help)
        p.add_argument(
            "--bucket",
            metavar="NAME",
            help="S3 bucket name (overrides ACESTOR_S3_BUCKET env var)",
        )
        p.add_argument(
            "--only",
            choices=list(DATASETS.keys()),
            metavar="DATASET",
            help="Sync only one dataset: cases | geojsons | weather | prepared",
        )
        p.add_argument(
            "--backend",
            choices=["boto3", "awscli"],
            default="boto3",
            help="Transfer backend: boto3 (default, no CLI needed) or awscli",
        )
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be transferred without actually doing it",
        )
        p.set_defaults(func=cmd_fn)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
