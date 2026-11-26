"""Write the prep and forecast configs for a geography from the bundled templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

TEMPLATES = Path(__file__).resolve().parent.parent / "assets"
# Levels the forecast pipeline maps to a column when it loads prepared cases
# (_REGION_ADMIN_COL in pipelines/dengue/lib/case_data.py). Other levels run
# through prep but fail or lose their maps in the forecast pipeline.
SUPPORTED_LEVELS = ("district", "subdistrict", "mandal", "corp", "zone", "ward")


@dataclass(frozen=True)
class LineList:
    """Where the case-level files are and which columns the parser should read."""

    folder: Path
    date_column: str
    unit_column: str = ""  # holds full unit IDs such as district_344
    code_column: str = ""  # holds numeric codes such as 344
    filters: tuple = ()  # (column, [values]) pairs; a row must match every one


@dataclass(frozen=True)
class Geography:
    name: str  # short code used in file names
    title: str  # display name
    workdir: Path
    region_type: str
    n_units: int
    date_start: str
    run_date: str
    history_years: int
    models: list[str] = field(default_factory=lambda: ["rf"])
    line_list: LineList | None = None

    @property
    def geojson_base(self) -> str:
        return str(self.workdir / "geojsons")

    @property
    def prepared(self) -> str:
        return str(self.workdir / "prepared_data")


def _artifacts(config: dict, path: Path) -> None:
    config["storages"]["artifacts"]["filesystem"]["base_path"] = str(path)


def prep_config(geo: Geography) -> dict:
    config = yaml.safe_load((TEMPLATES / "prep.template.yaml").read_text())
    config["pipeline"]["title"] = f"Dengue Data Prep: {geo.title}"
    data = config["data"]
    data["region_type"] = geo.region_type
    data["prepared_data"]["base_dir"] = geo.prepared
    # Bound prep by the run date, so it fetches no weather beyond what the
    # forecast will read and a rerun gives the same files.
    config["run"]["run_date"] = geo.run_date
    data["date_range"] = {"start": geo.date_start, "end": geo.run_date}
    data["geojson"]["base_path"] = geo.geojson_base
    data["weather_download"]["parsed_output_path"] = str(geo.workdir / "weather")
    data["weather_parse"]["region_type"] = geo.region_type
    if geo.line_list:
        data["case_download"] = {"enabled": True, "source_mode": "filesystem",
                                 "source_path": str(geo.line_list.folder)}
        parse = {"region_types": [geo.region_type],
                 "date_column": geo.line_list.date_column}
        if geo.line_list.unit_column:
            parse["region_id_column"] = geo.line_list.unit_column
        if geo.line_list.code_column:
            parse["lgd_code_column"] = geo.line_list.code_column
        if geo.line_list.filters:
            parse["filters"] = [{"column": column, "values": list(values)}
                                for column, values in geo.line_list.filters]
        data["case_parse"] = parse
    _artifacts(config, geo.workdir / "artifacts" / "prep")
    return config


def forecast_config(geo: Geography) -> dict:
    config = yaml.safe_load((TEMPLATES / "forecast.template.yaml").read_text())
    config["state"] = geo.name
    config["pipeline"]["title"] = f"Dengue Early Warning System: {geo.title}"
    config["run"]["run_date"] = geo.run_date
    data = config["data"]
    data["prepared_data"] = {"base_dir": geo.prepared, "region_type": geo.region_type}
    data["case_parse"] = {"region_types": [geo.region_type],
                          "date_start": geo.date_start, "date_end": ""}
    data["geojson"]["base_path"] = geo.geojson_base
    data["weather_parse"]["region_type"] = geo.region_type
    config["thresholds"]["region_type"] = geo.region_type
    config["thresholds"]["historical_n_years"] = geo.history_years
    config["model"]["spatial_res"] = geo.region_type
    config["model"]["models"] = list(geo.models)
    config["assess"] = {f"total_{geo.region_type}_regions": geo.n_units}
    config["maps"]["figure_title"] = f"{geo.title} Dengue Risk Map"
    if len(geo.models) == 1:
        # With one model there is no ensemble row, so the report must be told
        # which model to show.
        config["report"]["primary"] = geo.models[0]
    config["report_distribution"]["state"] = geo.title
    config["report_distribution"]["region"] = f"{geo.title} ({geo.n_units} units)"
    _artifacts(config, geo.workdir / "artifacts" / "forecast")
    return config


def write(geo: Geography) -> dict[str, str]:
    geo.workdir.mkdir(parents=True, exist_ok=True)
    written = {}
    for label, config in (("prep", prep_config(geo)), ("forecast", forecast_config(geo))):
        path = geo.workdir / f"{geo.name}_{geo.region_type}_{label}.yaml"
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        written[label] = str(path)
    return written
