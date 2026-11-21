# -*- coding: utf-8 -*-
"""
MappingTransformer - Interprets CSV mapping files (or inline column specs) to
transform source DataFrames into CDM table-specific DataFrames.
"""

from __future__ import annotations

import calendar
import json
import logging
import warnings
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Union

import pandas as pd
from sqlalchemy import Integer

from database import models
from database.session import SessionLocal
from etl.transformers.concept_resolver import AUTO_ACCEPTED, ConceptResolver
from utilities.config.source_spec import MAPPING_COLUMNS

if TYPE_CHECKING:  # pragma: no cover - type checkers only
    from etl.transformers.dimension_index import DimensionKeyIndex

UNPARTITIONED = "<unpartitioned>"

logger = logging.getLogger(__name__)


class MappingTransformer:
    """Transforms a source DataFrame into multiple CDM table DataFrames."""

    def __init__(self, mapping_csv_path: Optional[str] = None,
                 large_df_threshold: int = 1_000_000, *,
                 column_specs: Optional[List[Any]] = None,
                 registry: Any = None,
                 run_id: Optional[str] = None) -> None:
        if mapping_csv_path and column_specs:
            raise ValueError(
                "MappingTransformer accepts a mapping CSV or inline column "
                "specs, not both; they describe the same table and there is no "
                "defined precedence between them."
            )
        if not mapping_csv_path and not column_specs:
            raise ValueError(
                "MappingTransformer needs `mapping_csv_path` or `column_specs`; "
                "with neither there is nothing to transform."
            )

        self.mapping_csv_path = mapping_csv_path
        self.column_specs: List[Any] = list(column_specs) if column_specs else []
        self.large_df_threshold = large_df_threshold
        self.registry = registry
        self.run_id = run_id

        self._input_kind = "columns" if self.column_specs else "csv"
        if self.column_specs:
            rows = [spec.mapping_row() for spec in self.column_specs]
            self.mapping = pd.DataFrame(rows, columns=list(MAPPING_COLUMNS))
        else:
            self.mapping = pd.read_csv(mapping_csv_path, quotechar='"')

        self._normalise_mapping_frame()

        self._resolver: Optional[ConceptResolver] = None
        self._resolver_session: Any = None
        self._pending_resolutions: Dict[tuple, Any] = {}
        self._index_cache: Dict[tuple, Optional[DimensionKeyIndex]] = {}
        self._warned_unpartitioned: set = set()

        self._validate_mapping()
        self._group_by_target_table()

    # -------------------------------------------------------------------------
    # Construction hygiene
    # -------------------------------------------------------------------------

    def _normalise_mapping_frame(self) -> None:
        """Coerce the mapping frame into the dtypes the transform loop expects."""

        nullable_columns = (
            "source_column", "transform_params", "static_value",
            "default_value", "source_data_type",
        )
        for column in nullable_columns:
            if column not in self.mapping.columns:
                continue
            self.mapping[column] = self.mapping[column].where(
                self.mapping[column].map(
                    lambda value: not (
                        isinstance(value, str) and not value.strip()
                    )
                )
            )

        if "required" in self.mapping.columns:
            def _as_bool(value) -> bool:
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() in ("true", "t", "1", "yes")

            self.mapping["required"] = self.mapping["required"].map(_as_bool)


    def _validate_mapping(self) -> None:
        """Validate the mapping table for whichever input shape produced it."""
        
        required_cols = {
            "source_column", "target_table", "target_column", "transform", "required",
        }
        missing = required_cols - set(self.mapping.columns)
        if missing:
            origin = (
                f"inline column specs ({len(self.column_specs)} entries)"
                if self._input_kind == "columns"
                else f"mapping CSV {self.mapping_csv_path!r}"
            )
            raise ValueError(f"{origin} is missing column(s): {sorted(missing)}")

        if self._input_kind == "columns":
            known = {
                "none", "map", "date_parse", "cast", "split", "unit_convert",
                "format_date", "coalesce", "derive_category", "concat", "resolve",
            }
            for spec in self.column_specs:
                if spec.transform_name not in known:
                    raise ValueError(
                        f"{spec.target_field}: transform {spec.transform_name!r} "
                        f"is not one this transformer dispatches on "
                        f"({', '.join(sorted(known))})"
                    )

        params_sourced = {"concat", "coalesce", "format_date", "derive_category"}

        for index, row in self.mapping.iterrows():
            transform = str(row.get("transform") or "none").strip().lower()

            def _present(column: str) -> bool:
                value = row.get(column)
                return not (value is None or (isinstance(value, float) and pd.isna(value)))

            if _present("source_column") or _present("static_value") or _present("default_value"):
                continue
            if transform in params_sourced or transform == "resolve":
                continue

            if transform == "map":
                raw_params = row.get("transform_params")
                parsed: dict = {}
                if _present("transform_params") and str(raw_params).strip():
                    try:
                        candidate = json.loads(str(raw_params))
                        parsed = candidate if isinstance(candidate, dict) else {}
                    except (TypeError, ValueError):
                        parsed = {}
                if parsed.get("columns"):
                    continue
                if parsed.get("ref") and not parsed.get("columns"):
                    logger.warning(
                        "mapping row %d writes %s.%s with a `map` ref but no "
                        "`columns`, so there is no value to look up; the row will "
                        "write NULL. Add `columns`, or a `source_column`.",
                        index, row.get("target_table"), row.get("target_column"),
                    )
                    continue

            raise ValueError(
                f"mapping row {index} writes "
                f"{row.get('target_table')}.{row.get('target_column')} but names no "
                f"`source_column`, no `static_value`, no `default_value`, and uses "
                f"transform {transform!r}, which reads no column. That row would "
                f"write NULL for every record."
            )


    def _group_by_target_table(self) -> None:
        """Group mapping rows by target table name."""
        self.table_mappings = defaultdict(list)
        for _, row in self.mapping.iterrows():
            self.table_mappings[row["target_table"]].append(row.to_dict())

    
    def _apply_map_transform(
        self,
        value: Any,
        params: Optional[Union[str, dict]],
        extra_context: Optional[dict] = None,
        target_field: Optional[str] = None,
    ) -> Any:
        """Apply a ``map`` transform: a dimension lookup, or a literal table."""

        ref = params.get("ref") if isinstance(params, dict) else None

        if ref:
            parts = str(ref).split(".")
            if len(parts) != 2:
                logger.warning("Invalid ref format: %r. Expected 'table.field'", ref)
                return None

            table_name, field_name = parts[0].strip(), parts[1].strip()
            lookup_value = self._build_lookup_value(
                value, params or {}, extra_context
            )
            if lookup_value is None or (
                isinstance(lookup_value, float) and pd.isna(lookup_value)
            ):
                return None

            index = self._index_for(table_name, field_name)
            if index is None:
                logger.warning(
                    "No dimension index for %s.%s; leaving %r unresolved. "
                    "(ref=%r, target=%r)",
                    table_name, field_name, lookup_value, ref, target_field,
                )
                return None

            entry = index.lookup_raw(lookup_value)
            if entry is None:
                logger.warning(
                    "No match found for %s.%s = %r",
                    table_name, field_name, lookup_value,
                )
                return None

            # A fact FK holds the surrogate PK where the dimension has one, and
            # the natural key where it does not (dim_vector's PK *is* its code).
            if entry.pk is not None:
                return entry.pk
            return entry.code

        mapping = params or {}
        default = mapping.get("default", value)
        lookup = {k: v for k, v in mapping.items() if k != "default"}

        governing = self._governing_pack(target_field)
        if governing is not None and lookup:
            logger.warning(
                "%s is governed by pack %r, so this inline {raw: code} table is "
                "non-authoritative: the registry owns that decision and a literal "
                "here is a second, unversioned source of truth. Declare the "
                "mapping in the pack's vocabulary, or use "
                "transform: {type: resolve}. The literal is still applied so this "
                "run's output matches the config as written.",
                target_field, getattr(governing, "pack_id", "?"),
            )

        return lookup.get(str(value), default)

    
    def _apply_resolve_transform(
        self,
        value: Any,
        field: str,
        params: Optional[dict] = None,
    ) -> Optional[str]:
        """Resolve one raw value against the pack that governs ``field``."""

        if self.registry is None:
            raise ValueError(
                "the `resolve` transform needs a VocabularyRegistry "
                "(MappingTransformer(..., registry=registry)). Without one there "
                "is no pack to resolve against, and guessing one is the "
                "policy-as-literal this refactor removes."
            )

        params = dict(params or {})
        target_field = str(field or "").strip()
        if not target_field:
            raise ValueError(
                "the `resolve` transform needs the CDM field it writes into "
                "(for example 'dim_vector.species_name')."
            )

        supplied_pack_id = params.get("pack")
        if supplied_pack_id:
            pack = self.registry.get(str(supplied_pack_id))
        else:
            pack = self.registry.select_pack(target_field)

        source_table, source_column = self._partition_field(target_field)

        resolver = self._ensure_resolver()
        resolution = resolver.resolve(
            value,
            pack,
            {
                "source_table": source_table,
                "source_column": source_column,
                "target_field": target_field,
                "run_id": self.run_id,
            },
        )

        self._pending_resolutions[
            (
                resolution.source_table,
                resolution.source_column,
                resolution.source_value,
                resolution.run_id or "",
            )
        ] = resolution

        return (
            resolution.concept_code
            if resolution.decision == AUTO_ACCEPTED
            else None
        )

    def _partition_field(self, target_field: str) -> tuple[str, str]:
        """Split ``'dim_vector.species_name'`` into its two halves."""

        table, separator, column = str(target_field).partition(".")
        table = table.strip()
        column = column.strip()
        if not separator or not table or not column:
            marker = (table or UNPARTITIONED, column or UNPARTITIONED)
            if marker not in self._warned_unpartitioned:
                self._warned_unpartitioned.add(marker)
                logger.warning(
                    "the `resolve` transform was given field %r, which is not "
                    "'<table>.<column>'. Every resolution for it will be recorded "
                    "with source_table=%r and source_column=%r, so the audit trail "
                    "will not say which CDM column these decisions belong to. Fix "
                    "the mapping row to name the target field in full.",
                    target_field, marker[0], marker[1],
                )
            return marker
        return table, column

    
    def _apply_date_parse_transform(self, value: Any,
                                    params: Optional[Union[str, dict]]) -> Any:
        """Parse a date value and return it as a ``YYYY-MM-DD`` string."""
        fmt = params.get("format", "%Y-%m-%d") if params else "%Y-%m-%d"
        try:
            if isinstance(value, (pd.Timestamp, datetime)):
                return value.strftime("%Y-%m-%d")
            try:
                dt = pd.to_datetime(value, format=fmt)
            except (ValueError, TypeError):
                dt = pd.to_datetime(value)
            return dt.strftime("%Y-%m-%d")
        except Exception as exc:  # noqa: BLE001 - one bad cell must not stop a table
            logger.warning(
                "Date parse failed for %r with format %r: %s", value, fmt, exc
            )
            return None


    def _apply_derive_category_transform(self, value, params,
                                         extra_context=None) -> Any:
        """Derive a categorical label from one or more source columns."""
        if not params:
            return None
        if not extra_context:
            return params.get("default")

        null_tokens = {"", "NA", "N/A", "NONE", "NULL", "NAN", "NR", "-"}
        extra_nulls = params.get("treat_as_null") or []
        if isinstance(extra_nulls, str):
            extra_nulls = [extra_nulls]
        null_tokens.update(str(x).strip().upper() for x in extra_nulls)

        def _num(v):
            if v is None:
                return None
            if isinstance(v, float) and pd.isna(v):
                return None
            if isinstance(v, str):
                s = v.strip()
                if not s or s.upper() in null_tokens:
                    return None
                try:
                    return float(s)
                except (ValueError, TypeError):
                    return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def _check(cond) -> bool:
            op = cond["op"]

            if "cols" in cond:
                a = _num(extra_context.get(cond["cols"][0]))
                b = _num(extra_context.get(cond["cols"][1]))
                if a is None or b is None or (a + b) <= 0:
                    return False
                ratio = a / (a + b)
                v = float(cond["value"])
                return {
                    "ratio_gt": ratio > v,
                    "ratio_gte": ratio >= v,
                    "ratio_lt": ratio < v,
                    "ratio_lte": ratio <= v,
                }.get(op, False)

            x = _num(extra_context.get(cond["col"]))
            if x is None:
                return False
            y = float(cond["value"])
            return {
                ">": x > y, ">=": x >= y,
                "<": x < y, "<=": x <= y,
                "==": x == y, "!=": x != y,
            }.get(op, False)

        for rule in params.get("rules", []):
            if all(_check(c) for c in rule["when"]):
                return rule["label"]

        return params.get("default")

    def _apply_cast_transform(self, value, params):
        target_type = params.get("type", "str")
        null_tokens = {"NR", "NF", "ND", "-", "NA", "N/A", "NONE", "NULL", "NAN"}
        if isinstance(value, str) and value.strip().upper() in null_tokens:
            return None
        try:
            if target_type == "int":
                return int(float(value))
            if target_type == "float":
                return float(value)
            if target_type == "str":
                return str(value)
            return value
        except (ValueError, TypeError):
            logger.warning("Cast to %s failed for value %r", target_type, value)
            return None

    def _apply_split_transform(self, value: Any,
                               params: Optional[Union[str, dict]]) -> Any:
        delimiter = params.get("delimiter", ",")
        index = int(params.get("index", 0))
        parts = str(value).split(delimiter)
        if -len(parts) <= index < len(parts):
            return parts[index].strip()
        logger.warning("Split index %s out of range for value %r", index, value)
        return None


    def _apply_unit_convert_transform(self, value: Any,
                                      params: Optional[Union[str, dict]]) -> Any:
        """Affine unit conversion."""

        params = params or {}
        factor = float(params.get("factor", 1.0))
        offset = float(params.get("offset", 0.0))
        divide = bool(params.get("divide", False))
        default = params.get("default")
        clamp_min = params.get("clamp_min")
        clamp_max = params.get("clamp_max")
        round_to = params.get("round_to")
        fraction = bool(params.get("fraction", False))

        null_tokens = {"", "NA", "N/A", "NONE", "NULL", "NAN", "<NA>", "NAT"}
        extra_nulls = params.get("treat_as_null") or []
        if isinstance(extra_nulls, str):
            extra_nulls = [extra_nulls]
        null_tokens.update(str(x).strip().upper() for x in extra_nulls)

        if value is None or (isinstance(value, float) and pd.isna(value)):
            return default
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text or text.upper() in null_tokens:
                return default
            try:
                number = float(text)
            except (TypeError, ValueError):
                logger.warning("unit_convert: could not parse %r", value)
                return default
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                logger.warning("unit_convert: could not parse %r", value)
                return default

        if pd.isna(number):
            return default

        if isinstance(value, str) and value.strip().endswith("%") and fraction:
            number = number / 100.0

        if divide:
            if factor == 0:
                logger.warning(
                    "unit_convert: divide by zero (factor=%r); returning default",
                    factor,
                )
                return default
            number = number / factor
        else:
            number = number * factor
        number += offset

        if clamp_min is not None:
            number = max(number, float(clamp_min))
        if clamp_max is not None:
            number = min(number, float(clamp_max))
        if round_to is not None:
            number = round(number, int(round_to))
        return number


    def _apply_coalesce_transform(self, value: Any,
                                  params: Optional[Union[str, dict]],
                                  extra_context: Optional[dict] = None) -> Any:
        """Return the first non-null value among ``params['columns']``."""

        params = params or {}
        cols = params.get("columns", [])
        default = params.get("default")

        if not cols or not isinstance(cols, list):
            logger.warning(
                "coalesce: 'columns' must be a non-empty list, got %r", cols
            )
            return default
        if not extra_context:
            return default

        null_tokens = {"", "NA", "N/A", "NONE", "NULL", "NAN", "NR", "<NA>", "NAT"}
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
            if type(v).__name__ in ("NAType", "NaTType"):
                continue
            if isinstance(v, str) and v.strip().upper() in null_tokens:
                continue
            return v

        return default


    def _apply_format_date_transform(self, value: Any,
                                     params: Optional[Union[str, dict]],
                                     extra_context: Optional[dict] = None) -> Any:
        """Build a ``YYYY-MM-DD`` date_id from one to three source columns."""

        params = params or {}
        cols = params.get("columns", [])
        if not isinstance(cols, list) or not (1 <= len(cols) <= 3):
            logger.warning(
                "format_date: 'columns' must be a list of 1-3 names, got %r", cols
            )
            return params.get("default")

        default = params.get("default")
        month_default = int(params.get("month_default", 1))
        day_default = int(params.get("day_default", 1))
        min_year = int(params.get("min_year", 1800))
        max_year = int(params.get("max_year", 2200))
        clamp_invalid = bool(params.get("clamp_invalid_day", True))

        def _as_int(raw):
            if raw is None:
                return None
            if isinstance(raw, float) and pd.isna(raw):
                return None
            if isinstance(raw, str):
                s = raw.strip()
                if not s or s.upper() in ("NA", "N/A", "NONE", "NULL", "NAN", "<NA>"):
                    return None
                try:
                    return int(float(s))
                except (ValueError, TypeError):
                    return None
            try:
                return int(float(raw))
            except (ValueError, TypeError):
                return None

        year = _as_int(extra_context.get(cols[0])) if extra_context else None
        if year is None or not (min_year <= year <= max_year):
            logger.debug("format_date: unusable year for %r; using %r",
                         extra_context, default)
            return default

        month = month_default
        if len(cols) >= 2 and extra_context:
            m = _as_int(extra_context.get(cols[1]))
            if m is not None and 1 <= m <= 12:
                month = m

        day = day_default
        if len(cols) >= 3 and extra_context:
            d = _as_int(extra_context.get(cols[2]))
            if d is not None and 1 <= d <= 31:
                day = d

        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            if not clamp_invalid:
                return default
            last_day = calendar.monthrange(year, month)[1]
            return datetime(year, month, last_day).strftime("%Y-%m-%d")


    def _apply_transform(self, value: Any, transform: str,
                         params: Optional[Union[str, dict]],
                         extra_context: Optional[dict] = None,
                         target_field: Optional[str] = None) -> Any:
        """Apply one transformation to one value."""

        if isinstance(params, str):
            if not params.strip():
                params = {}
            elif transform == "cast":
                params = {"type": params}
            else:
                params = {}
        elif params is None:
            params = {}

        if transform not in (
            "format_date", "map", "concat", "coalesce", "derive_category"
        ):
            if value is None:
                return None
            if isinstance(value, float) and pd.isna(value):
                return None

        if transform == "none":
            return value
        if transform == "map":
            return self._apply_map_transform(
                value, params, extra_context, target_field
            )
        if transform == "resolve":
            return self._apply_resolve_transform(value, target_field, params)
        if transform == "date_parse":
            return self._apply_date_parse_transform(value, params)
        if transform == "cast":
            return self._apply_cast_transform(value, params)
        if transform == "split":
            return self._apply_split_transform(value, params)
        if transform == "unit_convert":
            return self._apply_unit_convert_transform(value, params)
        if transform == "format_date":
            return self._apply_format_date_transform(value, params, extra_context)
        if transform == "coalesce":
            return self._apply_coalesce_transform(value, params, extra_context)
        if transform == "derive_category":
            return self._apply_derive_category_transform(
                value, params, extra_context
            )

        logger.warning("Unknown transform type: %r", transform)
        return value

    # -------------------------------------------------------------------------
    # Model / key discovery
    # -------------------------------------------------------------------------

    def _model_for_table(self, table_name: str):
        """The model class for ``table_name``, derived rather than listed."""

        for attribute in sorted(vars(models)):
            candidate = getattr(models, attribute)
            if getattr(candidate, "__tablename__", None) == table_name:
                return candidate
        return None


    def _natural_key(self, table_name: str) -> Optional[str]:
        """The column a dimension row is identified by."""

        try:
            from etl.loaders.db_loader import NATURAL_KEY_CONFIG
        except Exception as exc:  # noqa: BLE001
            logger.debug("NATURAL_KEY_CONFIG unavailable: %s", exc)
            return None

        config = NATURAL_KEY_CONFIG.get(table_name) or {}
        columns = config.get("columns") or []
        return columns[0] if columns else None


    def _index_for(self, table_name: str,
                   field_name: str) -> Optional["DimensionKeyIndex"]:
        """A ``DimensionKeyIndex`` for ``table_name`` matched on ``field_name``."""

        cache_key = (table_name, field_name)
        if cache_key in self._index_cache:
            return self._index_cache[cache_key]

        from etl.transformers.dimension_index import DimensionKeyIndex

        index: Optional[DimensionKeyIndex] = None
        model = self._model_for_table(table_name)
        natural_key = self._natural_key(table_name)

        if model is None or not natural_key:
            logger.warning(
                "Cannot build a dimension index for %s.%s (model=%r, "
                "natural_key=%r); refs into it will resolve to NULL.",
                table_name, field_name, model, natural_key,
            )
            self._index_cache[cache_key] = None
            return None

        primary_key = list(model.__table__.primary_key.columns)
        pk_field = None
        if len(primary_key) == 1 and isinstance(primary_key[0].type, Integer):
            pk_field = primary_key[0].name

        columns = set(model.__table__.columns.keys())
        try:
            with SessionLocal() as session:
                index = DimensionKeyIndex.from_session(
                    session,
                    table_name,
                    code_field=natural_key,
                    pk_field=pk_field,
                    concept_code_field=None,
                    concept_id_field=(
                        "concept_id" if "concept_id" in columns else None
                    ),
                    standard_code_field=(
                        "standard_code" if "standard_code" in columns else None
                    ),
                    concept_name_field=None,
                    alias_field=field_name if field_name in columns else None,
                    label=table_name,
                )
        except Exception as exc:  # noqa: BLE001 - a bad ref must not abort a source
            logger.error(
                "Could not seed a dimension index for %s on %r: %s: %s",
                table_name, field_name, type(exc).__name__, exc,
            )
            index = None

        self._index_cache[cache_key] = index
        return index


    def _governing_pack(self, target_field: Optional[str]):
        """The pack that governs ``target_field``, or None."""

        if self.registry is None or not target_field:
            return None
        try:
            return self.registry.select_pack(str(target_field))
        except Exception as exc:  # noqa: BLE001 - FieldNotInCatalog included
            logger.debug("no governing pack for %r: %s", target_field, exc)
            return None


    def _ensure_resolver(self) -> ConceptResolver:
        """The per-transformer ``ConceptResolver``, created on first use."""

        if self._resolver is None:
            if self._resolver_session is None:
                self._resolver_session = SessionLocal()
            self._resolver = ConceptResolver(self.registry, self._resolver_session)
        return self._resolver

    # -------------------------------------------------------------------------
    # Row loop
    # -------------------------------------------------------------------------

    def _build_lookup_value(self, original_value: Any, params: dict,
                            extra_context: Optional[dict]) -> Any:
        """Build the lookup value from one column, or from several joined."""
        columns = params.get("columns")
        if columns and extra_context:
            parts = []
            for col in columns:
                if col in extra_context and pd.notna(extra_context[col]):
                    parts.append(str(extra_context[col]))
            if parts:
                separator = params.get("separator", "/")
                return separator.join(parts)
        return original_value


    def _legacy_lookup_dimension_id(self, model_class, field_name: str,
                                    lookup_value: Any) -> Optional[int]:
        """Deprecated. Per-value ``SELECT ... WHERE field == value`` lookup."""

        warnings.warn(
            "MappingTransformer._lookup_dimension_id is deprecated; use "
            "_index_for() + DimensionKeyIndex.lookup_raw(), which keys values "
            "consistently and returns the concept alongside the surrogate key.",
            DeprecationWarning,
            stacklevel=2,
        )

        if lookup_value is None or (
            isinstance(lookup_value, float) and pd.isna(lookup_value)
        ):
            return None

        cache = self.__dict__.setdefault("_dimension_cache", {})
        cache_key = f"{model_class.__tablename__}_{field_name}_{lookup_value}"
        if cache_key in cache:
            return cache[cache_key]

        try:
            with SessionLocal() as session:
                query = session.query(model_class).filter(
                    getattr(model_class, field_name) == lookup_value
                )
                result = query.first()
                if result:
                    pk_column = model_class.__table__.primary_key.columns.values()[0]
                    pk_value = getattr(result, pk_column.name)
                    cache[cache_key] = pk_value
                    return pk_value
                logger.warning(
                    "No match found for %s.%s = %r",
                    model_class.__tablename__, field_name, lookup_value,
                )
                return None
        except Exception as exc:  # noqa: BLE001
            logger.error("Error during dimension lookup: %s", exc)
            return None


    def _determine_raw_value(self, source_row, target_table, mapping) -> Any:
        value = None

        if pd.notna(mapping.get("static_value")):
            return mapping["static_value"]

        src_col = mapping.get("source_column")
        if src_col is None or (isinstance(src_col, float) and pd.isna(src_col)):
            return None

        if src_col not in source_row:
            if mapping.get("required", False):
                raise ValueError(
                    f"Required source column {src_col!r} missing in data "
                    f"(table {target_table})"
                )
            value = mapping.get("default_value")
        else:
            value = source_row[src_col]
            if pd.notna(mapping.get("source_data_type")):
                dtype = mapping["source_data_type"]
                try:
                    if dtype == "int":
                        value = int(float(value)) if pd.notna(value) else None
                    elif dtype == "float":
                        value = float(value) if pd.notna(value) else None
                    elif dtype == "str":
                        value = str(value) if pd.notna(value) else None
                except (ValueError, TypeError):
                    value = None
        return value


    def _apply_transformation(self, idx, raw_value, source_row, mapping,
                              row_context) -> Any:
        """Parse one mapping row's params and apply its transform."""
        transform = mapping["transform"]
        target_field = f"{mapping['target_table']}.{mapping['target_column']}"
        params = None

        raw_params = mapping.get("transform_params")
        if pd.notna(raw_params):
            params_str = raw_params
            if params_str and str(params_str).strip():
                try:
                    params = json.loads(params_str)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Invalid JSON in transform_params: %r - %s", params_str, exc
                    )
                    params = {}
            else:
                params = {}

        if transform == "concat":
            cols = (params or {}).get("columns", [])
            sep = (params or {}).get("separator", " ")
            parts = []
            for col in cols:
                if col in row_context and pd.notna(row_context[col]):
                    parts.append(str(row_context[col]))
                else:
                    logger.warning(
                        "Concat: column %r missing or null in row %s", col, idx
                    )
            return sep.join(parts) if parts else mapping.get("default_value")

        return self._apply_transform(
            raw_value, transform, params,
            extra_context=row_context,
            target_field=target_field,
        )


    def _apply_default_transform(self, value, mapping, target_table):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            if mapping.get("required", False):
                raise ValueError(
                    f"Required field '{mapping['target_column']}' in table "
                    f"'{target_table}' has null value"
                )
            value = mapping.get("default_value")
        return value


    def _build_table_df(self, source_df: pd.DataFrame,
                        target_table: str) -> pd.DataFrame:
        rows = []
        mappings = self.table_mappings[target_table]
        records = source_df.to_dict(orient="records")

        for idx, row_context in enumerate(records):
            record = {}
            for mapping in mappings:
                value = self._determine_raw_value(
                    row_context, target_table, mapping
                )
                value = self._apply_transformation(
                    idx, value, row_context, mapping, row_context
                )
                value = self._apply_default_transform(value, mapping, target_table)
                record[mapping["target_column"]] = value
            rows.append(record)
        return pd.DataFrame(rows)


    def transform(self, source_df: pd.DataFrame,
                  target_filter: Optional[set] = None) -> Dict[str, pd.DataFrame]:
        """Transform a source DataFrame into ``{table: DataFrame}``."""

        if len(source_df) > self.large_df_threshold:
            logger.warning(
                "Source DataFrame has %s row(s) (> %s). Row-wise transformation "
                "may be slow. Consider chunking the source data.",
                f"{len(source_df):,}", f"{self.large_df_threshold:,}",
            )

        result: Dict[str, pd.DataFrame] = {}
        for target_table in self.table_mappings.keys():
            if target_filter is not None and target_table not in target_filter:
                continue
            df = self._build_table_df(source_df, target_table)
            df = df.dropna(how="all")
            if not df.empty:
                result[target_table] = df
                logger.info(
                    "Built %d rows for table '%s'", len(df), target_table
                )
        return result

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def pending_resolutions(self) -> List[Any]:
        """Resolutions produced by ``resolve`` transforms, sorted, de-duplicated."""
        return [
            self._pending_resolutions[key]
            for key in sorted(self._pending_resolutions)
        ]


    def close(self) -> None:
        """Release the resolver's session. Safe to call twice."""

        if self._resolver_session is not None:
            try:
                self._resolver_session.close()
            finally:
                self._resolver_session = None
                self._resolver = None


    def __enter__(self) -> "MappingTransformer":
        return self


    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = ["MappingTransformer"]