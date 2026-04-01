"""LaTeX / report helpers.

Figure captions, relevant-figure metadata, and map zip assembly for the report stage.
Full PyLaTeX document generation (``gen_latex_code``) is optional and not bundled
as a hard dependency; see ``compile_latex`` if you supply a ``.tex`` tree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd


# --- SOT-aligned: AssessThresholdPerformance.generate_LaTeX_fig_captions ----------


def _to_latex_double_dash_date(val) -> str:
    """SOT: '--'.join(x.split('-')) on a YYYY-MM-DD (or datetime-like) value."""
    ts = pd.to_datetime(val, errors="coerce")
    if pd.isna(ts):
        s = str(val).replace("--", "-")
    else:
        s = ts.strftime("%Y-%m-%d")
    parts = s.split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return "--".join(parts)
    return str(val).replace("-", "--")


def _week_for_fig_filename(val) -> str:
    """Normalize prediction week to YYYY-MM-DD for filenames (matches map step)."""
    s = str(val).replace("--", "-")
    ts = pd.to_datetime(s, errors="coerce")
    if pd.isna(ts):
        return s[:10] if len(s) >= 10 else s
    return ts.strftime("%Y-%m-%d")


def add_latex_figure_metadata(
    df: pd.DataFrame, *, figtype: str = "png", run_date: str = ""
) -> pd.DataFrame:
    """Mirror SOT ``generate_LaTeX_fig_captions`` (AssessThresholdPerformance.py L99–135).

    Adds ``fig_name`` and ``caption``, rewrites ``model`` / ``thresholdMethod`` to
    display strings, and converts date columns to double-dash form for LaTeX.
    """
    if df is None or len(df) == 0:
        return df

    out = df.copy()

    dict_text_model: dict[str, str] = {
        "ensembleModel": "Ensemble Model",
        "negativeBinomialRegression": "Negative Binomial Regression",
        "timeSeriesExtrapolation": "Time Series Extrapolation",
    }
    dict_text_thresh: dict[str, str] = {
        "historical": "Historical Thresholds",
        "previousNweeks": "Previous N-Weeks Thresholds",
    }

    out.loc[:, "model"] = out["model"].map(lambda x: dict_text_model.get(x, x))
    out.loc[:, "thresholdMethod"] = out["thresholdMethod"].map(
        lambda x: dict_text_thresh.get(x, x)
    )

    end_string = (
        pd.Timestamp(run_date).date().strftime("%Y%m%d")
        if run_date
        else pd.Timestamp.today().date().strftime("%Y%m%d")
    )

    def _fig_name_row(row: pd.Series) -> str:
        region_type = row["region_type"]
        thisdate = _week_for_fig_filename(row["startDatePredictedWeek"])
        model = row["model"]
        th = row["thresholdMethod"]
        return (
            "_".join([region_type + "s", thisdate, model, th, end_string])
            + "."
            + figtype
        )

    def _caption_row(row: pd.Series) -> str:
        region_type = row["region_type"]
        thisdate = _week_for_fig_filename(row["startDatePredictedWeek"])
        th = row["thresholdMethod"]
        cap = (
            f"Dengue risk map ({thisdate}) for {str(region_type).lower()}s "
            f"using {str(th).lower()}"
        )
        return cap.replace("n-weeks", "N-weeks")

    out.loc[:, "fig_name"] = out.apply(_fig_name_row, axis=1)
    out.loc[:, "caption"] = out.apply(_caption_row, axis=1)

    # Caption hyphen doubling and scope wording happen in the report step
    # (see ``postprocess_captions_for_rep``).

    out.loc[:, "dateOfComputingPrediction"] = out["dateOfComputingPrediction"].map(
        _to_latex_double_dash_date
    )
    out.loc[:, "startDatePredictedWeek"] = out["startDatePredictedWeek"].map(
        _to_latex_double_dash_date
    )
    return out


# --- SOT-aligned: GenerateLatexCode_PyLaTeX.get_relevant_figures_details ----------


def get_relevant_figures_details(
    best_methods_df: pd.DataFrame,
    reference_date: pd.Timestamp | None = None,
) -> tuple[str, list[str], list[str], list[str]] | None:
    """Match SOT ``get_relevant_figures_details`` (GenerateLatexCode_PyLaTeX.py L35–48)."""
    if reference_date is None:
        reference_date = pd.Timestamp.today().normalize()
    ref = pd.Timestamp(reference_date).normalize()
    cutoff_date = str(ref.date()).replace("-", "--")

    if best_methods_df is None or len(best_methods_df) == 0:
        return None
    df = best_methods_df.copy()
    if "startDatePredictedWeek" not in df.columns:
        return None

    df = df[df["startDatePredictedWeek"] >= cutoff_date].reset_index(drop=True)
    if len(df) == 0:
        return None

    prediction_date = df.loc[0, "dateOfComputingPrediction"]
    listdates = list(df["startDatePredictedWeek"])
    if "fig_name" not in df.columns or "caption" not in df.columns:
        return None
    listfilenames = list(df["fig_name"])
    listcaptions = list(df["caption"])
    return prediction_date, listdates, listfilenames, listcaptions


def get_month_year_range_from_strings(dates: list[str]) -> str:
    """SOT ``get_month_year_range`` using prediction-week strings (with ``--`` ok)."""
    from pipelines.dengue.lib import predictions as pred_lib

    ts_list = [pd.Timestamp(str(d).replace("--", "-")) for d in dates]
    return pred_lib.get_month_year_range(ts_list)


def build_rep_dict(
    *,
    pred_c: str | None,
    dates_c: list[str] | None,
    fnames_c: list[str] | None,
    captions_c: list[str] | None,
    pred_z: str | None,
    dates_z: list[str] | None,
    fnames_z: list[str] | None,
    captions_z: list[str] | None,
    cutoff_case: str,
    cutoff_weather: str,
    epi_data_start_date: str,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Structured report payload (dates use ``--`` for LaTeX-friendly display)."""
    dates_c = dates_c or []
    dates_z = dates_z or []
    fnames_c = fnames_c or []
    fnames_z = fnames_z or []
    captions_c = captions_c or []
    captions_z = captions_z or []

    reportmonth = (
        get_month_year_range_from_strings(dates_c)
        if dates_c
        else get_month_year_range_from_strings(dates_z)
    )
    prediction_date = str(
        pd.Timestamp(run_date).date() if run_date else pd.Timestamp.today().date()
    ).replace("-", "--")
    cc = str(cutoff_case).replace("-", "--")
    cw = str(cutoff_weather).replace("-", "--")
    epi_dd = str(epi_data_start_date).replace("-", "--")
    rep: dict[str, Any] = {
        "reportmonth": reportmonth,
        "prediction_date": prediction_date,
        "report_date": str(pd.Timestamp.today().date()).replace("-", "--"),
        "epi_data_start_date": epi_dd,
        "epi_data_end_date": cc,
        "weather_data_end_date": cw,
        "listdates_corp": dates_c,
        "filenames_corp": fnames_c,
        "captions_corp": captions_c,
        "labels_corp": [f"fig:Corp_{str(val).replace('-', '')}" for val in dates_c],
        "listdates_zone": dates_z,
        "filenames_zone": fnames_z,
        "captions_zone": captions_z,
        "labels_zone": [f"fig:Zone_{str(val).replace('-', '')}" for val in dates_z],
    }
    return rep


