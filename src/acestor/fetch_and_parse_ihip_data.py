#!/usr/bin/env python3
"""
Fetch and parse IHIP data from S3.

This script performs the following operations:
1. Downloads IHIP linelist data from S3 bucket (specified in dengue_pipeline.yaml)
2. Converts all XLSX files to CSV format
3. Merges all CSV files into a single linelist.csv file
4. Creates aggregated cases.csv with positive test counts by date and district

Output files:
- data/ihip/linelist.csv: Individual test records
- data/ihip/cases.csv: Aggregated positive cases by date and district
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import boto3
import pandas as pd
import yaml
from dotenv import load_dotenv
from tqdm import tqdm

# Load environment variables
load_dotenv(override=True)

# Configure logging
logger = logging.getLogger("fetch_and_parse_ihip_data")


def load_config(config_path="config/dengue_pipeline.yaml"):
    """
    Load configuration from YAML file.

    Parameters
    ----------
    config_path : str
        Path to the configuration YAML file.

    Returns
    -------
    dict
        Configuration dictionary.
    """
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config


def parse_s3_path(s3_path):
    """
    Parse S3 path into bucket and prefix.

    Parameters
    ----------
    s3_path : str
        S3 path in format s3://bucket-name/prefix/

    Returns
    -------
    tuple
        (bucket_name, prefix)
    """
    if not s3_path.startswith("s3://"):
        raise ValueError(f"Invalid S3 path: {s3_path}. Must start with 's3://'")

    path_parts = s3_path[5:].split("/", 1)
    bucket = path_parts[0]
    prefix = path_parts[1] if len(path_parts) > 1 else ""

    return bucket, prefix


def fetch_ihip_data_from_s3(s3_location, destination_folder):
    """
    Download IHIP data from S3 to local folder.

    Parameters
    ----------
    s3_location : str
        S3 path (e.g., s3://bucket-name/prefix/)
    destination_folder : str or Path
        Local folder path to store downloaded files.

    Returns
    -------
    None
    """
    destination_folder = Path(destination_folder)
    destination_folder.mkdir(parents=True, exist_ok=True)

    logger.info(f"Fetching IHIP data from {s3_location}")
    logger.info(f"Destination folder: {destination_folder}")

    try:
        # Parse S3 path
        bucket, prefix = parse_s3_path(s3_location)
        logger.info(f"Bucket: {bucket}, Prefix: {prefix}")

        # Create S3 client
        aws_access_key = os.getenv("AWS_ACCESS_KEY")
        aws_secret_key = os.getenv("AWS_SECRET_KEY")

        if not aws_access_key or not aws_secret_key:
            raise ValueError(
                "AWS credentials not found in environment variables. Please set AWS_ACCESS_KEY and AWS_SECRET_KEY in .env file"
            )

        client = boto3.client("s3", aws_access_key_id=aws_access_key, aws_secret_access_key=aws_secret_key)

        # List all objects in the S3 folder
        logger.info("Listing objects in S3 bucket...")
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)

        if "Contents" not in response:
            logger.warning(f"No files found in {s3_location}")
            return

        # Get all file keys (excluding folders)
        keys = [obj["Key"] for obj in response["Contents"] if not obj["Key"].endswith("/")]

        if not keys:
            logger.warning(f"No files found in {s3_location}")
            return

        logger.info(f"Found {len(keys)} files to download")

        # Download each file
        for key in keys:
            filename = Path(key).name
            local_path = destination_folder / filename

            # Get file size for progress bar
            file_size = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
            size_mb = file_size / (1024**2)

            logger.info(f"Downloading: {filename} ({size_mb:.2f} MB)")

            with tqdm(
                total=file_size,
                desc=f"Downloading {filename}",
                unit="B",
                unit_scale=True,
                leave=True,
                bar_format="{l_bar}{bar}|{rate_fmt}",
            ) as pbar:

                def progress_callback(bytes_transferred):
                    pbar.update(bytes_transferred)

                client.download_file(Bucket=bucket, Key=key, Filename=str(local_path), Callback=progress_callback)

            sys.stdout.flush()
            logger.info(f"Downloaded to: {local_path}")

        logger.info(f"Download completed successfully! All files saved to {destination_folder}")

    except Exception as e:
        logger.error(f"Error during download: {e}")
        raise


def convert_xlsx_to_csv(source_folder):
    """
    Convert all XLSX files in the source folder to CSV format.

    Parameters
    ----------
    source_folder : str or Path
        Folder containing XLSX files.

    Returns
    -------
    list
        List of paths to converted CSV files.
    """
    source_folder = Path(source_folder)
    xlsx_files = list(source_folder.glob("*.xlsx"))

    if not xlsx_files:
        logger.warning(f"No XLSX files found in {source_folder}")
        return []

    logger.info(f"Found {len(xlsx_files)} XLSX files to convert")
    csv_files = []

    for xlsx_file in xlsx_files:
        try:
            logger.info(f"Converting {xlsx_file.name} to CSV...")

            # Read XLSX file
            df = pd.read_excel(xlsx_file)

            # Create CSV filename
            csv_file = xlsx_file.with_suffix(".csv")

            # Save as CSV
            df.to_csv(csv_file, index=False)
            csv_files.append(csv_file)

            logger.info(f"Converted: {csv_file.name} ({len(df)} rows)")

        except Exception as e:
            logger.error(f"Error converting {xlsx_file.name}: {e}")
            continue

    logger.info(f"Successfully converted {len(csv_files)} files to CSV")
    return csv_files


def merge_csv_files(source_folder, output_file):
    """
    Merge all CSV files in the source folder into a single CSV file.
    Only keeps specified columns in the output.

    Parameters
    ----------
    source_folder : str or Path
        Folder containing CSV files.
    output_file : str or Path
        Path to the output merged CSV file.

    Returns
    -------
    None
    """
    source_folder = Path(source_folder)
    output_file = Path(output_file)

    # Define columns to keep
    columns_to_keep = ["Sub District", "District", "State", "Date Of Onset", "Sample Collected Date", "Test Performed Date", "Test Result"]

    # Get all CSV files in the folder
    csv_files = list(source_folder.glob("*.csv"))

    if not csv_files:
        logger.warning(f"No CSV files found in {source_folder}")
        return

    logger.info(f"Found {len(csv_files)} CSV files to merge")

    try:
        # Read and concatenate all CSV files
        df_list = []
        for csv_file in csv_files:
            logger.info(f"Reading {csv_file.name}...")
            df = pd.read_csv(csv_file)

            # Check which columns are available
            available_cols = [col for col in columns_to_keep if col in df.columns]
            missing_cols = [col for col in columns_to_keep if col not in df.columns]

            if missing_cols:
                logger.warning(f"  Missing columns in {csv_file.name}: {missing_cols}")

            # Select only available columns
            if available_cols:
                df = df[available_cols]
                df_list.append(df)
                logger.info(f"  Rows: {len(df)}, Columns kept: {len(available_cols)}")
            else:
                logger.warning(f"  Skipping {csv_file.name} - no matching columns found")

        if not df_list:
            logger.error("No data to merge - no files had the required columns")
            return

        # Concatenate all dataframes
        merged_df = pd.concat(df_list, ignore_index=True)

        # Create 'date' column with priority logic:
        # Date Of Onset > Sample Collected Date > Test Performed Date
        logger.info("Creating 'date' column with priority logic...")
        merged_df["date"] = merged_df.get("Date Of Onset")
        if "Sample Collected Date" in merged_df.columns:
            merged_df["date"] = merged_df["date"].fillna(merged_df["Sample Collected Date"])
        if "Test Performed Date" in merged_df.columns:
            merged_df["date"] = merged_df["date"].fillna(merged_df["Test Performed Date"])

        # Convert date to YYYY-MM-DD format
        logger.info("Converting date to YYYY-MM-DD format...")
        merged_df["date"] = pd.to_datetime(merged_df["date"], errors="coerce")
        merged_df["date"] = merged_df["date"].dt.strftime("%Y-%m-%d")

        # Log how many dates could not be parsed
        null_dates = merged_df["date"].isna().sum()
        if null_dates > 0:
            logger.warning(f"Could not parse {null_dates} dates - they will be set to null")

        # Rename columns
        column_rename_map = {
            "Sub District": "subdistrict.name",
            "District": "district.name",
            "State": "state.name",
            "Test Result": "test_result",
        }
        merged_df = merged_df.rename(columns=column_rename_map)
        logger.info("Renamed columns to standardized format")

        # Drop the original date columns as we now have 'date'
        date_cols_to_drop = ["Date Of Onset", "Sample Collected Date", "Test Performed Date"]
        cols_to_drop = [col for col in date_cols_to_drop if col in merged_df.columns]
        if cols_to_drop:
            merged_df = merged_df.drop(columns=cols_to_drop)
            logger.info(f"Dropped original date columns: {cols_to_drop}")

        # Ensure output directory exists
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Save merged dataframe
        merged_df.to_csv(output_file, index=False)

        logger.info(f"Successfully merged {len(csv_files)} files into {output_file}")
        logger.info(f"Total rows in merged file: {len(merged_df)}")
        logger.info(f"Columns in output: {list(merged_df.columns)}")

    except Exception as e:
        logger.error(f"Error merging CSV files: {e}")
        raise


def create_cases_file(linelist_file, output_file, regionids_file="data/regionids.csv"):
    """
    Create aggregated cases file from linelist data.
    Groups by date and district, counting positive test results.
    Merges with regionids to add district.ID and state.ID.

    Parameters
    ----------
    linelist_file : str or Path
        Path to the linelist CSV file.
    output_file : str or Path
        Path to the output cases CSV file.
    regionids_file : str or Path
        Path to the regionids CSV file.

    Returns
    -------
    None
    """
    linelist_file = Path(linelist_file)
    output_file = Path(output_file)
    regionids_file = Path(regionids_file)

    logger.info(f"Reading linelist file: {linelist_file}")
    logger.info(f"Reading regionids file: {regionids_file}")

    try:
        # Read linelist data
        df = pd.read_csv(linelist_file)

        # Read regionids data
        regionids_df = pd.read_csv(regionids_file)

        # Normalize region names for matching (uppercase and strip whitespace)
        regionids_df["regionName_normalized"] = regionids_df["regionName"].str.upper().str.strip()

        # Filter for positive test results only
        df_positive = df[df["test_result"].str.upper() == "POSITIVE"].copy()
        logger.info(f"Found {len(df_positive)} positive test results out of {len(df)} total records")

        if len(df_positive) == 0:
            logger.warning("No positive test results found. Creating empty cases file.")
            # Create empty dataframe with expected columns
            cases_df = pd.DataFrame(columns=["date", "district.ID", "district.name", "state.ID", "state.name", "case"])
        else:
            # Group by date and district, count positive cases
            # Also keep state.name (taking first value per district since it should be the same)
            cases_df = (
                df_positive.groupby(["date", "district.name"])
                .agg(
                    {
                        "test_result": "count",  # Count positive cases
                        "state.name": "first",  # Keep state name
                    }
                )
                .reset_index()
            )

            # Rename test_result column to case
            cases_df = cases_df.rename(columns={"test_result": "case"})

            # Fix known spelling differences between IHIP data and regionids
            district_name_fixes = {
                "UTTAR KANNAD": "UTTARA KANNADA",
                "DAKSHIN KANNAD": "DAKSHINA KANNADA",
                "BAGALKOT": "BAGALKOTE",
                "CHIKBALLAPUR": "CHIKKABALLAPURA",
            }

            # Normalize district and state names for matching
            cases_df["district_name_normalized"] = cases_df["district.name"].str.upper().str.strip()

            # Apply spelling fixes to normalized column
            cases_df["district_name_normalized"] = cases_df["district_name_normalized"].replace(district_name_fixes)

            # Also apply corrections to the actual district.name column
            cases_df["district.name"] = cases_df["district_name_normalized"]
            logger.info(f"Applied {len(district_name_fixes)} district name corrections")

            cases_df["state_name_normalized"] = cases_df["state.name"].str.upper().str.strip()

            # Merge with district IDs
            district_lookup = regionids_df[regionids_df["regionID"].str.startswith("district_", na=False)].copy()
            district_lookup = district_lookup.rename(columns={"regionID": "district.ID", "parentID": "state.ID"})

            cases_df = cases_df.merge(
                district_lookup[["regionName_normalized", "district.ID", "state.ID"]],
                left_on="district_name_normalized",
                right_on="regionName_normalized",
                how="left",
            )

            # Check for unmatched districts
            unmatched_districts = cases_df[cases_df["district.ID"].isna()]["district.name"].unique()
            if len(unmatched_districts) > 0:
                logger.warning(f"Could not match {len(unmatched_districts)} districts to region IDs: {unmatched_districts[:10]}")

            # Merge with state names to verify state.ID (optional verification)
            state_lookup = regionids_df[regionids_df["regionID"].str.startswith("state_", na=False)].copy()
            state_lookup = state_lookup.rename(columns={"regionID": "state.ID_verify"})

            cases_df = cases_df.merge(
                state_lookup[["regionName_normalized", "state.ID_verify"]],
                left_on="state_name_normalized",
                right_on="regionName_normalized",
                how="left",
                suffixes=("", "_state"),
            )

            # Use verified state ID if district lookup failed
            cases_df["state.ID"] = cases_df["state.ID"].fillna(cases_df["state.ID_verify"])

            # Clean up temporary columns
            cases_df = cases_df.drop(
                columns=["district_name_normalized", "state_name_normalized", "regionName_normalized", "state.ID_verify"]
                + ([col for col in cases_df.columns if col.endswith("_state")])
            )

            # Reorder columns
            cases_df = cases_df[["date", "district.ID", "district.name", "state.ID", "state.name", "case"]]

        # Sort by date and district
        cases_df = cases_df.sort_values(["date", "district.name"]).reset_index(drop=True)

        # Save to file
        output_file.parent.mkdir(parents=True, exist_ok=True)
        cases_df.to_csv(output_file, index=False)

        logger.info(f"Successfully created cases file: {output_file}")
        logger.info(f"Total rows: {len(cases_df)}")
        logger.info(f"Date range: {cases_df['date'].min()} to {cases_df['date'].max()}")
        logger.info(f"Unique districts: {cases_df['district.name'].nunique()}")
        logger.info(f"Total positive cases: {cases_df['case'].sum()}")

    except Exception as e:
        logger.error(f"Error creating cases file: {e}")
        raise


def main():
    """Main function to fetch IHIP data."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Fetch and parse IHIP data from S3",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-o",
        "--output-file",
        type=str,
        default="datasets/cases_district_daily.csv",
        help="Output file path for aggregated cases data",
    )
    args = parser.parse_args()

    # Setup logging
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(f"logs/fetch_ihip_data_{date_str}.log"), logging.StreamHandler(sys.stdout)],
    )

    logger.info("Starting IHIP data fetch and processing")
    logger.info(f"Output file: {args.output_file}")

    try:
        # Load configuration
        config = load_config("config/dengue_pipeline.yaml")
        s3_location = config.get("ihip_s3_location")

        if not s3_location:
            raise ValueError("ihip_s3_location not found in dengue_pipeline.yaml")

        # Step 1: Fetch data from S3
        linelist_folder = Path("data/ihip/linelist")
        logger.info("=" * 60)
        logger.info("STEP 1: Fetching data from S3")
        logger.info("=" * 60)
        fetch_ihip_data_from_s3(s3_location, linelist_folder)

        # Step 2: Convert XLSX files to CSV
        logger.info("=" * 60)
        logger.info("STEP 2: Converting XLSX files to CSV")
        logger.info("=" * 60)
        convert_xlsx_to_csv(linelist_folder)

        # Step 3: Merge all CSV files
        logger.info("=" * 60)
        logger.info("STEP 3: Merging CSV files")
        logger.info("=" * 60)
        linelist_file = Path("data/ihip/linelist.csv")
        merge_csv_files(linelist_folder, linelist_file)

        # Step 4: Create aggregated cases file
        logger.info("=" * 60)
        logger.info("STEP 4: Creating aggregated cases file")
        logger.info("=" * 60)
        cases_file = Path(args.output_file)
        create_cases_file(linelist_file, cases_file)

        logger.info("=" * 60)
        logger.info("IHIP data fetch and processing completed successfully!")
        logger.info(f"Linelist file: {linelist_file}")
        logger.info(f"Cases file: {cases_file}")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"IHIP data processing failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
