"""LaTeX report generation.

Translated from GBA ``GenerateLatexCode_PyLaTeX.py``.
Only the orchestration helpers are included here.  The full LaTeX
generation requires ``pylatex`` and will call through to it when
available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pandas as pd


def get_relevant_figures_details(
    best_methods_df: pd.DataFrame,
    reference_date: pd.Timestamp | None = None,
) -> tuple[str, list[str], list[str], list[str]] | None:
    """Extract figure filenames and captions from the best-methods assessment."""
    if reference_date is None:
        reference_date = pd.Timestamp.today().normalize()
    cutoff = str(reference_date.date()).replace("-", "--")
    df = best_methods_df.copy()
    if "startDatePredictedWeek" not in df.columns:
        return None
    df = df[df["startDatePredictedWeek"] >= cutoff].reset_index(drop=True)
    if df.empty:
        return None
    pred_date = df.loc[0, "dateOfComputingPrediction"]
    return (
        pred_date,
        list(df["startDatePredictedWeek"]),
        list(df.get("fig_name", [])),
        list(df.get("caption", [])),
    )


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