def safe_bundle_filename_prefix(prefix: str) -> str:
    """Filesystem-safe token for report zip names (no spaces/slashes)."""
    t = "".join(c if c.isalnum() or c in "-_" else "_" for c in prefix.strip())
    return t or "Report"


def zip_map_files(
    *,
    plots_dir: Path,
    filenames: list[str],
    destination_zip: Path,
) -> None:
    """SOT ``zip_selected_files`` (GenerateLatexCode_PyLaTeX.py L608–624)."""
    destination_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination_zip, "w", zipfile.ZIP_DEFLATED) as zipf:
        for filename in filenames:
            file_path = plots_dir / filename
            if file_path.is_file():
                zipf.write(file_path, arcname=filename)
            else:
                # Log via caller if needed
                continue


def compile_latex(tex_path: str, output_dir: str = ".") -> str:
    """Compile a .tex file to PDF using pdflatex + bibtex."""
    base = Path(tex_path).stem
    cwd = Path(tex_path).parent
    for _ in range(2):
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", base], cwd=cwd, capture_output=True
        )
    subprocess.run(["bibtex", base], cwd=cwd, capture_output=True)
    subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", base], cwd=cwd, capture_output=True
    )
    pdf = cwd / f"{base}.pdf"
    dest = Path(output_dir) / pdf.name
    if pdf.exists() and str(pdf) != str(dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, dest)
    return str(dest)


