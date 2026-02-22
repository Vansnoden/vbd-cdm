#!-*- coding: utf8 -*-


# Mapping from NetCDF variable name to database column and conversion
NC_VARIABLE_MAP = {
    't2m': {
        'column': 'temperature_mean',
        'convert': lambda k: k - 273.15,   # Kelvin to Celsius
        'agg_method': 'mean',
        'description': '2m temperature'
    },
    # 'mx2t': {
    #     'column': 'temperature_max',
    #     'convert': lambda k: k - 273.15,
    #     'agg_method': 'max',
    #     'description': 'Maximum temperature at 2m'
    # },
    # 'mn2t': {
    #     'column': 'temperature_min',
    #     'convert': lambda k: k - 273.15,
    #     'agg_method': 'min',
    #     'description': 'Minimum temperature at 2m'
    # },
    'tp': {
        'column': 'rainfall_total',
        'convert': lambda m: m * 1000,  # meters to mm
        'agg_method': 'sum',
        'description': 'Total precipitation'
    },
    # 'r': {
    #     'column': 'relative_humidity',
    #     'convert': None,
    #     'agg_method': 'mean',
    #     'description': 'Relative humidity'
    # },
}



VECTOR_OBSERVATION_MAPPING = {
    # Special keys (prefixed with '__') control processing
    '__latitude__': 'site_latitude',           # column with latitude
    '__longitude__': 'site_longitude',         # column with longitude
    '__year__': 'year_start',                   # column with year
    '__month__': 'month_start',                 # column with month
    # Count can be a single column or a list of columns to sum
    '__count__': 'sample_occurrence_n_tot',     # main count column
    # If you need to sum multiple columns, use a list:
    # '__count__': ['sample_occurrence_n_1', 'sample_occurrence_n_2', 'sample_occurrence_n_3', 'sample_occurrence_n_4'],

    # Direct mappings to VectorObservation fields (required fields must be provided)
    'vector_species': 'recordedSpecies_species',
    # Optional fields – include only if your CSV has them
    # 'life_stage': 'life_stage_column_name',
    # 'collection_method': 'collection_method_column',
    # 'tested_for_pathogen': 'tested_for_pathogen_column',
    # 'infection_status': 'infection_status_column',
    # 'infection_rate': 'infection_rate_column',
}