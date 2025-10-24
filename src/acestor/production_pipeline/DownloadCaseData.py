# -*- coding: utf-8 -*-
"""
Created on Fri Mar 14 11:35:14 2025

@author: TarunK
"""

import logging
import os
import sys
import boto3

from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv(override=True)

logger = logging.getLogger("download_case_data")


# %% Define the functions
def download_LL_data(*, folder_path, Bucket, Prefix):
    """
    Download the data into a folder from S3 bucket.

    Parameters
    ----------
    folder_path : str or Path
        The path to the destination folder.
    Bucket : str
        The bucket from which we want to download a dataset.
    Prefix : str
        The dataset located in S3 that we are interested to download.

    Returns
    -------
    None
    """
    folder_path = Path(folder_path)
    folder_path.mkdir(parents=True, exist_ok=True)  # Ensures folder exists

    try:
        client = boto3.client("s3", aws_access_key_id=os.getenv("AWS_ACCESS_KEY"), aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"))
        objects = client.list_objects_v2(Bucket=Bucket, Prefix=f"{Prefix}")

        Keys = [obj["Key"] for obj in objects.get("Contents", []) if obj["Key"].endswith(".csv")]

        if not Keys:
            return f"No files found in {Prefix}."

        for Key in Keys:
            Filename = folder_path / Path(Key).name

            file_size = client.head_object(Bucket=Bucket, Key=Key)["ContentLength"]
            size_MB = file_size / (1024**2)
            with tqdm(
                total=file_size,
                desc=f"Downloading {Path(Key).name} [{size_MB:.2f} MB]",
                unit="B",
                unit_scale=True,
                leave=True,
                bar_format="{l_bar}{bar}|{rate_fmt}",
            ) as pbar:

                def progress_callback(bytes_transferred):
                    pbar.update(bytes_transferred)

                client.download_file(Bucket=Bucket, Key=Key, Filename=Filename, Callback=progress_callback)
            if (len(Keys) == 1) or (Key == Keys[-1]):
                pbar.write("")
            sys.stdout.flush()
            pbar.write(f"Download path: {Filename!s}")
        logger.info(f"{Prefix}: Download completed successfully!\n" + "--" * 40)
        sys.stdout.flush()
    except Exception as e:
        logger.error(f"Error: {e}. Download did not complete successfully!")
        sys.stdout.flush()


def download_all_linelist_data(root_dir):
    # %% Download data: Example
    # Download KA linelist data
    logger.info("Downloading Karnataka linelist")
    download_LL_data(
        folder_path=root_dir / "datasets/raw_linelist_data/KA_linelist",
        Bucket="dsih-artpark-03-standardised-data",
        Prefix="EP0005DS0014-KA_Dengue_LL",
    )

    # Download KA IHIP linelist data
    logger.info("Downloading IHIP linelist")
    download_LL_data(
        folder_path=root_dir / "datasets/raw_linelist_data/IHIP_linelist",
        Bucket="dsih-artpark-03-standardised-data",
        Prefix="EP0005DS0067-KA_IHIP_Dengue_LL",
    )


if __name__ == "__main__":
    # Create logs directory if it doesn't exist
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Configure logging with both file and console output
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(f"logs/download_case_data_{date_str}.log"), logging.StreamHandler(sys.stdout)],
    )

    logger = logging.getLogger(__name__)
    logger.info("Starting case data download process")

    download_all_linelist_data(root_dir=".")

    logger.info("Case data download process completed")