def write_rep_dict_json(rep_dict: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rep_dict, indent=2, default=str), encoding="utf-8")


def postprocess_captions_for_rep(
    captions: list[str],
    *,
    kind: str,
    caption_corp_scope: str,
    caption_zone_scope: str,
) -> list[str]:
    """Double hyphens in dates; widen `` corps `` / `` zones `` using config phrases."""
    out = [val.replace("-", "--") for val in captions]
    corp_phrase = f" {caption_corp_scope.strip()} "
    zone_phrase = f" {caption_zone_scope.strip()} "
    if kind == "corp":
        return [val.replace(" corps ", corp_phrase) for val in out]
    return [val.replace(" zones ", zone_phrase) for val in out]


def tex_escape_line(s: str) -> str:
    """Minimal escaping for one-line LaTeX text mode."""
    return (
        str(s)
        .replace("\\", "/")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("_", r"\_")
        .replace("#", r"\#")
        .replace("^", r"\textasciicircum{}")
        .replace("~", r"\textasciitilde{}")
        .replace("$", r"\$")
    )


def _fig_block(img: str, caption: str, label: str) -> str:
    """Single LaTeX figure block matching SOT gen_latex_code."""
    return (
        "\\begin{figure}[h!]\n"
        "    \\centering\n"
        f"    \\includegraphics[width=0.95\\textwidth]{{Images/{img}}}\n"
        f"    \\caption{{{caption}}}\n"
        f"    \\label{{{label}}}\n"
        "\\end{figure}"
    )


