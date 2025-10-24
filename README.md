# Acestor Documentation

This directory contains the Sphinx documentation for the Acestor dengue modeling and prediction pipeline.

## Building the Documentation

### Install Dependencies

First, install the documentation dependencies:

```bash
uv sync --group docs
```

Or if you're using pip:

```bash
pip install sphinx furo
```

### Build HTML Documentation

Navigate to the `docs` directory and run:

```bash
cd docs
make html
```

The generated HTML documentation will be available in `docs/_build/html/index.html`.

### View the Documentation

Open the generated documentation in your browser:

```bash
open _build/html/index.html  # macOS
xdg-open _build/html/index.html  # Linux
start _build/html/index.html  # Windows
```

## Documentation Structure

- `conf.py` - Sphinx configuration file
- `index.rst` - Main documentation index
- `data_specification.rst` - Input case data specification
- `_static/` - Static files (images, CSS, etc.)
- `_build/` - Generated documentation (not committed to git)

## Adding New Pages

1. Create a new `.rst` file in the `docs/` directory
2. Add the file to the `toctree` in `index.rst`
3. Rebuild the documentation with `make html`
