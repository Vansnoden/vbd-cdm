# utilities.data_transform_library.tif_to_df_chunked

### utilities.data_transform_library.tif_to_df_chunked(fp: [str](https://docs.python.org/3/builtins/stdtypes.html#str), window_size: [int](https://docs.python.org/3/builtins/functions.html#int) = 512, nodata_threshold: [float](https://docs.python.org/3/builtins/functions.html#float) = -9999.0) → [Generator](https://docs.python.org/3/library/typing.html#typing.Generator)[DataFrame, [None](https://docs.python.org/3/builtins/constants.html#None), [None](https://docs.python.org/3/builtins/constants.html#None)]

Generator that yields DataFrames for each block of a GeoTIFF.
Filters out NoData values (<= nodata_threshold).
Yields DataFrames with columns: ‘x’, ‘y’, ‘value’ (single band).
