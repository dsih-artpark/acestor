FROM python:3.10-slim

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
    && rm -rf /var/lib/apt/lists/*

# Copy metadata first to leverage Docker layer caching for dependency install.
COPY pyproject.toml uv.lock ./

# Install package + pipeline extras.
RUN pip install --upgrade pip && \
    pip install ".[dengue,cds,s3]"

# Copy source after dependencies.
COPY . .

# acestor.run expects --pipeline / --config / --run-id at runtime.
ENTRYPOINT ["python", "-m", "acestor.run"]
