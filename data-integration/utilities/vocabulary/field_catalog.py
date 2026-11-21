# -*- coding: utf-8 -*-
"""The CDM's resolution surface: which target fields a vocabulary may govern.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Iterator

from database import models  # noqa: F401  (registers every table)
from database.session import Base

_CONCEPT_TABLE = "dim_concept"
_CONCEPT_COLUMN = "concept_id"

_STAMP_COLUMNS = ("concept_id", "standard_code")


class FieldNotInCatalog(KeyError):
    """Raised when a target field is not a resolvable CDM field.
    """

    def __init__(self, target_field: str, candidates: tuple[str, ...] = ()) -> None:
        self.target_field = target_field
        self.candidates = tuple(candidates)
        super().__init__(target_field, self.candidates)

    def __str__(self) -> str:
        hint = (
            "; did you mean: " + ", ".join(self.candidates)
            if self.candidates else ""
        )
        return f"{self.target_field!r} is not in FIELD_CATALOG{hint}"


@dataclass(frozen=True)
class FieldBinding:
    """What one target field resolves to."""

    target_field: str       
    domain: str             
    dimension_table: str    
    dimension_key: str      
    value_type: str        

    def as_dict(self) -> dict[str, str]:
        """The dict shape ``FIELD_CATALOG`` exposes, for symmetry."""
        return {
            "domain": self.domain,
            "dimension_table": self.dimension_table,
            "dimension_key": self.dimension_key,
            "value_type": self.value_type,
        }


def _dim(target_field: str, domain: str, table: str, key: str,
         value_type: str = "text") -> FieldBinding:
    return FieldBinding(target_field=target_field, domain=domain,
                        dimension_table=table, dimension_key=key,
                        value_type=value_type)


def _fact(table: str, column: str, domain: str, key: str) -> FieldBinding:
    """A free-text fact column. Its key is the fact table's own primary key."""
    return FieldBinding(target_field=f"{table}.{column}", domain=domain,
                        dimension_table=table, dimension_key=key,
                        value_type="text")


_BINDINGS: tuple[FieldBinding, ...] = (
    # ---- location ----------------------------------------------------
    _dim("dim_location.name", "location", "dim_location", "location_id"),
    _dim("dim_location.country_code", "location", "dim_location", "location_id",
         value_type="iso3166_alpha2"),
    # ---- vector ------------------------------------------------------
    _dim("dim_vector.species_name", "vector", "dim_vector", "vector_code"),
    # ---- disease -----------------------------------------------------
    _dim("dim_disease.disease_name", "disease", "dim_disease", "disease_code"),
    # ---- insecticide -------------------------------------------------
    _dim("dim_insecticide.insecticide_name", "insecticide", "dim_insecticide",
         "insecticide_code"),
    # ---- diagnostic method -------------------------------------------
    _dim("dim_identification_method.method_name", "diagnostic_method",
         "dim_identification_method", "method_code"),
    # ---- resistance test type ----------------------------------------
    _dim("dim_resistance_test_type.test_type_name", "resistance_test_type",
         "dim_resistance_test_type", "test_type_code"),
    # ---- socioeconomic indicator -------------------------------------
    _dim("dim_socioeconomic_indicator.indicator_name",
         "socioeconomic_indicator", "dim_socioeconomic_indicator",
         "indicator_code"),
    # ---- data source -------------------------------------------------
    _dim("dim_source.source_name", "source", "dim_source", "source_code"),

    # ---- vector observation, free text -------------------------------
    _fact("fact_vector_observation", "life_stage", "life_stage",
          "observation_id"),
    _fact("fact_vector_observation", "sex", "vector_sex", "observation_id"),
    _fact("fact_vector_observation", "collection_method", "collection_method",
          "observation_id"),
    _fact("fact_vector_observation", "tested_for_pathogen", "pathogen_tested",
          "observation_id"),
    _fact("fact_vector_observation", "infection_status", "infection_status",
          "observation_id"),
    # ---- resistance, free text ---------------------------------------
    _fact("fact_insecticide_resistance", "vgsc_generation", "vgsc_generation",
          "resistance_id"),
    # ---- bionomics, free text ----------------------------------------
    _fact("fact_bionomics", "feeding_preference", "feeding_preference",
          "bionomics_id"),
    _fact("fact_bionomics", "biting_location", "biting_location",
          "bionomics_id"),
    _fact("fact_bionomics", "resting_behavior", "resting_behavior",
          "bionomics_id"),
    _fact("fact_bionomics", "larval_site_type", "larval_site_type",
          "bionomics_id"),
    # ---- disease case, free text -------------------------------------
    _fact("fact_disease_case", "case_status", "case_status", "case_id"),
    _fact("fact_disease_case", "host_type", "host_type", "case_id"),
    _fact("fact_disease_case", "age_group", "age_group", "case_id"),
    _fact("fact_disease_case", "sex", "case_sex", "case_id"),
    _fact("fact_disease_case", "outcome", "case_outcome", "case_id"),
    # ---- intervention, free text -------------------------------------
    _fact("fact_intervention", "intervention_type", "intervention_type",
          "intervention_id"),
)

_BY_FIELD: dict[str, FieldBinding] = {b.target_field: b for b in _BINDINGS}

