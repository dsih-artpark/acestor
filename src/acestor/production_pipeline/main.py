"""Main pipeline orchestrator for dengue disease modeling."""

import argparse
import logging
import os
import sys
import yaml
import time
from datetime import datetime, timedelta
from pathlib import Path

from DownloadCaseData import download_all_linelist_data
from ParseCaseData import parse_linelist_to_no_of_cases, aggregate_and_sample_case_data
from DownloadWeatherData import download_weather_data
from ParseAndExtractWeatherData import parse_and_extract_weather_data
from ParseS3WeatherData import parse_s3_weather_data
from IdentifyCutoffDates import identify_cutoff_dates
from run_district import run_district_predictions


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Production pipeline for dengue disease modeling")

    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default="config/dengue_pipeline.yaml",
        help="Path to the configuration file (default: config/dengue_pipeline.yaml)",
    )

    parser.add_argument(
        "-dl", "--download-linelist", action="store_true", default=False, help="Download linelist data from S3 (default: False)"
    )

    parser.add_argument("-pl", "--process-linelist", action="store_true", default=False, help="Process linelist data (default: False)")

    parser.add_argument(
        "-dw",
        "--download-weather-data-from-cds-api",
        action="store_true",
        default=False,
        help="Download weather data from CDS API(default: False)",
    )

    parser.add_argument(
        "-ws", "--use-weather-data-from-s3", action="store_true", default=False, help="Use weather data from S3(default: False)"
    )

    return parser.parse_args()


def load_config(config_path):
    """Load pipeline configuration."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config


def setup_logging(root_dir):
    """Set up logging configuration for the pipeline."""
    # Create logs directory if it doesn't exist
    logs_dir = Path(root_dir) / "logs"
    logs_dir.mkdir(exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Configure logging with both file and console output
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(logs_dir / f"production_pipeline_{date_str}.log"), logging.StreamHandler(sys.stdout)],
    )

    return logging.getLogger(__name__)


def main():
    """Main pipeline execution."""

    # Parse arguments
    args = parse_args()

    # Load configuration
    config = load_config(args.config)
    root_dir = Path(config["root_dir"])
    debug = config.get("debug", False)  # Default to False if not specified
    region_name = config.get("region_name")  # Default to Karnataka
    geojson_folder_path = Path(config.get("geojson_folder")) / Path(region_name)
    weather_data_path = Path(config.get("weather_data_path"))
    raw_linelist_path = config.get("raw_linelist_path")
    granularity = config.get("granularity")
    parse_district = granularity == "district" or granularity == "subdistrict"
    parse_subdistrict = granularity == "subdistrict"

    # Setup logging
    logger = setup_logging(root_dir)
    logger.info("Starting production pipeline")
    logger.info(f"Using configuration file: {args.config}")
    logger.info(f"Using root directory: {root_dir}")
    logger.info(f"Region: {region_name}")
    logger.info(f"Download linelist: {args.download_linelist}")
    logger.info(f"Process linelist: {args.process_linelist}")

    # Timer Start

    start_time = time.time()

    # make necessary folders
    os.makedirs("results", exist_ok=True)

    if debug:
        logger.info("DEBUG MODE ENABLED: Processing only last year of data")

    # Step 1: Download ARTPARK linelist data from S3.
    if args.download_linelist:
        logger.info("Downloading case data")
        download_all_linelist_data(root_dir=root_dir)

    # Step 2: Parsing Line list data
    if args.process_linelist:
        logger.info("Parsing linelist to no of cases")
        parse_linelist_to_no_of_cases(
            root_dir=root_dir,
            raw_linelist_path=raw_linelist_path,
            parse_district_level=parse_district,
            parse_subdistrict_level=parse_subdistrict,
        )

    # Step 3: Parse case data
    logger.info("Parsing case data")
    sampling_day, case_start_date, case_end_date = aggregate_and_sample_case_data(
        root_dir=root_dir,
        debug=debug,
        parse_district_level=parse_district,
        parse_subdistrict_level=parse_subdistrict,
    )

    logging.info(f"sampling day: {sampling_day}")
    logging.info(f"Case start date: {case_start_date}, Case end date: {case_end_date}")

    # Step 3: Download weather data
    if args.download_weather_data_from_cds_api:
        # if False:
        # logger.info("Downloading weather data")
        # download_weather_data(root_dir, case_start_date, case_end_date, region_name, geojson_folder_path)

        # Step 4: Parse weather data
        logger.info("Parsing weather data")
        parse_and_extract_weather_data(
            root_dir,
            geojson_folder_path,
            case_data_start_date=case_start_date,
            case_data_end_date=case_end_date,
            sampling_day=sampling_day,
            parse_district_level=parse_district,
            parse_subdistrict_level=parse_subdistrict,
        )

    # if args.use_weather_data_from_s3:
    if False:
        case_start_date = datetime.strptime(case_start_date, "%Y-%m-%d") - timedelta(days=100)
        case_end_date = datetime.strptime(case_end_date, "%Y-%m-%d") + timedelta(days=100)

        logging.info(f"After shift Case start date: {case_start_date}, Case end date: {case_end_date}")

        logger.info("Using weather data from S3")
        parse_s3_weather_data(
            weather_data_path=weather_data_path,
            geojson_folder_path=geojson_folder_path,
            start_date=case_start_date,
            end_date=case_end_date,
            sampling_day=sampling_day,
        )

    # Step 5: Identify Cutoff Dates
    logger.info("Identify cutoff dates")
    cutoff, pred_upto, cutoff_case, cutoff_weather, prediction_dates = identify_cutoff_dates(root_dir=root_dir)

    logging.info(f"cutoff date: {cutoff}")
    logging.info(f"pred_upto date: {pred_upto}")
    logging.info(f"cutoff_case: {cutoff_case}")
    logging.info(f"cutoff_weather: {cutoff_weather}")
    logging.info(f"prediction_dates: {prediction_dates}")

    # Step 6: Run District Predictions
    if parse_district:
        logger.info("Run district predictions")
        run_district_predictions(root_dir, pred_upto, cutoff_case, sampling_day)

    logger.info("Production pipeline completed")

    # End time
    time_taken = time.time() - start_time
    logger.info(f"Time taken for pipeline: {time_taken}")


if __name__ == "__main__":
    main()
