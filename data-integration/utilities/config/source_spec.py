# -*- coding: utf-8 -*-
"""
The user-facing source specification: `ColumnSpec`, `SourceSpec`,
`load_source_specs`.

This module is the only place that decides what a `data_sources` entry may
contain, and the only place that refuses vocabulary configuration.

"""

from __future__ import annotations

import json
import logging
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


MAPPING_COLUMNS: tuple[str, ...] = (
    "source_column",
    "target_table",
    "target_column",
    "transform",
    "transform_params",
    "required",
    "static_value",
    "default_value",
    "source_data_type",
)

KNOWN_TRANSFORMS: frozenset[str] = frozenset(
    {
        "none",
        "map",
        "date_parse",
        "cast",
        "split",
        "unit_convert",
        "format_date",
        "coalesce",
        "derive_category",
        "concat",
        "resolve",
    }
)

FORBIDDEN_KEY_PATTERN = re.compile(r"^(vocab|concept)", re.IGNORECASE)


class DomainHint(str, Enum):
    """The eight domain hints of review §3 step 2, and nothing else.

    A hint says what a *source column* means, which is the one thing the tool
    cannot infer when a column could plausibly feed two CDM destinations (a
    "Plasmodium" column is a disease in one source and a pathogen in another).
    It never names a vocabulary: the domain is the input to
    `VocabularyRegistry.select_pack`, not a pack id.
    """

    location = "location"
    vector = "vector"
    disease = "disease"
    insecticide = "insecticide"
    diagnostic_method = "diagnostic_method"
    collection_method = "collection_method"
    resistance_test_type = "resistance_test_type"
    socioeconomic_indicator = "socioeconomic_indicator"


class TransformSpec(BaseModel):
    """Structured form of a transform: ``{type: cast, params: {type: float}}``."""

    model_config = ConfigDict(extra="forbid")

    type: str = "none"
    params: Optional[Dict[str, Any]] = None

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        name = str(value).strip().lower()
        if name not in KNOWN_TRANSFORMS:
            raise ValueError(
                f"unknown transform {value!r}; known transforms: "
                f"{', '.join(sorted(KNOWN_TRANSFORMS))}"
            )
        return name


