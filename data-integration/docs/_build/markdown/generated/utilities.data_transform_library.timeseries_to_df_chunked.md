# utilities.data_transform_library.timeseries_to_df_chunked

### utilities.data_transform_library.timeseries_to_df_chunked(fp: [str](https://docs.python.org/3/builtins/stdtypes.html#str), variables: [List](https://docs.python.org/3/library/typing.html#typing.List)[[str](https://docs.python.org/3/builtins/stdtypes.html#str)] | [None](https://docs.python.org/3/builtins/constants.html#None) = None, time_chunk_size: [int](https://docs.python.org/3/builtins/functions.html#int) = 100, output_csv: [str](https://docs.python.org/3/builtins/stdtypes.html#str) | [None](https://docs.python.org/3/builtins/constants.html#None) = None)

Process timeseries data in chunks along the time dimension.

* **Parameters:**
  * **fp** – Path to NetCDF or GRIB file
  * **variables** – List of variable names (None = all variables)
  * **time_chunk_size** – Number of time steps per chunk
  * **output_csv** – If provided, write all chunks to this CSV file (no DataFrame returned).
    If None, yields one DataFrame per chunk.
* **Yields:**
  DataFrame for each time chunk (if output_csv is None)
* **Returns:**
  bool (if output_csv provided) indicating success
