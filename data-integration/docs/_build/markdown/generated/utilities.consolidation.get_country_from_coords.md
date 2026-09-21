# utilities.consolidation.get_country_from_coords

### utilities.consolidation.get_country_from_coords(lat: [float](https://docs.python.org/3/builtins/functions.html#float), lon: [float](https://docs.python.org/3/builtins/functions.html#float), max_distance_km: [float](https://docs.python.org/3/builtins/functions.html#float) = 200.0) → [Tuple](https://docs.python.org/3/library/typing.html#typing.Tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None), [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None)]

Get ISO country name and code from latitude and longitude coordinates.

Resolution order:
: 1. reverse_geocode (requires internet, most accurate)
  2. Nearest coordinate in COUNTRY_LOOKUP (Haversine, offline)
  3. Bounding-box fallback for well-known African regions

Returns (None, None) if the coordinates are missing, non-numeric,
out of range, or cannot be resolved by any method.
