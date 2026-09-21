# utilities.data_transform_library.timeseries_to_df

### utilities.data_transform_library.timeseries_to_df(fp: [str](https://docs.python.org/3/builtins/stdtypes.html#str), variables: [List](https://docs.python.org/3/library/typing.html#typing.List)[[str](https://docs.python.org/3/builtins/stdtypes.html#str)] | [None](https://docs.python.org/3/builtins/constants.html#None) = None, warn_large: [bool](https://docs.python.org/3/builtins/functions.html#bool) = True, max_expected_rows: [int](https://docs.python.org/3/builtins/functions.html#int) = 10000000) → DataFrame

Extract all time steps for given variables into a single DataFrame.
WARNING: Only use for small to medium files. For large files, use timeseries_to_df_chunked.

* **Parameters:**
  * **fp** – Path to NetCDF or GRIB file
  * **variables** – List of variable names (None = all variables)
  * **warn_large** – If True, show warning when estimated rows exceed max_expected_rows
  * **max_expected_rows** – Threshold for warning (default 10 million)
* **Returns:**
  Single DataFrame with all time steps and variables (long format)