def gen_full_report_tex(rep_dict: dict[str, Any]) -> str:
    """Generate the full LaTeX report matching SOT ``gen_latex_code`` (GenerateLatexCode_PyLaTeX.py).

    Produces equivalent output without requiring the ``pylatex`` package.
    """
    fnames_c: list[str] = rep_dict.get("filenames_corp") or []
    caps_c: list[str] = rep_dict.get("captions_corp") or []
    labels_c: list[str] = rep_dict.get("labels_corp") or []
    fnames_z: list[str] = rep_dict.get("filenames_zone") or []
    caps_z: list[str] = rep_dict.get("captions_zone") or []
    labels_z: list[str] = rep_dict.get("labels_zone") or []

    epi_start = rep_dict.get("epi_data_start_date", "2021--11--09")
    epi_end = rep_dict.get("epi_data_end_date", "")
    weather_end = rep_dict.get("weather_data_end_date", "")
    reportmonth = rep_dict.get("reportmonth", "")
    prediction_date = rep_dict.get("prediction_date", "")
    report_date = rep_dict.get("report_date", "")

    preamble = r"""\documentclass[a4paper, 12pt]{article}
\usepackage{amsmath, amsthm, amssymb}
\usepackage{graphicx}
\usepackage{subfig}
\usepackage{tabularray}
\UseTblrLibrary{booktabs}
\usepackage[skip=1ex, font=small]{caption}
\usepackage{float}
\usepackage{multirow}
\usepackage{hyperref}
\usepackage[a4paper, left=20mm, right=20mm, top=25mm, bottom=30mm, heightrounded]{geometry}
\usepackage{url}
\usepackage{color}
\usepackage{bm}
\usepackage{comment}
\usepackage[table, dvipsnames]{xcolor}
\usepackage{fancyhdr}
\usepackage{enumitem}
\usepackage{titletoc}
\usepackage[subfigure]{tocloft}
\usepackage{setspace}
\usepackage[numbers]{natbib}

\hypersetup{
    colorlinks,
    linkcolor={blue!80!black},
    citecolor={blue!80!black},
    urlcolor={blue!90!black}
}

\newcommand{\footertext}{
\textcolor{CadetBlue}
{This is a Confidential Document only intended for the marked recipients.
\\ARTPARK, I-HUB for Robotics and Autonomous Systems Innovation Foundation, Ground Floor,
\\SID Entrepreneurship building, Indian Institute of Science, Bangalore - 560012. \url{www.artpark.in}}
}

\pagestyle{fancy}
\fancyhf{}
\fancyhead[R]{\thepage}
\renewcommand{\headrulewidth}{0pt}
\fancyfoot[C]{\scriptsize \footertext}

\fancypagestyle{plain}{
  \fancyhf{}
  \renewcommand{\headrulewidth}{0pt}
  \fancyfoot[C]{\scriptsize \footertext}
}

\makeatletter
\renewcommand{\tableofcontents}{
    \section*{}
    \@starttoc{toc}
}
\makeatother

\renewcommand{\cftsecleader}{\cftdotfill{\cftdotsep}}

\setlength{\parskip}{0.7em}
\setlength{\parindent}{2em}
\setlength{\headheight}{15pt}
\setlength{\cftbeforesecskip}{0.5em}
\setlength{\cftbeforesubsecskip}{0.3em}

\onehalfspacing"""

    title_block = (
        r"\begin{document}"
        "\n"
        r"\thispagestyle{plain}"
        "\n"
        r"{\begin{center}"
        "\n"
        r"\Huge\noindent\textbf{Dengue Risk Zone Predictions"
        "\n"
        r"\\\large "
        + reportmonth
        + r"}"
        + "\n"
        + r"\end{center}}"
        + "\n\n"
        + r"\begin{flushright}"
        + "\n"
        + r"\colorbox{CadetBlue!30}{Predictions performed on: "
        + prediction_date
        + r"}"
        + "\n"
        + r"\end{flushright}"
        + "\n\n"
        + r"\thispagestyle{plain}"
        + "\n\n"
        + r"{"
        + "\n"
        + r"\footnotesize"
        + "\n"
        + r"\begingroup"
        + "\n"
        + r"  \let\clearpage\relax"
        + "\n"
        + r"  \tableofcontents"
        + "\n"
        + r"\endgroup"
        + "\n"
        + r"}"
    )

    about_section = (
        r"\section{About Risk Zone Classification}\label{sec:IntroRiskZoneClass}"
        "\n"
        "The risk zone classification depends on thresholds computed using the historical dengue case data "
        "within the region(s) of interest. We use the following methods to calculate the thresholds:\n"
        r"\begin{itemize}[leftmargin=*]"
        "\n"
        r"\item Method A (Threshold based on historical cases): We establish a baseline for each month by computing "
        r"the mean $\left(\mu\right)$ of the weekly number of dengue cases for that month over the past "
        r"\emph{five} years. In addition, we calculate the corresponding standard deviation $\left(\sigma\right)$."
        "\n"
        r"\item Method B (Threshold based on recent cases): The mean and standard deviation are calculated from the "
        r"moving average of weekly dengue cases in the past 4 weeks. We have used the definition used by "
        r"Salim, et al. in \cite{Salim2021}. The moving mean and the moving standard deviation required to "
        r"determine the threshold value for each region in the $i$-th week are calculated from weekly cases "
        r"from the previous four weeks in that region."
        "\n"
        r"\end{itemize}"
        "\n"
        "Following the guidance provided in the World Health Organization (WHO) Technical Handbook for "
        r"Dengue Surveillance \cite{WHOHandbook}, we map each prediction to a dengue outbreak risk "
        "zone: Green, Yellow, Orange, and Red. Green indicates minimal risk, followed by yellow, orange, "
        "and red, with the latter indicating very high risk. If sufficient data is not available, we "
        "associate White color with it."
    )

    disclaimer = (
        r"\clearpage"
        "\n"
        r"\noindent\textbf{\textit{Disclaimer}}: These risk maps indicate qualitative risks based on "
        "preliminary analysis using available data, which includes historical case patterns and trends, "
        r"and weather parameters (see the section on~\nameref{sec:Data}). The predictions for Greater Bengaluru "
        "Authority (GBA) do NOT include case counts for the area of Bengaluru Urban district outside of GBA, "
        "and the projections for the same can be found separately. The risk map is only intended to serve "
        "as a guide for prioritising interventions such as Source Reduction Activities (SRA). Careful "
        "interpretation must be taken of any results herein and their practical significance to policy. "
        r"Please share feedback, if any, at: \href{mailto:onehealth@artpark.in}{onehealth@artpark.in}."
    )

    # Corp figures
    corp_figs_lines = [
        r"\clearpage",
        r"\section{Outbreak Risk Zone Maps}\label{sec:RiskMaps}",
    ]
    corp_figs_lines.append(
        "In the following risk maps, green, yellow, orange, and red indicate Low, Moderate, High, and "
        "Very High risk levels, respectively."
    )
    for img, cap, lbl in zip(fnames_c, caps_c, labels_c):
        corp_figs_lines.append(_fig_block(img, cap, lbl))
        corp_figs_lines.append(r"\clearpage")

    data_section = (
        r"\section{Data}\label{sec:Data}"
        "\n"
        "We use the following datasets, aggregated to weekly frequency at corp-level spatial resolution:\n"
        r"\begin{enumerate}[leftmargin=*]"
        "\n"
        rf"\item Epidemiological Data: Yearly dengue line list data from {epi_start} till {epi_end}."
        "\n"
        r"\item \sloppy Meteorological Data: {\sffamily 2m\_Temperature}, {\sffamily 2m\_Dewpoint\_Temperature}, "
        rf"and {{\sffamily Total\_Precipitation}} (available until {weather_end}). "
        r"\newline\textit{Source}: \href{https://www.ecmwf.int/en/forecasts/dataset/ecmwf-reanalysis-v5}"
        r"{ECMWF Reanalysis v5 (ERA5)} \cite{hersbach2023era5, climate2023change}."
        "\n"
        r"\item Socio-economic Data: Population data from the 15th decadal census of India (2011) and the area of the corp."
        "\n"
        r"\end{enumerate}"
        "\n"
        "Recall that we do not include the cases of Bengaluru Urban outside of the Greater Bengaluru "
        "Authority (GBA) in the epidemiological data. We examine GBA cases separately because "
        "(a)~the city of Bengaluru has GBA with a separate jurisdiction and governance structure, and "
        r"(b)~the city of Bengaluru forms 20\% of the population of Karnataka."
    )

    model_section = (
        r"\section{Model}\label{sec:Model}"
        "\n"
        "The risk maps in this report are generated using the results of an ensemble of the Negative "
        "Binomial Regression model (Generalised Linear Models Family) and Linear Time-series Extrapolation "
        "model. The maps in the Appendix are generated using the Negative Binomial Regression model."
    )

    references_section = (
        r"\newpage"
        "\n"
        r"\section{References}\label{sec:References}"
        "\n"
        r"\bibliographystyle{unsrtnat}"
        "\n"
        r"\begingroup"
        "\n"
        r"\renewcommand{\section}[2]{}"
        "\n"
        r"\bibliography{bibliography}"
        "\n"
        r"\endgroup"
    )

    # Zone figures in appendix
    appendix_lines = [r"\newpage", r"\section{Appendix}\label{sec: Appendix}"]
    appendix_lines.append(
        "The following are the risk maps based on the data available for different (10) zones:"
    )
    for i, (img, cap, lbl) in enumerate(zip(fnames_z, caps_z, labels_z)):
        appendix_lines.append(_fig_block(img, cap, lbl))
        if i < len(fnames_z) - 1:
            appendix_lines.append(r"\clearpage")

    footer_block = (
        r"\begin{flushright}"
        r"\colorbox{CadetBlue!30}{Report generated on: " + report_date + r"}"
        r"\end{flushright}"
    )

    parts = [
        preamble,
        title_block,
        about_section,
        disclaimer,
        "\n".join(corp_figs_lines),
        data_section,
        model_section,
        references_section,
        "\n".join(appendix_lines),
        footer_block,
        r"\end{document}",
    ]
    return "\n\n".join(parts) + "\n"