class ColumnSpec(BaseModel):
    """One source column and what it becomes in the CDM."""

    model_config = ConfigDict(extra="forbid")

    source_column: Optional[str] = None
    target_table: str
    target_column: str

    transform: Optional[Union[str, TransformSpec]] = None
    transform_params: Optional[Union[Dict[str, Any], str]] = None

    required: bool = False
    static_value: Optional[str] = None
    default_value: Optional[str] = None
    source_data_type: Optional[str] = None

    admin_level: Optional[str] = None
    domain_hint: Optional[DomainHint] = None

    @field_validator("target_table", "target_column")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text_value = str(value).strip()
        if not text_value:
            raise ValueError("must be a non-empty string")
        return text_value

    @field_validator("transform", mode="before")
    @classmethod
    def _normalise_transform(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            name = value.strip()
            if not name:
                return None
            return name if name.lower() in KNOWN_TRANSFORMS else name
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "ColumnSpec":
        """Reject the three combinations that have no defined meaning."""
        if isinstance(self.transform, TransformSpec):
            if self.transform_params is not None:
                raise ValueError(
                    f"{self.target_field}: give transform params either inside "
                    f"`transform: {{type, params}}` or as `transform_params`, "
                    f"not both"
                )
        elif isinstance(self.transform, str) and self.transform.strip().lower() not in KNOWN_TRANSFORMS:
            raise ValueError(
                f"{self.target_field}: unknown transform {self.transform!r}; "
                f"known transforms: {', '.join(sorted(KNOWN_TRANSFORMS))}"
            )

        if self.source_column is None and self.static_value is None:
            transform_name = self.transform_name
            if transform_name not in ("format_date", "coalesce", "concat", "derive_category"):
                raise ValueError(
                    f"{self.target_field}: needs a `source_column` or a "
                    f"`static_value` (transform {transform_name!r} does not "
                    f"read one column, so it must say which it is)"
                )

        if self.required and self.source_column is None and self.static_value is None:
            raise ValueError(
                f"{self.target_field}: `required: true` needs a source_column "
                f"or a static_value to be required *of*"
            )
        return self

    # -- derived views ---------------------------------------------------

    @property
    def target_field(self) -> str:
        """``dim_vector.species_name`` — a `FIELD_CATALOG`-shaped field name."""
        return f"{self.target_table}.{self.target_column}"

    @property
    def transform_name(self) -> str:
        """The transform's type name, defaulting to ``"none"``."""
        transform = self.transform
        if transform is None:
            return "none"
        if isinstance(transform, TransformSpec):
            return transform.type
        return str(transform).strip().lower() or "none"

    @property
    def transform_parameters(self) -> Optional[Dict[str, Any]]:
        """The transform's parameters as a dict, or None when there are none."""
        transform = self.transform
        if isinstance(transform, TransformSpec) and transform.params is not None:
            return dict(transform.params)
        params = self.transform_params
        if params is None:
            return None
        if isinstance(params, dict):
            return dict(params)
        text_value = str(params).strip()
        if not text_value:
            return None
        try:
            parsed = json.loads(text_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{self.target_field}: transform_params is not valid JSON: {exc}"
            ) from None
        if not isinstance(parsed, dict):
            raise ValueError(
                f"{self.target_field}: transform_params must be a JSON object"
            )
        return parsed

    def mapping_row(self) -> Dict[str, Any]:
        """One mapping row, in the exact shape ``pd.read_csv`` would produce."""

        params = self.transform_parameters
        return {
            "source_column": self.source_column,
            "target_table": self.target_table,
            "target_column": self.target_column,
            "transform": self.transform_name,
            "transform_params": (
                json.dumps(params, sort_keys=True) if params else None
            ),
            "required": bool(self.required),
            "static_value": self.static_value,
            "default_value": self.default_value,
            "source_data_type": self.source_data_type,
        }


class SourceSpec(BaseModel):
    """One `data_sources` entry: where the data is, and how to project it."""

    model_config = ConfigDict(extra="allow")

    source_name: str
    type: str = "file"
    extension: str
    path: str
    extension_options: Dict[str, Any] = Field(default_factory=dict)
    extract_mode: str = "single"

    matching: Optional[str] = None
    columns: Optional[List[ColumnSpec]] = None

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        name = str(value).strip().lower()
        if name not in ("file", "folder"):
            raise ValueError(f"type must be 'file' or 'folder', got {value!r}")
        return name

    @field_validator("extract_mode")
    @classmethod
    def _check_extract_mode(cls, value: str) -> str:
        name = str(value).strip().lower()
        if name not in ("single", "timeseries", "chunked"):
            raise ValueError(
                f"extract_mode must be single | timeseries | chunked, got {value!r}"
            )
        return name

    @field_validator("extension")
    @classmethod
    def _check_extension(cls, value: str) -> str:
        name = str(value).strip().lower().lstrip(".")
        if not name:
            raise ValueError("extension must be a non-empty string")
        return name

    @model_validator(mode="after")
    def _check_mapping_or_columns(self) -> "SourceSpec":
        has_matching = bool(self.matching and str(self.matching).strip())
        has_columns = bool(self.columns)

        if has_matching and has_columns:
            raise ValueError(
                f"source {self.source_name!r} declares both `matching:` and "
                f"`columns:`. They are alternatives, not layers: `matching:` is "
                f"a mapping CSV, `columns:` is the inline form of the same "
                f"information. Keep one and delete the other."
            )
        if not has_matching and not has_columns:
            raise ValueError(
                f"source {self.source_name!r} declares neither `matching:` nor "
                f"`columns:`. One of them is required to say which source "
                f"column becomes which CDM column."
            )

        if has_columns:
            seen: Dict[tuple[str, str], str] = {}
            for index, column in enumerate(self.columns):
                key = (column.target_table, column.target_column)
                if key in seen:
                    raise ValueError(
                        f"source {self.source_name!r}: columns[{index}] writes "
                        f"{column.target_field}, which is already written by "
                        f"another entry; one target column takes one source "
                        f"column"
                    )
                seen[key] = column.source_column or "(static)"
        return self

    # -- derived views ---------------------------------------------------

    @property
    def uses_inline_columns(self) -> bool:
        """True when the source is driven by `columns:` rather than a CSV."""
        return bool(self.columns)

    @property
    def unknown_keys(self) -> tuple[str, ...]:
        """Keys present in the YAML but not modelled here, sorted.

        `extra="allow"` keeps legacy configs parseable; this is how the caller
        can still say what it ignored.
        """
        declared = set(type(self).model_fields)
        return tuple(sorted(k for k in (self.__pydantic_extra__ or {}) if k not in declared))


# =============================================================================
# Loading
# =============================================================================

def _reject_vocabulary_keys(node: Any, path: tuple[str, ...] = ()) -> None:
    """Walk the whole document and refuse any vocabulary/concept key."""

    if isinstance(node, Mapping):
        for key in node:
            text_key = str(key)
            location = ".".join(path + (text_key,))
            if FORBIDDEN_KEY_PATTERN.match(text_key):
                raise ValueError(
                    f"{location}: vocabulary configuration is not accepted in a "
                    f"source spec. Vocabularies are chosen by the registry "
                    f"(utilities/vocabulary/registry.py), not by the user (review "
                    f"C1): a key here was read by at most one code path and "
                    f"ignored by the rest, so editing it changed nothing. Delete "
                    f"{location!r}; if you meant to point at boundary data, use "
                    f"core.admin_boundaries.path."
                )
            _reject_vocabulary_keys(node[key], path + (text_key,))
    elif isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _reject_vocabulary_keys(item, path + (f"[{index}]",))


def load_source_specs(yaml_path: Union[str, Path]) -> Dict[str, SourceSpec]:
    """Parse a YAML config into ``{source_key: SourceSpec}``."""

    path = Path(yaml_path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read source spec {path}: {exc}") from exc

    try:
        document = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(document, Mapping):
        raise ValueError(
            f"{path}: expected a YAML mapping at the top level, got "
            f"{type(document).__name__}"
        )

    _reject_vocabulary_keys(document)

    sources = document.get("data_sources")
    if not isinstance(sources, Mapping) or not sources:
        raise ValueError(
            f"{path}: expected a non-empty `data_sources` mapping"
        )

    specs: Dict[str, SourceSpec] = {}
    for source_key in sorted(sources, key=str):
        entry = sources[source_key]
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"{path}: data_sources.{source_key} must be a mapping, got "
                f"{type(entry).__name__}"
            )
        try:
            specs[str(source_key)] = SourceSpec.model_validate(entry)
        except Exception as exc:
            raise ValueError(
                f"{path}: data_sources.{source_key} is invalid: {exc}"
            ) from exc

        unknown = specs[str(source_key)].unknown_keys
        if unknown:
            logger.warning(
                "%s: data_sources.%s has key(s) this version does not use: %s. "
                "They are kept (legacy configs must still parse) but nothing "
                "reads them.",
                path, source_key, ", ".join(unknown),
            )

    return specs


__all__ = [
    "ColumnSpec",
    "DomainHint",
    "FORBIDDEN_KEY_PATTERN",
    "KNOWN_TRANSFORMS",
    "MAPPING_COLUMNS",
    "SourceSpec",
    "TransformSpec",
    "load_source_specs",
]