if len(_BY_FIELD) != len(_BINDINGS):
    raise RuntimeError(
        "FIELD_CATALOG contains duplicate target_field entries; "
        "_BINDINGS must key uniquely"
    )


FIELD_CATALOG: dict[str, dict[str, str]] = {
    b.target_field: b.as_dict() for b in _BINDINGS
}


def _build_domain_map() -> dict[str, str]:
    """Derive domain -> table, asserting the mapping is a function.
    """
    reverse: dict[str, str] = {}
    for binding in _BINDINGS:
        existing = reverse.get(binding.domain)
        if existing is not None and existing != binding.dimension_table:
            raise RuntimeError(
                f"domain {binding.domain!r} maps to more than one table "
                f"({existing!r} and {binding.dimension_table!r}); "
                f"DOMAIN_TO_DIM_TABLE must be a function"
            )
        reverse[binding.domain] = binding.dimension_table
    return reverse


DOMAIN_TO_DIM_TABLE: dict[str, str] = _build_domain_map()


def resolve_field(target_field: str) -> FieldBinding:
    """Return the binding for ``target_field``.
    """
    key = (target_field or "").strip()
    binding = _BY_FIELD.get(key)
    if binding is not None:
        return binding

    candidates = tuple(
        difflib.get_close_matches(key, sorted(_BY_FIELD), n=5, cutoff=0.5)
    )
    raise FieldNotInCatalog(key, candidates)


def can_stamp(target_field: str) -> bool:
    """Whether ``target_field``'s table can hold ``concept_id`` and
    ``standard_code``.
    """
    try:
        binding = resolve_field(target_field)
    except FieldNotInCatalog:
        return False
    table = Base.metadata.tables.get(binding.dimension_table)
    if table is None:
        return False
    return all(column in table.columns for column in _STAMP_COLUMNS)


def fields_for_domain(domain: str) -> tuple[str, ...]:
    """Catalog fields bound to ``domain``, sorted. Deterministic."""
    return tuple(sorted(b.target_field for b in _BINDINGS if b.domain == domain))


def fields_for_dimension_table(table_name: str) -> tuple[str, ...]:
    """Catalog fields attached to ``table_name``, sorted. Deterministic."""
    return tuple(
        sorted(b.target_field for b in _BINDINGS if b.dimension_table == table_name)
    )


def dimension_fields() -> tuple[str, ...]:
    """Catalog fields on a ``dim_*`` table, sorted. These can be stamped."""
    return tuple(sorted(b.target_field for b in _BINDINGS
                        if b.dimension_table.startswith("dim_")))


def fact_value_fields() -> tuple[str, ...]:
    """Catalog fields on a ``fact_*`` table, sorted. These cannot be stamped."""
    return tuple(sorted(b.target_field for b in _BINDINGS
                        if b.dimension_table.startswith("fact_")))


def domains() -> tuple[str, ...]:
    """Every domain in the catalog, sorted."""
    return tuple(sorted(DOMAIN_TO_DIM_TABLE))


def iter_bindings() -> Iterator[FieldBinding]:
    """Iterate bindings in sorted field order (never declaration order)."""
    for name in sorted(_BY_FIELD):
        yield _BY_FIELD[name]


def _references_concept(table) -> bool:
    """Whether ``table``'s ``concept_id`` column is a foreign key to ``dim_concept``.
    """
    column = table.columns.get(_CONCEPT_COLUMN)
    if column is None:
        return False
    try:
        targets = {
            fk.column.table.name
            for fk in column.foreign_keys
            if fk.column is not None
        }
    except Exception:  # noqa: BLE001 - an unresolvable FK is not a reference
        return False
    return _CONCEPT_TABLE in targets


def _concept_reference_tables() -> tuple[str, ...]:
    """Every ``dim_*`` table carrying a ``concept_id`` FK to ``dim_concept``.

    Excludes ``dim_concept`` itself. Sorted, so the diagnostic is stable.
    """
    return tuple(sorted(
        table.name
        for table in Base.metadata.tables.values()
        if table.name.startswith("dim_")
        and table.name != _CONCEPT_TABLE
        and _references_concept(table)
    ))


#: Kept as an alias so an out-of-tree import of the old name still works.
_concept_id_tables = _concept_reference_tables


def _assert_concept_id_coverage() -> None:
    """Fail import if a concept-stampable dimension has no catalog binding."""
    bound_tables = {b.dimension_table for b in _BINDINGS}
    uncovered = [
        table for table in _concept_reference_tables()
        if table not in bound_tables
    ]
    if uncovered:
        raise RuntimeError(
            "FIELD_CATALOG is missing a binding for every dim_* table that holds "
            "a concept_id foreign key. Uncovered: "
            + ", ".join(uncovered)
            + ". Add at least one entry for each to "
            "utilities/vocabulary/field_catalog.py::_BINDINGS."
        )


_assert_concept_id_coverage()


__all__ = [
    "DOMAIN_TO_DIM_TABLE",
    "FIELD_CATALOG",
    "FieldBinding",
    "FieldNotInCatalog",
    "can_stamp",
    "dimension_fields",
    "domains",
    "fact_value_fields",
    "fields_for_dimension_table",
    "fields_for_domain",
    "iter_bindings",
    "resolve_field",
]