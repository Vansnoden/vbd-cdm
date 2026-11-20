# utilities.consolidation.get_country_from_coords

### utilities.consolidation.get_country_from_coords(lat: [float](https://docs.python.org/3/builtins/functions.html#float), lon: [float](https://docs.python.org/3/builtins/functions.html#float)) → [Tuple](https://docs.python.org/3/library/typing.html#typing.Tuple)[[str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None), [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None)]

Get ISO country name and code from latitude and longitude coordinates.

Uses multiple methods in order of reliability:
1. reverse_geocode library (requires internet)
2. Nearest coordinate in COUNTRY_LOOKUP
3. Bounding box estimation for fallback

* **Parameters:**
  * **lat** – Latitude in decimal degrees
  * **lon** – Longitude in decimal degrees
* **Returns:**
  Tuple of (country_name, country_code) or (None, None) if not found
