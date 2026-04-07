Input Data Specification
=========================

The pipeline requires two types of input data:

1. **Case Data** — daily dengue case counts per region
2. **GeoJSON Files** — geographic boundary files for each region

1. Case Data Specification
============================

File Location
-------------

The case data file is read from the path configured under ``data.case_download.source_path`` in your YAML config.

File Format
-----------

A comma-separated values (CSV) file with the following columns:

.. list-table:: Case Data Columns
   :header-rows: 1
   :widths: 20 15 65

   * - Column
     - Type
     - Description
   * - ``region_id``
     - String
     - LGD Standard identifier in the format ``{region_type}_{lgd_code}`` (e.g. ``district_524``, ``zone_12``)
   * - ``date``
     - String
     - Date in ``YYYY-MM-DD`` format (e.g. ``2021-12-11``)
   * - ``case``
     - Float
     - Number of confirmed dengue cases for that region on that date

Example:

.. code-block:: text

    region_id,date,case
    district_524,2021-12-11,0.0
    district_524,2021-12-12,0.0
    district_524,2021-12-13,1.0
    district_525,2021-12-11,2.0
    district_525,2021-12-12,0.0

Data Requirements
-----------------

**Minimum data duration**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Data Duration
     - Pipeline Capability
   * - < configured ``min_date_span_days``
     - Pipeline exits at the sufficiency gate
   * - 4–12 months
     - Thresholds only (no predictions)
   * - ≥ 12 months
     - Full pipeline — thresholds and predictions

**Continuity requirements**

- Each ISO week must contain at least **4 days** of data
- Data must be continuous with no missing weeks
- All regions must have overlapping continuous data periods

**Identifiers**

- ``region_id`` values must exactly match the filenames of the GeoJSON boundary files

2. GeoJSON File Specification
==============================

Folder Structure
----------------

GeoJSON files must be organised under the ``data.geojson.base_path`` set in your config:

.. code-block:: text

    {base_path}/
    └── {region_type}s/
        ├── {region_type}_{lgd_code}.geojson
        └── ...

Example:

.. code-block:: text

    datasets/geojsons/
    └── districts/
        ├── district_524.geojson
        ├── district_525.geojson
        └── ...

Naming Convention
-----------------

Files follow the LGD Standard format: ``{region_type}_{lgd_code}.geojson``

- ``district_524.geojson`` — district with LGD code 524
- ``zone_12.geojson`` — zone with LGD code 12

The ``lgd_code`` must match the suffix in the ``region_id`` column of the case data.

File Format
-----------

Standard GeoJSON (RFC 7946) Feature:

.. code-block:: json

    {
      "id": "district_524",
      "type": "Feature",
      "properties": {
        "regionName": "BAGALKOTE",
        "regionType": "district",
        "parentID": "state_29",
        "parentName": "KARNATAKA",
        "Shape_Leng": 123.456,
        "Shape_Area": 789.012
      },
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[75.123, 15.456], [75.234, 15.567], [75.123, 15.456]]]
      }
    }

Required Properties
-------------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Property
     - Description
   * - ``regionName``
     - Name of the region (uppercase, LGD Standard)
   * - ``regionType``
     - Administrative type (e.g. ``district``, ``zone``)
   * - ``parentID``
     - Parent region identifier (e.g. ``state_29``)
   * - ``parentName``
     - Parent region name (e.g. ``KARNATAKA``)
   * - ``Shape_Leng``
     - Perimeter length (float)
   * - ``Shape_Area``
     - Area (float)

Additional Notes
----------------

- The top-level ``id`` field must match the filename (e.g. ``district_524.geojson`` → ``"id": "district_524"``)
- Coordinate system: WGS84 (EPSG:4326), pairs in ``[longitude, latitude]`` order
- Geometry types: ``Polygon`` or ``MultiPolygon``
