# Configuration file for the Sphinx documentation builder.

# -- Project information -----------------------------------------------------
project = 'Acestor'
copyright = '2025, ARTPARK'
author = 'Tarun Khandelwal, Sai Sneha'
release = '0.1.0'

# -- General configuration ---------------------------------------------------
extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.intersphinx',
]

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

# -- Options for HTML output -------------------------------------------------
html_theme = 'furo'
html_static_path = ['_static']
html_title = 'Acestor Documentation'
html_logo = '_static/logo.png'

# -- Furo theme options ------------------------------------------------------
html_theme_options = {
    "sidebar_hide_name": False,
}

# -- Intersphinx configuration -----------------------------------------------
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'pandas': ('https://pandas.pydata.org/docs', None),
    'numpy': ('https://numpy.org/doc/stable', None),
}
