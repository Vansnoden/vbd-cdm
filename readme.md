### VBD-CDM

A Common Data model for vector-borne diseases, attemping to combine various perspectives
systematically to better understand vectors and vector-borne diseases.

The project is composed of two components: data-integration, data-analysis

-   The data-integration component allows users to map thier data to a 
    standardize database, providing mean to integrate 16 data formats, namely:
    .tif, .dta, .xml, .xlsx, .sas7bdat, .nc, .json, .cog, .sav, .csv, .grib, .nc4, .shp, .geojson, .gpkg, .tsv.
    This component is implemented in Python (Python 3.10.20), usign FastAPI & Alembic with possibility to connected to either and SQLite database or a PostgreSQL database.
    A More detailed documentation of the integration component can be accessed [here](./data-integration/docs/_build/markdown/index.html).

-   The data-analysis component allows users to analyze the data available in the database
    provided by the data-integration component. This component is implemented in R, bundled
    and and RStudio project. A more detailed documentation of the component can be found [here](./data-analysis/docs/index.md).


Have Fun!
Submit an issue if identified.