# -*- coding: utf-8 -*-
"""
Created on Tue Jul  1 17:05:29 2025

@author: Tarun
"""

import pandas as pd
from pylatex import Document, Section, Figure, NoEscape, Itemize, Enumerate, NewPage, Command, Package
from pylatex.utils import italic
from pathlib import Path
import pickle
import zipfile
import io
import os
import subprocess
import shutil


# %% Specify the path
path = Path('.')

with open('dumps/cutoffs.pkl', 'rb') as f:
    cutoff, pred_upto, cutoff_case, cutoff_weather, prediction_dates = pickle.load(f)

with open('dumps/monthstring.pkl', 'rb') as f:
    monthstring = pickle.load(f)

endString = pd.Timestamp.today().date().strftime(format='%Y%m%d')  # This becomes a part of file names
# endString = '20250708'


# %% Read the files containing the choice of images
def get_relevant_figures_details(path_listfigs, reference_date=pd.Timestamp.today().normalize()):
    """Get details of relevant figures for the report."""
    df = pd.read_csv(path_listfigs, index_col=0)
    cutoff_date = str(reference_date).replace('-', '--')
    df = df[df['startDatePredictedWeek'] >= cutoff_date].reset_index(drop=True)
    if (len(df) == 0):
        print('WARNING: All dates for predicted risk zones are past! Nothing to add to the report!')
        return None
    else:
        prediction_date = df.loc[0, 'dateOfComputingPrediction']
        listdates = list(df['startDatePredictedWeek'])
        listfilenames = list(df['fig_name'])
        listcaptions = list(df['caption'])
        return prediction_date, listdates, listfilenames, listcaptions


# %% Generate monthstring for filtered dates
def get_month_year_range(listdates):
    """From a list of dates, generate a string which indicates the month range."""
    # Ensure all dates are pd.Timestamp and drop duplicates
    unique_months = sorted({(d.year, d.month) for d in listdates})

    # Format helper
    def format_month_year(y, m): return pd.Timestamp(year=y, month=m, day=1).strftime('%b %Y')
    def format_month(m): return pd.Timestamp(year=2000, month=m, day=1).strftime('%b')

    if not unique_months:
        print('WARNING: The list of dates is empty!')
        return ''

    # Extract first and last entries
    (start_year, start_month), (end_year, end_month) = unique_months[0], unique_months[-1]

    if len(unique_months) == 1:
        return format_month_year(start_year, start_month)

    elif start_year == end_year:
        # Same year, different months
        return f"{format_month(start_month)} - {format_month(end_month)} {start_year}"

    else:
        # Different years
        return f"{format_month(start_month)} {start_year} - {format_month(end_month)} {end_year}"


# %% Generate the values that go into the LaTeX report
rep_dict = {}
ref_date = pd.Timestamp.today().normalize().date()
# ref_date = dt.date(2025, 6, 10)
pred_D, dates_D, fnames_D, captions_D = get_relevant_figures_details(
    path_listfigs=f'dumps/best_method_district_{monthstring}_{endString}.csv',
    reference_date=ref_date
    )
captions_D = [val.replace('-', '--') for val in captions_D]

reportmonth = get_month_year_range(listdates=[pd.Timestamp(val.replace('--', '-')) for val in dates_D])
prediction_date = pred_D

pred_D0, dates_Sd, fnames_Sd, captions_Sd = get_relevant_figures_details(
    path_listfigs=f'dumps/best_method_subdistrict_{monthstring}_{endString}.csv',
    reference_date=ref_date
    )
captions_Sd = [val.replace('-', '--') for val in captions_Sd]

rep_dict['reportmonth'] = reportmonth
rep_dict['prediction_date'] = prediction_date
rep_dict['report_date'] = str(pd.Timestamp.today().date()).replace('-', '--')
rep_dict['epi_data_start_date'] = "2017--01--01"
rep_dict['epi_data_end_date'] = str(cutoff_case.date()).replace('-', '--')
rep_dict['weather_data_end_date'] = str(cutoff_weather.date()).replace('-', '--')
rep_dict['listdates_district'] = dates_D
rep_dict['filenames_district'] = fnames_D
rep_dict['captions_district'] = captions_D
rep_dict['labels_district'] = [f'fig:District_{val.replace('-', '')}' for val in dates_D]
rep_dict['listdates_subdistrict'] = dates_Sd
rep_dict['filenames_subdistrict'] = fnames_Sd
rep_dict['captions_subdistrict'] = captions_Sd
rep_dict['labels_subdistrict'] = [f'fig:Subdistrict_{val.replace('-', '')}' for val in dates_Sd]


