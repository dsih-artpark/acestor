"""Main pipeline orchestrator for dengue disease modeling."""

import argparse
import logging
import os
import sys
import time
import yaml
from datetime import datetime, timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd
import xarray as xr

import utils

from DataIOWeatherData import download_weather_data, initialize_dataio_client, parse_dataio_weather_to_csv
from DownloadCaseData import download_all_linelist_data
from GenerateMap import generate_map
from IdentifyCutoffDates import identify_cutoff_dates
from ParseCaseData import aggregate_and_sample_case_data, parse_linelist_to_no_of_cases
from GenerateThresholds import generate_thresholds
from run_district_refactored import run_district_predictions
from GenerateReport import generate_report
from indra.emails import Report, Status


def valid_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a valid date: {s!r}")


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
        "-d",
        "--date",
        type=valid_date,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Date to run the predictions for (in YYYY-MM-DD format) - default is today",
    )

    parser.add_argument(
        "-dl", "--download-linelist", action="store_true", default=False, help="Download linelist data from S3 (default: False)"
    )

    parser.add_argument("-pl", "--process-linelist", action="store_true", default=False, help="Process linelist data (default: False)")

    # parser.add_argument(
    #     "-dw",
    #     "--download-weather-data-from-cds-api",
    #     action="store_true",
    #     default=False,
    #     help="Download weather data from CDS API(default: False)",
    # )

    parser.add_argument(
        "-ws",
        "--download-and-use-weather-data-from-s3",
        action="store_true",
        default=False,
        help="Use weather data from S3(default: False)",
    )

    parser.add_argument(
        "-rws",
        "--use-previously-downloaded-weather-data-from-s3",
        action="store_true",
        default=False,
        help="Use weather data from S3(default: False)",
    )

    parser.add_argument("-t", "--generate-thresholds", action="store_true", default=False, help="Generate thresholds(default: False)")

    parser.add_argument(
        "-m",
        "--model-train-and-predict",
        action="store_true",
        default=False,
        help="Train the model and generate predictions \
                        (default: False)",
    )

    parser.add_argument("-gm", "--generate-maps", action="store_true", default=False, help="Plot the results on a map")

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
    continuous_data_strict = config.get("continuous_data_strict", True)  # Default to True if not specified
    parse_district = granularity == "district" or granularity == "subdistrict"
    parse_subdistrict = granularity == "subdistrict"

    run_date = args.date

    # Setup logging

    logger = setup_logging(root_dir)
    logger.info("Starting production pipeline")
    logger.info(f"Using configuration file: {args.config}")
    logger.info(f"Using root directory: {root_dir}")
    logger.info(f"Region: {region_name}")
    logger.info(f"Download linelist: {args.download_linelist}")
    logger.info(f"Process linelist: {args.process_linelist}")
    logger.info(f"Run date: {run_date}")

    # Timer Start

    start_time = time.time()

    # Initialize email report if enabled
    report = None
    if config.get("enable_email_reports", False):
        email_recipients = config.get("email_recipients", [])
        if email_recipients:
            report = Report(job_name=f"Dengue Pipeline - {region_name}", email_recipients=email_recipients, run_date=str(run_date))
            logger.info(f"Email reporting enabled for {len(email_recipients)} recipients")
        else:
            logger.warning("Email reporting enabled but no recipients configured")

    # make necessary folders
    os.makedirs("results", exist_ok=True)

    if debug:
        logger.info("DEBUG MODE ENABLED: Processing only 3 years of data")

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
    sampling_day, case_start_date, case_end_date, can_generate_thresholds, can_run_predictions = aggregate_and_sample_case_data(
        root_dir=root_dir, run_date=run_date, granularity=granularity, debug=debug, continuous_data_strict=continuous_data_strict
    )

    if not (can_generate_thresholds or can_run_predictions):
        logger.critical("Cant generate thresholds or run predictions due to insufficient case data. Exiting...")
        if report:
            report.add_a_status_report("Data Processing", Status.ERROR, "Insufficient case data to generate thresholds or run predictions")
            report.send_email()
        exit(0)

    if report:
        report.add_a_status_report("Data Processing", Status.SUCCESS, f"Processed case data from {case_start_date} to {case_end_date}")

    logging.info(f"sampling day: {sampling_day}")
    logging.info(f"Case start date: {case_start_date}, Case end date: {case_end_date}")

    if args.download_and_use_weather_data_from_s3:
        # if False:
        case_start_date_dt = datetime.strptime(case_start_date, "%Y-%m-%d") - timedelta(days=100)
        case_end_date_dt = run_date

        logging.info(f"After shift Case start date: {case_start_date_dt}, Case end date: {case_end_date_dt}")

        logger.info("Downloading weather data from DataIO")

        # Initialize DataIO client (loads credentials from .env)
        client = initialize_dataio_client()

        # Get DataIO settings from environment
        dataset_name = os.getenv("DATAIO_DATASET_NAME", "era5_sfc")
        variables = os.getenv("DATAIO_WEATHER_VARIABLES", "t2m,tp,d2m").split(",")

        # Read all district GeoJSONs and create union
        districts_path = geojson_folder_path / "districts"
        logger.info(f"Reading GeoJSON files from: {districts_path}")

        # Read all GeoJSON files in the districts folder
        geojson_files = list(districts_path.glob("*.geojson")) + list(districts_path.glob("*.json"))
        if not geojson_files:
            raise FileNotFoundError(f"No GeoJSON files found in {districts_path}")

        logger.info(f"Found {len(geojson_files)} GeoJSON files")

        # Read and union all geometries
        gdfs = [gpd.read_file(file) for file in geojson_files]
        combined_gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True))
        union_geometry = combined_gdf.unary_union

        # Convert to GeoJSON format for DataIO
        region_geojson = gpd.GeoSeries([union_geometry]).__geo_interface__

        # Get bbox from union geometry
        minx, miny, maxx, maxy = union_geometry.bounds
        bbox = ((miny, maxy), (minx, maxx))  # ((lat_min, lat_max), (lon_min, lon_max))

        logger.info(f"DataIO settings - Dataset: {dataset_name}, Variables: {variables}")
        logger.info(f"Using union of {len(geojson_files)} district GeoJSONs")
        logger.info(f"Calculated bbox from union geometry: {bbox}")

        # Download weather data from DataIO
        dataset = download_weather_data(
            client=client,
            dataset_name=dataset_name,
            variables=variables,
            start_date=case_start_date_dt.strftime("%Y-%m-%d"),
            end_date=case_end_date_dt.strftime("%Y-%m-%d"),
            geojson=region_geojson,
        )

        # log size of xarray dataset : no of rows in each variable
        logger.info(f"Size of xarray dataset: {dataset.to_dataframe().shape}")

        # Parse and save weather data
        logger.info("Parsing DataIO weather data")
        parse_dataio_weather_to_csv(
            root_dir=root_dir,
            dataset=dataset,
            output_csv_path="datasets/weather_district_sampled.csv",
            geojson_folder_path=geojson_folder_path,
            start_date=case_start_date_dt.strftime("%Y-%m-%d"),
            end_date=case_end_date_dt.strftime("%Y-%m-%d"),
            sampling_day=sampling_day,
            bbox=bbox,
            config=config,
        )

    if args.use_previously_downloaded_weather_data_from_s3:
        case_start_date_dt = datetime.strptime(case_start_date, "%Y-%m-%d") - timedelta(days=100)
        case_end_date_dt = run_date

        logger.info("Reusing weather data which was previously downloaded.")

        # Get DataIO settings from environment
        dataset_name = os.getenv("DATAIO_DATASET_NAME", "era5_sfc")
        variables = os.getenv("DATAIO_WEATHER_VARIABLES", "t2m,tp,d2m").split(",")

        # Read all district GeoJSONs and create union
        districts_path = geojson_folder_path / "districts"
        logger.info(f"Reading GeoJSON files from: {districts_path}")

        # Read all GeoJSON files in the districts folder
        geojson_files = list(districts_path.glob("*.geojson")) + list(districts_path.glob("*.json"))
        if not geojson_files:
            raise FileNotFoundError(f"No GeoJSON files found in {districts_path}")

        logger.info(f"Found {len(geojson_files)} GeoJSON files")

        # Read and union all geometries
        gdfs = [gpd.read_file(file) for file in geojson_files]
        combined_gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True))
        union_geometry = combined_gdf.unary_union

        # Convert to GeoJSON format for DataIO
        region_geojson = gpd.GeoSeries([union_geometry]).__geo_interface__

        # Get bbox from union geometry
        minx, miny, maxx, maxy = union_geometry.bounds
        bbox = ((miny, maxy), (minx, maxx))  # ((lat_min, lat_max), (lon_min, lon_max))

        logger.info(f"DataIO settings - Dataset: {dataset_name}, Variables: {variables}")
        logger.info(f"Using union of {len(geojson_files)} district GeoJSONs")
        logger.info(f"Calculated bbox from union geometry: {bbox}")

        dataset = xr.open_dataset(
            f"data/weather/{dataset_name}/{dataset_name}_{'_'.join(variables)}_{case_start_date_dt.strftime('%Y%m%d')}_{case_end_date_dt.strftime('%Y%m%d')}.nc"
        )

        # log size of xarray dataset : no of rows in each variable
        logger.info(f"Size of xarray dataset: {dataset.to_dataframe().shape}")

        # Parse and save weather data
        logger.info("Parsing DataIO weather data")
        parse_dataio_weather_to_csv(
            root_dir=root_dir,
            dataset=dataset,
            output_csv_path="datasets/weather_district_sampled.csv",
            geojson_folder_path=geojson_folder_path,
            start_date=case_start_date_dt.strftime("%Y-%m-%d"),
            end_date=case_end_date_dt.strftime("%Y-%m-%d"),
            sampling_day=sampling_day,
            bbox=bbox,
            config=config,
        )

    # Step 5: Identify Cutoff Dates
    logger.info("Identify cutoff dates")
    cutoff, pred_upto, cutoff_case, cutoff_weather, prediction_dates = identify_cutoff_dates(
        root_dir=root_dir, granularity=granularity, region_name=region_name
    )

    logging.info(f"cutoff date: {cutoff}")
    logging.info(f"pred_upto date: {pred_upto}")
    logging.info(f"cutoff_case: {cutoff_case}")
    logging.info(f"cutoff_weather: {cutoff_weather}")
    logging.info(f"prediction_dates: {prediction_dates}")

    if args.generate_thresholds and can_generate_thresholds:
        thresholds_df = generate_thresholds(granularity, [(1, 0), (1, 1), (1, 2)])
        thresholds_file = root_dir / "datasets/thresholds_df.csv"
        thresholds_df.to_csv(thresholds_file, index=False)
        logger.info(f"Thresholds generated and saved to {thresholds_file}")

        if report:
            report.add_a_status_report("Threshold Generation", Status.SUCCESS, "Generated alert thresholds.")

    # Step 6: Run District Predictions
    if args.model_train_and_predict and can_run_predictions:
        # logger.info("Run district predictions")
        # with open(root_dir / "config/config_district.yaml", "r") as f:
        #     config = yaml.safe_load(f)

        # case_data = utils.process_case_data(config, root_dir)  # %% Load weather data
        # weather_data = utils.process_weather_data(config, root_dir)
        # merged_df = utils.merge(config, case_data, weather_data)
        # listValidDates = pd.date_range(start=min(merged_df["recordDate"]), end=pred_upto, freq=sampling_day).tolist()
        # merged_df = merged_df.sort_values([config["spatial_res"], "recordDate"]).reset_index(drop=True)
        # merged_df = merged_df[merged_df["recordDate"].isin(listValidDates)].reset_index(drop=True)

        # """ NEGATIVE BINOMIAL REGRESSION """
        # pred_upto_date = pred_upto
        # to_date = pred_upto_date - pd.Timedelta(days=28)
        # logging.info(f"shape of merged_df: {merged_df.shape}")
        # merged_df = utils.retNAfilledDF(config, merged_df, to_date=pred_upto_date)

        df_district = run_district_predictions(root_dir, pred_upto, cutoff_case, sampling_day, thresholds_df, granularity, region_name)

        if report:
            report.add_a_status_report("Model Predictions", Status.SUCCESS, "Predictions successfully generated")
            # Attach prediction files
            prediction_files = list((root_dir / "results").glob("Predictions_*.csv"))
            # Get the most recent prediction files (created in the last minute)
            import time as time_mod

            current_time = time_mod.time()
            for pred_file in prediction_files:
                if (current_time - pred_file.stat().st_mtime) < 60:  # Files modified in last 60 seconds
                    # report.add_attachment(str(pred_file))
                    logger.info(f"Attached prediction file: {pred_file.name}")

    if args.generate_maps:
        district_filenames = generate_map(df_district, "district", region_name, geojson_folder_path / Path("districts"))
        # state_filenames = generate_map(df_state, "state", region_name, geojson_folder_path / Path("districts"))

        logger.info(f"Generated {len(district_filenames)} map files")

        # Generate LaTeX report (which also creates the plots zip)
        logger.info("Generating LaTeX report and plots zip")
        try:
            # Determine which map files to use based on granularity
            if granularity in ["district", "subdistrict"]:
                map_files_for_report = district_filenames

            zip_path, pdf_path, maps_zip_path = generate_report(
                root_dir=root_dir,
                granularity=granularity,
                region_name=region_name,
                case_start_date=case_start_date,
                cutoff_case=cutoff_case,
                cutoff_weather=cutoff_weather,
                map_filenames=map_files_for_report,
                output_dir="results",
                compile_pdf=True,
            )

            if report:
                # Attach PDF report if available
                if pdf_path and pdf_path.exists():
                    report.add_attachment(str(pdf_path))
                    logger.info(f"Attached report PDF: {pdf_path}")
                    report.add_a_status_report("Report Generation", Status.SUCCESS, f"Generated PDF report: {pdf_path.name}")
                else:
                    report.add_attachment(str(zip_path))
                    logger.info(f"Attached LaTeX zip: {zip_path}")
                    report.add_a_status_report("Report Generation", Status.WARNING, "LaTeX sources generated but PDF compilation failed")

                # Attach plots zip file
                if maps_zip_path and maps_zip_path.exists():
                    report.add_attachment(str(maps_zip_path))
                    logger.info(f"Attached plots zip: {maps_zip_path}")
                    report.add_a_status_report("Map Generation", Status.SUCCESS, f"Generated plots zip: {maps_zip_path.name}")
                else:
                    report.add_a_status_report("Map Generation", Status.WARNING, "Maps generated but zip creation failed")

        except Exception as e:
            logger.error(f"Failed to generate report: {e}")
            if report:
                report.add_a_status_report("Report Generation", Status.ERROR, f"Report generation failed: {e}")

    # Send email report
    if report:
        time_taken = time.time() - start_time
        report.add_a_status_report("Pipeline Completion", Status.SUCCESS, f"Pipeline completed successfully in {time_taken:.2f} seconds")

        try:
            report.send_email()
            logger.info("Email report sent successfully")
        except Exception as e:
            logger.error(f"Failed to send email report: {e}")

    logger.info("Production pipeline completed")

    # End time
    time_taken = time.time() - start_time
    logger.info(f"Time taken for pipeline: {time_taken}")


if __name__ == "__main__":
    main()
