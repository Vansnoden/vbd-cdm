#!-*- coding: utf8 -*-


#!-*- coding: utf8 -*-

# Mapping from NetCDF variable name to database column and conversion
NC_VARIABLE_MAP = {
    't2m': {
        'column': 'temperature_mean',
        'convert': lambda k: k - 273.15,   # Kelvin to Celsius
        'agg_method': 'mean',
        'description': '2m temperature'
    },
    'tp': {
        'column': 'rainfall_total',
        'convert': lambda m: m * 1000,  # meters to mm
        'agg_method': 'sum',
        'description': 'Total precipitation'
    },
}

# Vector Atlas vector occurrence mapping
VECTOR_OBSERVATION_MAPPING = {
    '__latitude__': 'site_latitude',
    '__longitude__': 'site_longitude',
    '__year__': 'year_start',
    '__month__': 'month_start',
    '__count__': 'sample_occurrence_n_tot',
    'vector_species': 'recordedSpecies_species',
}

# GBIF vector observation mapping
GBIF_VECTOR_OBSERVATION_MAPPING = {
    '__latitude__': 'decimalLatitude',
    '__longitude__': 'decimalLongitude',
    '__year__': 'year',
    '__month__': 'month',
    '__count__': 'individualCount',
    'vector_species': 'scientificName',
}

# Case data mapping example
CASE_DATA_MAPPING = {
    'case_identifier': 'case_id',
    'location_code': 'district',
    'date_id_onset': 'onset_date',
    'date_id_reported': 'report_date',
    'disease': 'disease',
    'age_years': 'age',
    'sex': 'gender',
    'outcome': 'outcome',
    '__constants__': {
        'disease': 'malaria',
        'host_type': 'human',
    }
}