# %% LaTeX code
def gen_latex_code(rep_dict):
    """Generate the LaTeX code content."""
    # Initialize the document with desired class and options
    doc = Document(documentclass='article', document_options=['a4paper', '12pt'])

    # Add necessary packages in the preamble
    doc.preamble.append(NoEscape(r'''
\usepackage{amsmath, amsthm, amssymb}
\usepackage{graphicx}
\usepackage{subfig}
\usepackage{tabularray}         % new
\UseTblrLibrary{booktabs}       % booktabs package load as  tabularray library
\usepackage[skip=1ex, font=small]{caption}  % for caption formating
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
\usepackage{titletoc} % For custom table of contents
\usepackage[subfigure]{tocloft}
\usepackage{setspace}
%\usepackage{tocbibind} % For adding bibliography to the table of contents
\usepackage[numbers]{natbib} % For bibliography
'''))

    # Custom hypersetup
    doc.preamble.append(NoEscape(r'''
\hypersetup{
    colorlinks,
    linkcolor={blue!80!black},
    citecolor={blue!80!black},
    urlcolor={blue!90!black}
}
'''))

    # Custom footer command and fancyhdr setup
    doc.preamble.append(NoEscape(r'''
\newcommand{\footertext}{
\textcolor{CadetBlue}
{This is a Confidential Document only intended for the marked recipients.
\\ARTPARK, I-HUB for Robotics and Autonomous Systems Innovation Foundation, Ground Floor,
\\SID Entrepreneurship building, Indian Institute of Science, Bangalore - 560012. \url{www.artpark.in}}
}

% Set up the header and footer
\pagestyle{fancy}
\fancyhf{} % Clear all header and footer fields

% Header
\fancyhead[R]{\thepage} % Left even, right odd
\renewcommand{\headrulewidth}{0pt} % Remove horizontal rule below header

% Footer
\fancyfoot[C]{\scriptsize \footertext} % Center footer

% Remove the header on the first page
\fancypagestyle{plain}{
  \fancyhf{} % Clear header and footer
  \renewcommand{\headrulewidth}{0pt} % Remove horizontal rule below header
  \fancyfoot[C]{\scriptsize \footertext} % Center footer on first page
}
'''))

    # For table of contents
    doc.preamble.append(NoEscape(r'''
% Custom table of contents command without title
\makeatletter
\renewcommand{\tableofcontents}{
    \section*{} % Remove default title
    \@starttoc{toc}
}
\makeatother

% Add dots between section headings and page numbers in TOC
\renewcommand{\cftsecleader}{\cftdotfill{\cftdotsep}}
'''))

    # Set spacing
    doc.preamble.append(NoEscape(r'''
\setlength{\parskip}{0.7em}
\setlength{\parindent}{2em}
\setlength{\headheight}{15pt}
\setlength{\cftbeforesecskip}{0.5em} % Adjust spacing between sections
\setlength{\cftbeforesubsecskip}{0.3em} % Adjust spacing between subsections

\onehalfspacing
'''))

    # Title and TOC
    this_block = NoEscape(r"""
\thispagestyle{plain}  % First page style (no header, footer with 3 lines)
{\begin{center}
\Huge\noindent\textbf{Dengue Risk Zone Predictions
\\\large """ + rep_dict['reportmonth'] + r"""}
\end{center}}

\begin{flushright}
\colorbox{CadetBlue!30}{Predictions performed on: """ + rep_dict['prediction_date'] + r"""}
\end{flushright}

\thispagestyle{plain}  % First page style (no header, footer with 3 lines)

{
\footnotesize
\begingroup
  \let\clearpage\relax
  \tableofcontents
\endgroup
}
""")
    doc.append(this_block)

    # About Risk Zone Classification
    with doc.create(Section('About Risk Zone Classification', label='sec:IntroRiskZoneClass')):
        rawtext = r'''
The risk zone classification depends on thresholds computed using the historical dengue case data
within the region(s) of interest. We use the following methods to calculate the thresholds:
'''
        doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))
        with doc.create(Itemize(options='leftmargin=*')) as itemize:
            methA = r'''
Method A (Threshold based on historical cases): We establish a baseline for each month by computing
the mean $\left(\mu\right)$ of the weekly number of dengue cases for that month over the past
\emph{five} years. In addition, we calculate the corresponding standard deviation
$\left(\sigma\right)$.
'''
            itemize.add_item(NoEscape(" ".join(methA.strip().splitlines())))
            methB = r'''
Method B (Threshold based on recent cases): The mean and standard deviation are calculated from the
moving average of weekly dengue cases in the past 4 weeks. We have used the definition used by
Salim, et al. in \cite{Salim2021}. The moving mean and the moving standard deviation required to
determine the threshold value for each region in the $i$-th week are calculated from weekly cases
from the previous four weeks in that region.
'''
            itemize.add_item(NoEscape(" ".join(methB.strip().splitlines())))
        rawtext = r'''
Following the guidance provided in the World Health Organization (WHO) Technical Handbook for
Dengue Surveillance \cite{WHOHandbook}, we map each prediction to a dengue outbreak risk
zone: Green, Yellow, Orange, and Red. Green indicates minimal risk, followed by yellow, orange,
and red, with the latter indicating very high risk. If sufficient data is not available, we
associate White color with it.
'''
        doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))

    # Disclaimer
    doc.append(NoEscape(r'\clearpage'))
    rawtext = r'''
\noindent\textbf{\textit{Disclaimer}}: These risk maps indicate qualitative risks based on
preliminary analysis using available data, which includes historical case patterns and trends,
and weather parameters (see the section on~\nameref{sec:Data}). The predictions for Bengaluru
Urban do NOT include case counts for the BBMP area, and the projections for BBMP can be found
separately. The risk map is only intended to serve as a guide for prioritising interventions
such as Source Reduction Activities (SRA). Careful interpretation must be taken of any results
herein and their practical significance to policy. Please share feedback, if any,
at: \href{mailto:onehealth@artpark.in}{onehealth@artpark.in}.
'''
    doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))

    # Risk Maps Section
    doc.append(NoEscape('\n'))
    doc.append(NoEscape(r'\clearpage'))
    with doc.create(Section('Outbreak Risk Zone Maps', label='sec:RiskMaps')):
        rawtext = r'''
In the following risk maps, green, yellow, orange, and red indicate Low, Moderate, High, and
Very High risk levels, respectively.
'''
        doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))
        for img, caption, label in zip(
                rep_dict['filenames_district'],
                rep_dict['captions_district'],
                rep_dict['labels_district']):
            with doc.create(Figure(position='h!')) as fig:
                fig.append(NoEscape(r'\centering'))
                fig.append(NoEscape(rf'''\includegraphics[trim={{4.5cm 2cm 1.2cm 1cm}}, clip, width=\textwidth]{{Images/{img}}}'''))
                fig.add_caption(NoEscape(caption))
                fig.append(NoEscape(rf'\label{{{label}}}'))
            doc.append(NoEscape(r'\clearpage'))

    # Data Section
    doc.append(NoEscape('\n'))
    with doc.create(Section("Data", label="sec:Data")):
        rawtext = r'''
We use the following datasets, aggregated to weekly frequency at district-level spatial resolution:
'''
        doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))
        with doc.create(Enumerate(options='leftmargin=*')) as enum:
            rawtext = rf'''
Epidemiological Data: Yearly dengue line list data from 2017--01--01 till {rep_dict['epi_data_end_date']}.
'''
            enum.add_item(NoEscape(" ".join(rawtext.strip().splitlines())))
            rawtext = rf"""
\sloppy Meteorological Data: {{\sffamily 2m\_Temperature}}, {{\sffamily 2m\_Dewpoint\_Temperature}},
and {{\sffamily Total\_Precipitation}} (available until {rep_dict['weather_data_end_date']}).
\newline\textit{{Source}}: \href{{https://www.ecmwf.int/en/forecasts/dataset/ecmwf-reanalysis-v5}}
{{ECMWF Reanalysis v5 (ERA5)}} \cite{{hersbach2023era5, climate2023change}}.
"""
            enum.add_item(NoEscape(" ".join(rawtext.strip().splitlines())))
            rawtext = r'''
Socio-economic Data: Population data from the 15th decadal census of India (2011) and the area of
the district.
'''
            enum.add_item(NoEscape(" ".join(rawtext.strip().splitlines())))
        rawtext = r'''
Recall that we do not include the cases of urban local bodies of BBMP in the epidemiological data.
We examine BBMP cases separately because (a) the city of Bengaluru has BBMP with a separate
jurisdiction and governance structure, and (b) the city of Bengaluru forms 20\% of the population
of Karnataka.
'''
        doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))

    # Model and Performance
    doc.append(NoEscape('\n'))
    with doc.create(Section("Model", label="sec:Model")):
        rawtext = r'''
The risk maps in this report are generated using the results of an ensemble of the Negative
Binomial Regression model (Generalised Linear Models Family) and Linear Time-series Extrapolation
model. The maps in the Appendix are generated using the Negative Binomial Regression model.
'''
        doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))

    # Model Performance
    doc.append(NoEscape('\n'))
    with doc.create(Section("Model Performance", label="sec:ModelPerfomance")):
        rawtext = r'''
Model performance was assessed by a cost-sensitive performance matrix where underpredicted red
zones are penalised more than underpredicted green zones. The normalised model score computed
from the metric for the ensemble of Time Series Extrapolation (TSE) and Negative Binomial
Regression (NBR) is 0.67. Here, the maximum possible value of 1 will be when the model performs
best with no misclassifications. The minimum possible value of 0 will be when the model performs
in the worst possible way by maximising the cost for each prediction.
'''
        doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))

    # References
    doc.append(NewPage())
    with doc.create(Section("References", label="sec:References")):
        doc.append(NoEscape(r'''
\bibliographystyle{unsrtnat}
\begingroup
\renewcommand{\section}[2]{}
\bibliography{bibliography}
\endgroup
'''))

    # Appendix
    doc.append(NewPage())
    with doc.create(Section("Appendix", label="sec: Appendix")):
        rawtext = r'''
The following are the risk maps based on the data available for different (230) sub-districts:
'''
        doc.append(NoEscape(" ".join(rawtext.strip().splitlines())))
        for img, caption, label in zip(
                rep_dict['filenames_subdistrict'],
                rep_dict['captions_subdistrict'],
                rep_dict['labels_subdistrict']):
            with doc.create(Figure(position='h!')) as fig:
                fig.append(NoEscape(r'\centering'))
                fig.append(NoEscape(rf'''\includegraphics[trim={{4.5cm 2cm 1.2cm 1cm}}, clip, width=\textwidth]{{Images/{img}}}'''))
                fig.add_caption(NoEscape(caption))
                fig.append(NoEscape(rf'\label{{{label}}}'))
            if img != rep_dict['filenames_subdistrict'][-1]:
                doc.append(NoEscape(r'\clearpage'))

    doc.append(NoEscape(
        r'\begin{flushright}'
        r'\colorbox{CadetBlue!30}{Report generated on: '
        f'{rep_dict['report_date']}'
        r'}'
        r'\end{flushright}'
        ))

    # doc.generate_tex('dengue_risk_report')
    return doc


