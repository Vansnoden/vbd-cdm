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
    # 'tp': {
    #     'column': 'rainfall_total',
    #     'convert': lambda m: m * 1000,  # meters to mm
    #     'agg_method': 'sum',
    #     'description': 'Total precipitation'
    # },
    # 'r': {
    #     'column': 'relative_humidity',
    #     'convert': None,
    #     'agg_method': 'mean',
    #     'description': 'Relative humidity'
    # },
}