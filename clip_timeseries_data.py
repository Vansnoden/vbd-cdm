#!-*-coding:utf8-*-

import os
import pandas as pd

from database.utils import batch_clip_era5

BASE_DIR = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets"
shapefile = "/mnt/monadworld/projects/phd/article_manuscripts/a_cdm_for_vector_of_vbds/data/datasets/Ghana/gha_admin_boundaries.shp/gha_admin1.shp"

print("\nBatch processing multiple GRIB files")
grib_files = [
    BASE_DIR + "/climatic_timeseries_data/2024_data.grib",
    BASE_DIR + "/climatic_timeseries_data/2025_data.grib"
]

batch_results = batch_clip_era5(
    grib_files=grib_files,
    shapefile_path=shapefile,
    filter_params=[
        {'column': 'adm1_name', 'value': 'Greater Accra'},
        # {'column': 'COUNTY', 'value': 'Siaya'}
    ],
    output_base_dir=BASE_DIR + '/clipped/batch_results'
)

# Print batch results summary
for input_file, output_files in batch_results.items():
    print(f"{os.path.basename(input_file)}: {len(output_files)} output files")