def minimal_summary_tex(rep_dict: dict[str, Any], *, document_title: str) -> str:
    """Minimal summary .tex for standalone preview (not the full bundle report)."""
    title_tex = tex_escape_line(document_title)
    lines = [
        r"\documentclass[11pt,a4paper]{article}",
        r"\usepackage[hidelinks]{hyperref}",
        r"\usepackage[margin=20mm]{geometry}",
        r"\begin{document}",
        rf"\title{{{title_tex}}}",
        r"\date{\today}",
        r"\maketitle",
        r"\section*{Overview}",
        r"This PDF summarizes key \texttt{rep\_dict} fields. "
        r"The full structure is in the accompanying JSON file.",
        r"\section*{Key fields}",
        r"\begin{itemize}",
    ]
    for key in (
        "reportmonth",
        "prediction_date",
        "report_date",
        "epi_data_start_date",
        "epi_data_end_date",
        "weather_data_end_date",
    ):
        if key in rep_dict and rep_dict[key] is not None:
            k = tex_escape_line(key)
            v = tex_escape_line(str(rep_dict[key]))
            lines.append(rf"\item \textbf{{{k}}}: {v}")
    lines.append(r"\end{itemize}")
    nc = len(rep_dict.get("filenames_corp") or [])
    nz = len(rep_dict.get("filenames_zone") or [])
    lines.append(r"\section*{Figures}")
    lines.append(
        rf"Planned map filenames: {tex_escape_line(str(nc))} corp, "
        rf"{tex_escape_line(str(nz))} zone (see JSON for full lists)."
    )
    lines.append(r"\end{document}")
    return "\n".join(lines) + "\n"


