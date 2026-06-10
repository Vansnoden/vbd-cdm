"""
MappingTransformer - Interprets CSV mapping files to transform source DataFrames
into CDM table-specific DataFrames.
"""

import pandas as pd
import json
from typing import Dict, List, Any, Optional, Union
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


class MappingTransformer:
    """
    Transforms a source DataFrame (from any file format) into multiple CDM table DataFrames
    using a mapping CSV that defines source -> target column mappings.
    """

    def __init__(self, mapping_csv_path: str, large_df_threshold: int = 1_000_000):
        """
        Args:
            mapping_csv_path: Path to the mapping CSV file.
            large_df_threshold: Number of rows above which a warning is logged.
        """
        self.mapping = pd.read_csv(mapping_csv_path, quotechar='"')
        self.large_df_threshold = large_df_threshold
        self._validate_mapping()
        self._group_by_target_table()

    def _validate_mapping(self):
        required_cols = {'source_column', 'target_table', 'target_column', 'transform', 'required'}
        missing = required_cols - set(self.mapping.columns)
        if missing:
            raise ValueError(f"Mapping CSV missing columns: {missing}")

    def _group_by_target_table(self):
        """Group mapping rows by target table name."""
        self.table_mappings = defaultdict(list)
        for _, row in self.mapping.iterrows():
            self.table_mappings[row['target_table']].append(row.to_dict())

    def _apply_transform(self, value: Any, transform: str, params: Optional[Union[str, dict]]) -> Any:
        """
        Apply a single transformation to a value.

        Args:
            value: The input value (may be None or NaN).
            transform: Transformation type ('none', 'map', 'date_parse', 'cast', 'split', 'unit_convert').
            params: Optional parameters (dict or, for cast, a string type name).

        Returns:
            Transformed value, or None if transformation fails.
        """
        if pd.isna(value) or value is None:
            return None

        # Normalise params: if a string is given for cast, treat as {'type': params}
        if isinstance(params, str):
            if transform == 'cast':
                params = {'type': params}
            else:
                params = {}

        if transform == 'none':
            return value

        elif transform == 'map':
            mapping = params or {}
            return mapping.get(str(value), value)

        elif transform == 'date_parse':
            fmt = params.get('format', '%Y-%m-%d') if params else '%Y-%m-%d'
            try:
                return pd.to_datetime(value, format=fmt).date()
            except Exception:
                logger.warning(f"Date parse failed for '{value}' with format '{fmt}'")
                return None

        elif transform == 'cast':
            target_type = params.get('type', 'str')
            try:
                if target_type == 'int':
                    return int(float(value))
                elif target_type == 'float':
                    return float(value)
                elif target_type == 'str':
                    return str(value)
                else:
                    return value
            except (ValueError, TypeError):
                logger.warning(f"Cast to {target_type} failed for value '{value}'")
                return None

        elif transform == 'split':
            delimiter = params.get('delimiter', ',')
            index = params.get('index', 0)
            parts = str(value).split(delimiter)
            if index < len(parts):
                return parts[index].strip()
            logger.warning(f"Split index {index} out of range for value '{value}'")
            return None

        elif transform == 'unit_convert':
            factor = params.get('factor', 1.0)
            offset = params.get('offset', 0.0)
            try:
                return float(value) * factor + offset
            except Exception:
                logger.warning(f"Unit conversion failed for value '{value}'")
                return None

        else:
            logger.warning(f"Unknown transform type: '{transform}'")
            return value

    def _build_table_df(self, source_df: pd.DataFrame, target_table: str) -> pd.DataFrame:
        """
        Build a DataFrame for a single target table using its mapping rows.
        """
        rows = []
        for idx, source_row in source_df.iterrows():
            record = {}
            for mapping in self.table_mappings[target_table]:
                # 1. Determine raw value
                if pd.notna(mapping.get('static_value')):
                    value = mapping['static_value']
                else:
                    src_col = mapping['source_column']
                    if src_col not in source_row.index:
                        if mapping.get('required', False):
                            raise ValueError(
                                f"Required source column '{src_col}' missing in data (table {target_table})"
                            )
                        value = mapping.get('default_value')
                    else:
                        value = source_row[src_col]
                        # Optional source data type casting
                        if pd.notna(mapping.get('source_data_type')):
                            dtype = mapping['source_data_type']
                            try:
                                if dtype == 'int':
                                    value = int(float(value)) if pd.notna(value) else None
                                elif dtype == 'float':
                                    value = float(value) if pd.notna(value) else None
                                elif dtype == 'str':
                                    value = str(value) if pd.notna(value) else None
                            except (ValueError, TypeError):
                                value = None

                # 2. Apply transformation
                transform = mapping['transform']
                params = None
                if pd.notna(mapping.get('transform_params')):
                    params = json.loads(mapping['transform_params'])

                # Special handling for concat (needs multiple columns from the row)
                if transform == 'concat':
                    cols = params.get('columns', [])
                    sep = params.get('separator', ' ')
                    parts = []
                    for col in cols:
                        if col in source_row.index and pd.notna(source_row[col]):
                            parts.append(str(source_row[col]))
                        else:
                            logger.warning(f"Concat: column '{col}' missing or null in row {idx}")
                    value = sep.join(parts) if parts else mapping.get('default_value')
                else:
                    value = self._apply_transform(value, transform, params)

                # 3. Apply default if still missing and not required
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    if mapping.get('required', False):
                        raise ValueError(
                            f"Required field '{mapping['target_column']}' in table '{target_table}' has null value"
                        )
                    value = mapping.get('default_value')

                record[mapping['target_column']] = value

            rows.append(record)

        return pd.DataFrame(rows)

    def transform(self, source_df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        """
        Transform source DataFrame into a dictionary of table -> DataFrame.

        Args:
            source_df: Source DataFrame (e.g., from FileExtractor).

        Returns:
            Dictionary where keys are target table names, values are DataFrames ready for loading.
        """
        # Warn if source is large (row-wise processing may be slow)
        if len(source_df) > self.large_df_threshold:
            logger.warning(
                f"Source DataFrame has {len(source_df):,} rows (> {self.large_df_threshold:,}). "
                "Row-wise transformation may be slow. Consider chunking the source data."
            )

        result = {}
        for target_table in self.table_mappings.keys():
            df = self._build_table_df(source_df, target_table)
            df = df.dropna(how='all')  # drop rows that are completely empty
            if not df.empty:
                result[target_table] = df
                logger.info(f"Built {len(df)} rows for table '{target_table}'")
        return result