# %% Example
latex_content = gen_latex_code(rep_dict)


# %% Generate the BibTeX file contents
def create_bibtex_content(access_date):
    """Generate the bibtex file and save it."""
    bib_content = f"""
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

    # output_path = "references.bib"
    # with open(output_path, "w", encoding="utf-8") as f:
    #     f.write(bib_content)
    # print(f"BibTeX file saved to {output_path}")
    return bib_content


# %% Example
bib_content = create_bibtex_content(access_date=pd.Timestamp(pred_D.replace('--', '-')).date().strftime(format='%d-%b-%Y'))


# %% Create the zip file containing the tex and bib files and the image files
def create_zip_with_latex_and_images(rep_dict, image_filenames, image_src_dir, access_date, zip_path):
    """Generate zip file containing LaTeX codebase, including bibliography and Images, and save it."""
    # In-memory zip file writing
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:

        # Write LaTeX source to main.tex
        tex_doc = gen_latex_code(rep_dict)
        tex_source = tex_doc.dumps()
        zipf.writestr("main.tex", tex_source)

        # Write BibTeX file
        bib_content = create_bibtex_content(access_date)
        zipf.writestr("bibliography.bib", bib_content)

        # Copy specified images into Images/ folder in the zip
        image_src_path = Path(image_src_dir)
        for img_name in image_filenames:
            img_path = image_src_path / img_name
            if img_path.exists():
                with open(img_path, 'rb') as f:
                    zipf.writestr(f"Images/{img_name}", f.read())
            else:
                print(f"Warning: {img_path} not found.")


# %% Example
create_zip_with_latex_and_images(
    rep_dict=rep_dict,
    zip_path=f"results/Report_KA_{monthstring}_{endString}.zip",
    image_filenames=fnames_D+fnames_Sd,
    image_src_dir="plots",  # change this to actual folder path
    access_date=pd.Timestamp(pred_D.replace('--', '-')).date().strftime(format='%d-%b-%Y')
)


# %% Run the LaTeX Code
def compile_latex_from_zip(zip_path, output_dir, destination_pdf_path=None):
    """Compile the LaTeX code and save results in desired folders."""
    zip_path = Path(zip_path).resolve()

    # Set extraction directory
    if output_dir is None:
        output_dir = zip_path.with_suffix('')  # removes .zip
    else:
        output_dir = Path(output_dir).resolve()

    os.makedirs(output_dir, exist_ok=True)

    # Extract zip contents
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(output_dir)

    # Find .tex files
    tex_files = list(output_dir.rglob("*.tex"))
    if not tex_files:
        raise FileNotFoundError("No .tex file found in the ZIP archive.")

    # Pick the first .tex file (customize if needed)
    main_tex = tex_files[0]
    tex_dir = main_tex.parent
    tex_name = main_tex.stem

    # Step 1: Compile LaTeX (generates .aux)
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "-output-directory",
                    str(tex_dir), str(main_tex)], check=True)

    # Step 2: Run bibtex (assumes .bib is present and used)
    subprocess.run(["bibtex", tex_name], cwd=tex_dir, check=True)

    # Step 3: Compile LaTeX twice more to resolve references
    for _ in range(2):
        subprocess.run(["pdflatex", "-interaction=nonstopmode", "-output-directory",
                        str(tex_dir), str(main_tex)], check=True)

    # Check PDF output
    pdf_path = tex_dir / f"{tex_name}.pdf"
    if not pdf_path.exists():
        raise FileNotFoundError("PDF was not generated.")

    print(f"PDF generated at: {pdf_path}")

    # Copy PDF to destination if specified
    if destination_pdf_path:
        destination_pdf_path = Path(destination_pdf_path).resolve()
        destination_pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pdf_path, destination_pdf_path)
        print(f"PDF copied to: {destination_pdf_path}")

    return pdf_path


# %% Run the LaTeX code
pdf = compile_latex_from_zip(
    zip_path=f"results/Report_KA_{reportmonth}_{endString}.zip",
    output_dir="latex/Output",
    destination_pdf_path=f"results/Report_KA_{reportmonth}_{endString}.pdf")


# %% Copy the relevant figures in a destination zip file
def zip_selected_files(source_dir, filenames, destination_zip_path):
    """
    Copy selected files from source_dir into a zip file at destination_zip_path.

    Parameters :
    ------------
        source_dir (str or Path): The directory where the files are located.
        filenames (list of str): List of filenames (relative to source_dir) to be zipped.
        destination_zip_path (str or Path): Path to the resulting zip file.
    """
    with zipfile.ZipFile(destination_zip_path, 'w') as zipf:
        for filename in filenames:
            file_path = os.path.join(source_dir, filename)
            if os.path.isfile(file_path):
                zipf.write(file_path, arcname=filename)  # store with relative name
            else:
                print(f"Skipped (not found): {filename}")


# %% Run the function to zip the relevant image files
zip_selected_files(source_dir='plots',
                   filenames=(fnames_D + fnames_Sd),
                   destination_zip_path=f'results/AllMaps_{monthstring}_{endString}.zip')
