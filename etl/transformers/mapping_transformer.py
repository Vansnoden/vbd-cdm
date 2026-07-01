"""
MappingTransformer - Interprets CSV mapping files to transform source DataFrames
into CDM table-specific DataFrames.
"""

from datetime import datetime

import pandas as pd
import json
from typing import Dict, List, Any, Optional, Union
from collections import defaultdict
import logging

from database import models
from database.session import SessionLocal 

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
        self._dimension_cache = {}

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

    def _apply_map_transform(
        self, 
        value: Any, 
        params: Optional[Union[str, dict]],
        extra_context: Optional[dict] = None) -> Any:
        # Handle dimension lookups with ref parameter
        ref = params.get('ref') if params else None
        if ref:
            # Parse ref: "dim_location.name" or "dim_location.location_name"
            parts = ref.split('.')
            if len(parts) == 2:
                table_name, field_name = parts[0], parts[1]
                # Get the model class
                model_class = self._get_model_for_table(table_name)
                if model_class:
                    # Handle composite lookups from extra_context
                    lookup_value = self._build_lookup_value(value, params, extra_context)
                    return self._lookup_dimension_id(model_class, field_name, lookup_value)
                else:
                    logger.warning(f"Model not found for table: {table_name}")
                    return value
            else:
                logger.warning(f"Invalid ref format: {ref}. Expected 'table.field'")
                return value
        else:
            # Simple mapping (dict lookup)
            mapping = params or {}
            return mapping.get(str(value), value)
    
    def _apply_date_parse_transform(
        self,
        value: Any,
        params: Optional[Union[str, dict]]) -> Any:
        """
        Parse date value and return as 'YYYY-MM-DD' string.
        Handles Timestamp, datetime, and strings (with/without time).
        """
        fmt = params.get('format', '%Y-%m-%d') if params else '%Y-%m-%d'
        try:
            # Already datetime-like → format directly
            if isinstance(value, (pd.Timestamp, datetime)):
                return value.strftime('%Y-%m-%d')
            # Try exact format first, fallback to flexible parser
            try:
                dt = pd.to_datetime(value, format=fmt)
            except (ValueError, TypeError):
                dt = pd.to_datetime(value)   # infers time, etc.
            return dt.strftime('%Y-%m-%d')
        except Exception as e:
            logger.warning(f"Date parse failed for '{value}' with format '{fmt}': {e}")
            return None
                
    def _apply_cast_transform(
        self, 
        value: Any, 
        params: Optional[Union[str, dict]]) -> Any:
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
        
    def _apply_split_transform(
        self, 
        value: Any, 
        params: Optional[Union[str, dict]]) -> Any:
        delimiter = params.get('delimiter', ',')
        index = params.get('index', 0)
        parts = str(value).split(delimiter)
        if index < len(parts):
            return parts[index].strip()
        logger.warning(f"Split index {index} out of range for value '{value}'")
        return None
    
    def _apply_unit_convert_transform(
        self, 
        value: Any, 
        params: Optional[Union[str, dict]]) -> Any:
        factor = params.get('factor', 1.0)
        offset = params.get('offset', 0.0)
        try:
            return float(value) * factor + offset
        except Exception:
            logger.warning(f"Unit conversion failed for value '{value}'")
            return None
        
    def _apply_format_date_transform(
        self, 
        value: Any, 
        params: Optional[Union[str, dict]],
        extra_context: Optional[dict] = None) -> Any:
        cols = params.get('columns', [])
        if len(cols) != 3:
            logger.warning(f"format_date requires exactly 3 columns, got {cols}")
            return None
        try:
            year = int(float(extra_context.get(cols[0], 0))) if extra_context.get(cols[0]) is not None else 0
            month = int(float(extra_context.get(cols[1], 0))) if extra_context.get(cols[1]) is not None else 0
            day = int(float(extra_context.get(cols[2], 0))) if extra_context.get(cols[2]) is not None else 0
            logger.info(f"format_date requires exactly 3 columns, got {cols} - extracted Year: {year}, Month: {month}; Day: {day}")
            if year == 0 or month == 0 or day == 0:
                return params.get('default')
            return f"{year:04d}-{month:02d}-{day:02d}"
        except (ValueError, TypeError):
            return params.get('default')

    def _apply_transform(
        self,
        value: Any,
        transform: str,
        params: Optional[Union[str, dict]],
        extra_context: Optional[dict] = None) -> Any:
        """
        Apply a single transformation to a value.

        Args:
            value: The input value (may be None or NaN).
            transform: Transformation type ('none', 'map', 'date_parse', 'cast', 'split', 'unit_convert', 'format_date').
            params: Optional parameters (dict or, for cast, a string type name).
            extra_context: Additional context for lookups (e.g., other columns from the row)
        Returns:
            Transformed value, or None if transformation fails.
        """
        # Normalise params: if a string is given for cast, treat as {'type': params}
        if isinstance(params, str):
            if params.strip() == '':
                params = {}
            elif transform == 'cast':
                params = {'type': params}
            else:
                params = {}
        elif params is None:
            params = {}
        # For transforms that don't require a raw value, skip the None check.
        if transform not in ('format_date', 'map', 'concat'):
            if pd.isna(value) or value is None:
                return None
        if transform == 'none':
            return value
        elif transform == 'map':
            return self._apply_map_transform(value, params, extra_context)
        elif transform == 'date_parse':
            return self._apply_date_parse_transform(value, params)
        elif transform == 'cast':
            return self._apply_cast_transform(value, params)
        elif transform == 'split':
            return self._apply_split_transform(value, params)
        elif transform == 'unit_convert':
            return self._apply_unit_convert_transform(value, params)
        elif transform == 'format_date':
            return self._apply_format_date_transform(value, params, extra_context)
        else:
            logger.warning(f"Unknown transform type: '{transform}'")
            return value
        
    def _get_model_for_table(self, table_name: str):
        """Get the SQLAlchemy model class for a given table name."""
        # Map table names to model classes
        model_map = {
            'dim_location': models.DimLocation,
            'dim_time': models.DimTime,
            'dim_disease': models.DimDisease,
            'dim_vector': models.DimVector,
            'dim_insecticide': models.DimInsecticide,
            'dim_identification_method': models.DimIdentificationMethod,
            'dim_resistance_test_type': models.DimResistanceTestType,
            'dim_socioeconomic_indicator': models.DimSocioeconomicIndicator,
            'dim_source': models.DimSource,
        }
        return model_map.get(table_name)
    
    def _build_lookup_value(self, original_value: Any, params: dict, extra_context: Optional[dict]) -> Any:
        """
        Build the lookup value using the source column value and optionally other columns.
        
        Args:
            original_value: The value from the source column
            params: Transform parameters (may contain 'columns' for composite lookups)
            extra_context: Additional row data for composite lookups
        
        Returns:
            The value to use for the lookup
        """
        # Check if we need to combine multiple columns for uniqueness
        columns = params.get('columns')
        if columns and extra_context:
            # Combine multiple columns
            parts = []
            for col in columns:
                if col in extra_context and pd.notna(extra_context[col]):
                    parts.append(str(extra_context[col]))
            if parts:
                # Use separator or default to '/'
                separator = params.get('separator', '/')
                return separator.join(parts)
        
        # Single column lookup
        return original_value
    
    def _lookup_dimension_id(self, model_class, field_name: str, lookup_value: Any) -> Optional[int]:
        """
        Look up the primary key ID from a dimension table.
        
        Args:
            model_class: SQLAlchemy model class
            field_name: The field to match (e.g., 'name', 'code')
            lookup_value: The value to search for
        
        Returns:
            The ID (primary key) of the matching record, or None if not found
        """
        if lookup_value is None or (isinstance(lookup_value, float) and pd.isna(lookup_value)):
            return None
        # Check cache first
        cache_key = f"{model_class.__tablename__}_{field_name}_{lookup_value}"
        if cache_key in self._dimension_cache:
            return self._dimension_cache[cache_key]
        try:
            with SessionLocal() as session:
                # Build query dynamically
                query = session.query(model_class)
                # Handle different field types (string vs numeric)
                if isinstance(lookup_value, str):
                    query = query.filter(getattr(model_class, field_name).ilike(lookup_value))
                else:
                    query = query.filter(getattr(model_class, field_name) == lookup_value)
                result = query.first()
                if result:
                    # Get the primary key
                    pk_column = model_class.__table__.primary_key.columns.values()[0]
                    pk_value = getattr(result, pk_column.name)
                    self._dimension_cache[cache_key] = pk_value
                    return pk_value
                else:
                    logger.warning(f"No match found for {model_class.__tablename__}.{field_name} = '{lookup_value}'")
                    return None
        except Exception as e:
            logger.error(f"Error during dimension lookup: {e}")
            return None
        
    def _determine_raw_value(self, source_row, target_table, mapping) -> Any:
        value = None
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
        return value
    
    def _apply_transformation(self, idx, raw_value, source_row, mapping, row_context):
        value = raw_value
        transform = mapping['transform']
        params = None
        if pd.notna(mapping.get('transform_params')):
            params_str = mapping['transform_params']
            # Skip if the string is empty or only whitespace
            if params_str and params_str.strip():
                try:
                    params = json.loads(params_str)
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON in transform_params: '{params_str}' - {e}")
                    params = {}
            else:
                params = {}
        if transform == 'concat':
            cols = params.get('columns', []) if params else []
            sep = params.get('separator', ' ') if params else ' '
            parts = []
            for col in cols:
                if col in source_row.index and pd.notna(source_row[col]):
                    parts.append(str(source_row[col]))
                else:
                    logger.warning(f"Concat: column '{col}' missing or null in row {idx}")
            value = sep.join(parts) if parts else mapping.get('default_value')
        else:
            value = self._apply_transform(raw_value, transform, params, extra_context=row_context)
        return value

    def _apply_default_transform(self, value, mapping, target_table):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            if mapping.get('required', False):
                raise ValueError(
                    f"Required field '{mapping['target_column']}' in table '{target_table}' has null value"
                )
            value = mapping.get('default_value')
        return value

    def _build_table_df(self, source_df: pd.DataFrame, target_table: str) -> pd.DataFrame:
        """
        Build a DataFrame for a single target table using its mapping rows.
        """
        rows = []
        for idx, source_row in source_df.iterrows():
            record = {}
            # Convert the entire row to dict for extra context
            row_context = source_row.to_dict()
            for mapping in self.table_mappings[target_table]:
                # 1. Determine raw value
                value = self._determine_raw_value(source_row, target_table, mapping)
                # 2. Apply transformation
                value = self._apply_transformation(idx, value, source_row, mapping, row_context)
                # 3. Apply default if still missing and not required
                value = self._apply_default_transform(value, mapping, target_table)
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