def create_report_bibliography_bib(access_date: str) -> str:
    """BibTeX bundle aligned with the legacy report zip (references block)."""
    return f"""
@misc{{tr2013directorate,
type    = {{Technical Report}},
title   = {{Directorate of Economics and Statistics, Bangalore. Projected Population of Karnataka 2012-2021 (Provisional), DES 22 of 2013}},
url     = {{https://des.kar.nic.in/docs/Projected\\%20Population\\%202012-2021.pdf}},
year    = {{2013}},
month   = {{18 Feb}},
}}

@misc{{WHOHandbook,
author  = {{World Health Organization}},
title   = {{Technical handbook for dengue surveillance, outbreak prediction/detection and outbreak response}},
year    = {{2016}},
pages   = {{92 p.}},
publisher = {{World Health Organization}},
type    = {{Publications}},
url     = {{https://iris.who.int/handle/10665/250240}}
}}

@Article{{Salim2021,
author  = {{Salim, Nurul Azam Mohd
and Wah, Yap Bee
and Reeves, Caitlynn
and Smith, Madison
and Yaacob, Wan Fairos Wan
and Mudin, Rose Nani
and Dapari, Rahmat
and Sapri, Nik Nur Fatin Fatihah
and Haque, Ubydul}},
title   = {{Prediction of dengue outbreak in {{Selangor}} {{Malaysia}} using machine learning techniques}},
journal = {{Scientific Reports}},
year    = {{2021}},
volume  = {{11}},
number  = {{1}},
pages   = {{939}},
issn    = {{2045-2322}},
doi     = {{10.1038/s41598-020-79193-2}},
url     = {{https://doi.org/10.1038/s41598-020-79193-2}}
}}

@misc{{hersbach2023era5,
author  = {{Hersbach, Hans
and Bell, Bill
and Berrisford, Paul
and Biavati, Gionata
and Hor{{\\'a}}nyi, Andr{{\\'a}}s
and Mu{{\\~n}}oz Sabater, Joaqu{{\\'\\i}}n
and Nicolas, Julien
and Peubey, Carole
and Radu, Raluca
and Rozum, Iryna
and Schepers, Dinand
and Simmons, Adrian
and Soci, Cornel
and Dee, Dick
and Th{{\\'e}}paut, Jean-No\\"el}},
title ={{{{ERA5}} hourly data on single levels from 1940 to present. {{Copernicus Climate Change Service (C3S) Climate Data Store (CDS)}}}},
year    = {{2023}},
publisher = {{{{Copernicus Climate Change Service (C3S) Climate Data Store (CDS)}}}},
doi     = {{10.24381/cds.adbb2d47}},
note    = {{Accessed on {access_date}}},
url     = {{https://doi.org/10.24381/cds.adbb2d47}}
}}

@misc{{climate2023change,
author  = {{{{Copernicus Climate Change Service, Climate Data Store}}}},
title   = {{{{ERA5 hourly data on single levels from 1940 to present. Copernicus Climate Change Service (C3S) Climate Data Store (CDS)}}}},
year    = {{2023}},
note    = {{Accessed on {access_date}}},
doi     = {{10.24381/cds.adbb2d47}},
url     = {{https://doi.org/10.24381/cds.adbb2d47}}
}}"""


