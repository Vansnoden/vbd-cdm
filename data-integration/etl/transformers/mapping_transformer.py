"""
MappingTransformer - Interprets CSV mapping files to transform source DataFrames
into CDM table-specific DataFrames.
"""

from datetime import datetime
import calendar
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
        params: Optional[Union[str, dict]],
    ) -> Any:
        """
        Affine unit conversion: out = (value / factor) + offset   (divide=True)
                                out = (value * factor) + offset   (divide=False)

        Params
        ------
        factor : float
            Multiplicative scale. Default 1.0.
        offset : float
            Additive offset applied after the scale. Default 0.0.
        divide : bool
            When True, divide by `factor` instead of multiplying. Useful for
            reciprocal-style conversions where expressing the scale as a
            multiplier would be awkward (e.g. 1 / 0.01). Default False.
        default : Any
            Value returned when the input cannot be parsed. If omitted, the
            function returns None, which the caller's default-value logic
            then handles. Default None.
        clamp_min, clamp_max : float | None
            Optional bounds applied after conversion. Typical use: clamp a
            proportion into [0, 1] after multiplying a percentage by 0.01.
        round_to : int | None
            Optional number of decimal places to round to. Useful for
            converting to integer-valued units (e.g. minutes).
        treat_as_null : list[str]
            Additional strings (beyond the defaults) to treat as null.
            Case-insensitive.

        Returns
        -------
        float | default
            The converted value, or `default` if parsing failed.
        """
        if not params:
            params = {}

        factor    = float(params.get("factor", 1.0))
        offset    = float(params.get("offset", 0.0))
        divide    = bool(params.get("divide", False))
        default   = params.get("default")            # None is a valid value here
        clamp_min = params.get("clamp_min")
        clamp_max = params.get("clamp_max")
        round_to  = params.get("round_to")

        null_tokens = {"", "NA", "N/A", "NONE", "NULL", "NAN"}
        extra_nulls = params.get("treat_as_null") or []
        if isinstance(extra_nulls, str):
            extra_nulls = [extra_nulls]
        null_tokens.update(str(x).strip().upper() for x in extra_nulls)

        # ---- Guard the divide case ----------------------------------------
        if divide and factor == 0:
            logger.warning(
                "unit_convert: divide=True with factor=0 is undefined; "
                "returning default"
            )
            return default

        # ---- Null / sentinel handling -------------------------------------
        if value is None:
            return default

        if isinstance(value, float) and pd.isna(value):
            return default

        if isinstance(value, str):
            s = value.strip()
            if not s or s.upper() in null_tokens:
                return default
            value = s  # keep the trimmed string for float() below

        # ---- Numeric coercion ---------------------------------------------
        try:
            v = float(value)
        except (ValueError, TypeError):
            logger.warning(f"unit_convert: could not parse {value!r}")
            return default

        if pd.isna(v):
            return default

        # ---- Apply the affine transform -----------------------------------
        v = (v / factor) if divide else (v * factor)
        v = v + offset

        # ---- Optional bounds ----------------------------------------------
        if clamp_min is not None:
            v = max(v, float(clamp_min))
        if clamp_max is not None:
            v = min(v, float(clamp_max))

        # ---- Optional rounding --------------------------------------------
        if round_to is not None:
            v = round(v, int(round_to))

        return v


    def _apply_coalesce_transform(
        self,
        value: Any,
        params: Optional[Union[str, dict]],
        extra_context: Optional[dict] = None,
    ) -> Any:
        """
        Return the first non-null value among a list of source columns.

        Useful when a source stores the same logical quantity in two
        mutually-exclusive columns — e.g. IRMapper files 4 and 5, where the
        concentration appears in either `Concentration (%)` (WHO tube) or
        `Concentration (µg)` (CDC bottle), never both in the same row.

        Params
        ------
        columns : list[str]
            Column names to try, in priority order. The first column with a
            usable value wins.
        default : Any
            Value returned when every column is empty or unusable.
            Default None.
        treat_as_null : list[str]
            Extra strings to treat as null (case-insensitive, whitespace
            trimmed). The defaults are already recognised:
            "", "NA", "N/A", "NONE", "NULL", "NAN", "NR".

        Returns
        -------
        Any
            The first usable value, or `default`.

        Notes
        -----
        A value is "usable" if it is not None, not NaN, and — for strings —
        does not reduce to a recognised null token. Numeric zero IS usable;
        the transform deliberately does not treat 0 as missing.
        """
        if not params:
            params = {}

        cols    = params.get("columns", [])
        default = params.get("default")

        if not cols or not isinstance(cols, list):
            logger.warning(
                f"coalesce: 'columns' must be a non-empty list, got {cols!r}"
            )
            return default

        if not extra_context:
            return default

        null_tokens = {"", "NA", "N/A", "NONE", "NULL", "NAN", "NR"}
        extra_nulls = params.get("treat_as_null") or []
        if isinstance(extra_nulls, str):
            extra_nulls = [extra_nulls]
        null_tokens.update(str(x).strip().upper() for x in extra_nulls)

        for col in cols:
            if col not in extra_context:
                continue
            v = extra_context[col]
            if v is None:
                continue
            if isinstance(v, float) and pd.isna(v):
                continue
            if isinstance(v, str) and v.strip().upper() in null_tokens:
                continue
            return v

        return default    
        
    
    def _apply_format_date_transform(
        self,
        value: Any,
        params: Optional[Union[str, dict]],
        extra_context: Optional[dict] = None,
    ) -> Any:
        """
        Build a YYYY-MM-DD date_id string from one to three source columns.

        Because `dim_time.date_id` is a full-date primary key, every fact row
        must carry a complete YYYY-MM-DD string. Sources that only know the
        year (or year + month) are still accommodated: the missing components
        are filled from configurable defaults, and whatever precision was
        available is preserved in the returned date.

        Params
        ------
        columns : list[str]
            One to three column names, ordered [year, month, day]. Only the
            first entry (year) is required. Missing trailing entries trigger
            the defaults below.
        default : str | None
            Fallback date_id string, returned when the year is missing or
            out of range. Typically "1970-01-01" or "1900-01-01".
        month_default : int
            Month to use when the month column is absent or unparseable.
            Default 1 (January).
        day_default : int
            Day to use when the day column is absent or unparseable.
            Default 1.
        min_year, max_year : int
            Bounds on the accepted year. Values outside the range fall back
            to `default`. Defaults 1800 and 2200.
        clamp_invalid_day : bool
            When True (the default), a day that is out of range for the
            given month is clamped to the last valid day of that month
            (e.g. Feb 30 -> Feb 28/29). When False, the row falls back to
            `default` instead.

        Returns
        -------
        str | None
            A YYYY-MM-DD date_id string, or `default` if the year could not
            be resolved.
        """
        if not params:
            params = {}

        cols = params.get("columns", [])
        if not isinstance(cols, list) or not (1 <= len(cols) <= 3):
            logger.warning(
                f"format_date: 'columns' must be a list of 1-3 names, got {cols!r}"
            )
            return params.get("default")

        default          = params.get("default")
        month_default    = int(params.get("month_default", 1))
        day_default      = int(params.get("day_default", 1))
        min_year         = int(params.get("min_year", 1800))
        max_year         = int(params.get("max_year", 2200))
        clamp_invalid    = bool(params.get("clamp_invalid_day", True))

        # ---- Internal: coerce a raw cell to a positive integer, or None ----
        def _as_int(raw):
            if raw is None:
                return None
            if isinstance(raw, float) and pd.isna(raw):
                return None
            if isinstance(raw, str):
                s = raw.strip()
                if not s or s.upper() in ("NA", "N/A", "NONE", "NULL", "NAN"):
                    return None
                try:
                    return int(float(s))
                except (ValueError, TypeError):
                    return None
            try:
                return int(float(raw))
            except (ValueError, TypeError):
                return None

        # ---- Year (mandatory) ---------------------------------------------
        raw_year = extra_context.get(cols[0]) if extra_context else None
        year = _as_int(raw_year)
        if year is None or not (min_year <= year <= max_year):
            logger.debug(
                f"format_date: unusable year {raw_year!r}; using default {default!r}"
            )
            return default

        # ---- Month (optional) ---------------------------------------------
        month = month_default
        if len(cols) >= 2:
            raw_month = extra_context.get(cols[1]) if extra_context else None
            m = _as_int(raw_month)
            if m is not None and 1 <= m <= 12:
                month = m
            elif m is not None:
                logger.debug(
                    f"format_date: month {m} out of range; using {month_default}"
                )

        # ---- Day (optional) -----------------------------------------------
        day = day_default
        if len(cols) >= 3:
            raw_day = extra_context.get(cols[2]) if extra_context else None
            d = _as_int(raw_day)
            if d is not None and 1 <= d <= 31:
                day = d
            elif d is not None:
                logger.debug(
                    f"format_date: day {d} out of range; using {day_default}"
                )

        # ---- Assemble and validate ----------------------------------------
        # datetime() raises ValueError on impossible dates (Feb 30, Apr 31, ...).
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            if not clamp_invalid:
                logger.debug(
                    f"format_date: invalid date {year}-{month:02d}-{day:02d}; "
                    f"using default {default!r}"
                )
                return default
            # Clamp the day to the last valid day of the month.
            last_day = calendar.monthrange(year, month)[1]
            clamped = datetime(year, month, last_day)
            logger.debug(
                f"format_date: {year}-{month:02d}-{day:02d} invalid; "
                f"clamped to {clamped.strftime('%Y-%m-%d')}"
            )
            return clamped.strftime("%Y-%m-%d")


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
        if transform not in ('format_date', 'map', 'concat', 'coalesce'):
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
        elif transform == 'coalesce':
            return self._apply_coalesce_transform(value, params, extra_context)
        else:
            logger.warning(f"Unknown transform type: '{transform}'")
            return value
        
    def _get_model_for_table(self, table_name: str):
        """Get the SQLAlchemy model class for a given table name."""
        # Map table names to model classes
        model_map = {
            'dim_vocabulary': models.DimVocabulary,
            'dim_concept': models.DimConcept,
            'dim_location': models.DimLocation,
            'dim_time': models.DimTime,
            'dim_disease': models.DimDisease,
            'dim_vector': models.DimVector,
            'dim_insecticide': models.DimInsecticide,
            'dim_identification_method': models.DimIdentificationMethod,
            'dim_resistance_test_type': models.DimResistanceTestType,
            'dim_socioeconomic_indicator': models.DimSocioeconomicIndicator,
            'dim_source': models.DimSource,
            'fact_concept_map': models.FactConceptMap,
            'fact_vector_observation': models.FactVectorObservation,
            'fact_disease_case': models.FactDiseaseCase,
            'fact_insecticide_resistance': models.FactInsecticideResistance,
            'fact_bionomics': models.FactBionomics,
            'fact_intervention': models.FactIntervention,
            'fact_socioeconomic_condition': models.FactSocioeconomicCondition,
            'fact_health_facility': models.FactHealthFacility,
            'fact_disease_burden': models.FactDiseaseBurden,
            'fact_environment': models.FactEnvironment,
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
                # if isinstance(lookup_value, str):
                #     query = query.filter(getattr(model_class, field_name).ilike(lookup_value))
                # else:
                #     query = query.filter(getattr(model_class, field_name) == lookup_value)
                query = query.filter(getattr(model_class, field_name) == lookup_value)
                result = query.first()
                if result:
                    # Get the primary key
                    pk_column = model_class.__table__.primary_key.columns.values()[0]
                    pk_value = getattr(result, pk_column.name)
                    self._dimension_cache[cache_key] = pk_value
                    logger.info(f"Looked up for PK Column {pk_column} with {field_name} == {lookup_value} in model {model_class}, Result ID: {pk_value}")
                    return pk_value
                else:
                    logger.warning(f"No match found for {model_class.__tablename__}.{field_name} = '{lookup_value}'")
                    return None
        except Exception as e:
            logger.error(f"Error during dimension lookup: {e}")
            return None
        

    def _determine_raw_value(self, source_row, target_table, mapping) -> Any:
        value = None

        # 1. Static value is the value itself; no source column needed.
        if pd.notna(mapping.get('static_value')):
            return mapping['static_value']

        # 2. Empty source_column → defer to the transform.
        src_col = mapping.get('source_column')
        if src_col is None or (isinstance(src_col, float) and pd.isna(src_col)):
            return None

        # 3. Otherwise read from the source row.
        if src_col not in source_row:
            if mapping.get('required', False):
                raise ValueError(
                    f"Required source column '{src_col}' missing in data (table {target_table})"
                )
            value = mapping.get('default_value')
        else:
            value = source_row[src_col]
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
                if col in row_context and pd.notna(row_context[col]):
                    parts.append(str(row_context[col]))
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
        rows = []
        mappings = self.table_mappings[target_table]
        records = source_df.to_dict(orient='records')      # once, not per row

        for idx, row_context in enumerate(records):
            record = {}
            for mapping in mappings:
                value = self._determine_raw_value(row_context, target_table, mapping)
                value = self._apply_transformation(idx, value, row_context, mapping, row_context)
                value = self._apply_default_transform(value, mapping, target_table)
                record[mapping['target_column']] = value
            rows.append(record)
        return pd.DataFrame(rows)


    def transform(self, source_df: pd.DataFrame,
              target_filter: Optional[set] = None) -> Dict[str, pd.DataFrame]:
        """
        Transform source DataFrame into a dictionary of table -> DataFrame.

        Args:
            source_df:      Source DataFrame.
            target_filter:  If provided, only build DataFrames for these target
                            tables. Used to keep dimension-only and fact-only
                            transform passes separated.
        """
        if len(source_df) > self.large_df_threshold:
            logger.warning(
                f"Source DataFrame has {len(source_df):,} rows (> {self.large_df_threshold:,}). "
                "Row-wise transformation may be slow. Consider chunking the source data."
            )
        result = {}
        for target_table in self.table_mappings.keys():
            if target_filter is not None and target_table not in target_filter:
                continue
            df = self._build_table_df(source_df, target_table)
            df = df.dropna(how='all')
            if not df.empty:
                result[target_table] = df
                logger.info(f"Built {len(df)} rows for table '{target_table}'")
        return result