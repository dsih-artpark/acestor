# Configuration file for the Sphinx documentation builder.

# -- Project information -----------------------------------------------------
project = "Acestor"
copyright = "2025, ARTPARK"
author = "Ashutosh Singhai"
release = "0.1.0"

# -- General configuration ---------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "myst_parser",
    "sphinx_design",
]

myst_enable_extensions = ["colon_fence", "deflist"]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Options for HTML output -------------------------------------------------
html_theme = "sphinx_book_theme"
html_static_path = ["_static"]
html_title = "Acestor Documentation"
html_logo = "_static/logo.png"

# Add custom JavaScript to open external links in new tab
html_js_files = [
    ("external_links.js", {"defer": "defer"}),
]

# -- Sphinx Book Theme options -----------------------------------------------
html_theme_options = {
    "repository_url": "https://github.com/dsih-artpark/acestor",
    "use_repository_button": True,
    "show_navbar_depth": 3,
    "show_toc_level": 2,
    "logo": {
        "image_light": "_static/logo.png",
        "image_dark": "_static/logo.png",
    },
}

# -- Intersphinx configuration -----------------------------------------------
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs", None),
    "numpy": ("https://numpy.org/doc/stable", None),
}