def create_latex_bundle_zip(
    *,
    rep_dict: dict[str, Any],
    access_date: str,
    plots_dir: Path,
    image_filenames: list[str],
    destination_zip: Path,
) -> None:
    """Zip full ``main.tex``, ``bibliography.bib``, and ``Images/`` map PNGs (matching SOT zip layout)."""
    destination_zip.parent.mkdir(parents=True, exist_ok=True)
    main_tex = gen_full_report_tex(rep_dict)
    bib = create_report_bibliography_bib(access_date)
    with zipfile.ZipFile(destination_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.tex", main_tex)
        zf.writestr("bibliography.bib", bib)
        for name in image_filenames:
            p = plots_dir / name
            if p.is_file():
                zf.write(p, arcname=f"Images/{name}")


def write_minimal_pdf_source(
    rep_dict: dict[str, Any], tex_path: Path, *, document_title: str
) -> None:
    """Write the summary article .tex (same body as ``main.tex`` in the LaTeX bundle zip)."""
    tex_path.parent.mkdir(parents=True, exist_ok=True)
    tex_path.write_text(
        minimal_summary_tex(rep_dict, document_title=document_title),
        encoding="utf-8",
    )


def compile_latex_bundle_zip(
    zip_path: Path,
    *,
    destination_pdf_path: Path,
    latex_bin: str = "pdflatex",
) -> Path | None:
    """Extract zip to a temp dir, compile main.tex, copy PDF, clean up.

    SOT sequence: extract zip → pdflatex → bibtex → pdflatex → pdflatex → copy PDF.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_dir)

        main_tex = tmp_dir / "main.tex"
        if not main_tex.exists():
            return None

        def _run(*cmd: str) -> bool:
            try:
                r = subprocess.run(
                    list(cmd), cwd=tmp_dir, capture_output=True, text=True
                )
                return r.returncode == 0
            except FileNotFoundError:
                return False

        _run(latex_bin, "-interaction=nonstopmode", "main.tex")
        _run("bibtex", "main")
        _run(latex_bin, "-interaction=nonstopmode", "main.tex")
        _run(latex_bin, "-interaction=nonstopmode", "main.tex")

        pdf = tmp_dir / "main.pdf"
        if not pdf.exists():
            return None

        destination_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, destination_pdf_path)

    return destination_pdf_path
