FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System dependencies for geospatial/scientific Python packages used by the
# dengue pipeline extras (geopandas, shapely, pyproj, etc.).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    proj-bin \
    proj-data \
    libspatialindex-dev \
    texlive-latex-base \
    texlive-latex-extra \
    texlive-fonts-recommended \
    && rm -rf /var/lib/apt/lists/*

# Copy metadata first to leverage Docker layer caching for dependency install.
COPY pyproject.toml uv.lock README.md ./

# Install uv and sync dependencies from lockfile (skip project itself for layer caching).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv sync --frozen --no-install-project --extra dengue --extra cds --extra s3

# Copy source then install the project.
COPY . .
RUN uv sync --frozen --extra dengue --extra cds --extra s3

CMD ["uv", "run", "python", "scripts/run_schedules.py"]
