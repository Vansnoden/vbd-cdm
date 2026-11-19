# -*- coding: utf-8 -*-
"""The CDM's resolution surface: which target fields a vocabulary may govern.

``FIELD_CATALOG`` maps a target field (``dim_vector.species_name``) to its
domain, dimension table, dimension key and value type. It is derived from the
schema, never from user configuration.

A field belongs here when it is either

* a human-readable label column on a ``dim_*`` table that carries a
  ``concept_id`` **foreign key** — the column a resolved concept is stamped
  onto; or
* a discriminating key column whose values are themselves vocabulary
  (``dim_location.country_code`` is the only current example).

The tables' own natural-code columns (``vector_code``, ``disease_code``, ...)
are deliberately absent. Codes are *assigned* by ``DimensionKeyIndex`` and
rewritten by ``_update_fact_references``; they are never resolved *to* a
concept, and listing them would invite a mapper to try.

``_assert_concept_id_coverage()`` runs at import and refuses to load the module
if a dimension that can be stamped has no binding. The test is "holds a foreign
key to ``dim_concept.concept_id``", not "has a column called concept_id":
``dim_concept`` itself has such a column and it is that table's primary key, so
counting it demanded a catalog entry for the vocabulary table — which resolves
nothing and is not a dimension.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Iterator

from database import models  # noqa: F401  (registers every table)
from database.session import Base

#: The concept dictionary and the column a dimension points at.
_CONCEPT_TABLE = "dim_concept"
_CONCEPT_COLUMN = "concept_id"


class FieldNotInCatalog(KeyError):
    """Raised when a target field is not a resolvable CDM field.

    Carries ``difflib`` candidates so the message can suggest what the caller
    probably meant instead of only saying no.
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

    target_field: str       # 'dim_vector.species_name'
    domain: str             # 'vector'
    dimension_table: str    # 'dim_vector'
    dimension_key: str      # 'vector_code' — the column a fact row's FK holds
    value_type: str         # 'text' | 'iso3166_alpha2'

    def as_dict(self) -> dict[str, str]:
        """The dict shape ``FIELD_CATALOG`` exposes, for symmetry."""
        return {
            "domain": self.domain,
            "dimension_table": self.dimension_table,
            "dimension_key": self.dimension_key,
            "value_type": self.value_type,
        }


_BINDINGS: tuple[FieldBinding, ...] = (
    # ---- location ----------------------------------------------------
    FieldBinding(
        target_field="dim_location.name",
        domain="location",
        dimension_table="dim_location",
        dimension_key="location_id",
        value_type="text",
    ),
    FieldBinding(
        target_field="dim_location.country_code",
        domain="location",
        dimension_table="dim_location",
        dimension_key="location_id",
        value_type="iso3166_alpha2",
    ),
    # ---- vector ------------------------------------------------------
    FieldBinding(
        target_field="dim_vector.species_name",
        domain="vector",
        dimension_table="dim_vector",
        dimension_key="vector_code",
        value_type="text",
    ),
    # ---- disease -----------------------------------------------------
    FieldBinding(
        target_field="dim_disease.disease_name",
        domain="disease",
        dimension_table="dim_disease",
        dimension_key="disease_code",
        value_type="text",
    ),
    # ---- insecticide -------------------------------------------------
    FieldBinding(
        target_field="dim_insecticide.insecticide_name",
        domain="insecticide",
        dimension_table="dim_insecticide",
        dimension_key="insecticide_code",
        value_type="text",
    ),
    # ---- diagnostic method -------------------------------------------
    FieldBinding(
        target_field="dim_identification_method.method_name",
        domain="diagnostic_method",
        dimension_table="dim_identification_method",
        dimension_key="method_code",
        value_type="text",
    ),
    # ---- resistance test type ----------------------------------------
    FieldBinding(
        target_field="dim_resistance_test_type.test_type_name",
        domain="resistance_test_type",
        dimension_table="dim_resistance_test_type",
        dimension_key="test_type_code",
        value_type="text",
    ),
    # ---- socioeconomic indicator -------------------------------------
    FieldBinding(
        target_field="dim_socioeconomic_indicator.indicator_name",
        domain="socioeconomic_indicator",
        dimension_table="dim_socioeconomic_indicator",
        dimension_key="indicator_code",
        value_type="text",
    ),
    # ---- data source -------------------------------------------------
    FieldBinding(
        target_field="dim_source.source_name",
        domain="source",
        dimension_table="dim_source",
        dimension_key="source_code",
        value_type="text",
    ),
)

_BY_FIELD: dict[str, FieldBinding] = {b.target_field: b for b in _BINDINGS}

if len(_BY_FIELD) != len(_BINDINGS):
    raise RuntimeError(
        "FIELD_CATALOG contains duplicate target_field entries; "
        "_BINDINGS must key uniquely"
    )


#: Public, contract-shaped view: field -> {domain, dimension_table,
#: dimension_key, value_type}.
FIELD_CATALOG: dict[str, dict[str, str]] = {
    b.target_field: b.as_dict() for b in _BINDINGS
}


def _build_domain_map() -> dict[str, str]:
    """Derive domain -> dimension table, asserting the mapping is a function."""
    reverse: dict[str, str] = {}
    for binding in _BINDINGS:
        existing = reverse.get(binding.domain)
        if existing is not None and existing != binding.dimension_table:
            raise RuntimeError(
                f"domain {binding.domain!r} maps to more than one dimension "
                f"table ({existing!r} and {binding.dimension_table!r}); "
                f"DOMAIN_TO_DIM_TABLE must be a function"
            )
        reverse[binding.domain] = binding.dimension_table
    return reverse


#: domain -> the single dim table a resolved concept for that domain lives in.
DOMAIN_TO_DIM_TABLE: dict[str, str] = _build_domain_map()


def resolve_field(target_field: str) -> FieldBinding:
    """Return the binding for ``target_field``.

    Raises
    ------
    FieldNotInCatalog
        With up to five ``difflib`` candidates, so a caller sees
        "did you mean dim_vector.species_name" rather than a bare ``KeyError``.
    """
    key = (target_field or "").strip()
    binding = _BY_FIELD.get(key)
    if binding is not None:
        return binding

    candidates = tuple(
        difflib.get_close_matches(key, sorted(_BY_FIELD), n=5, cutoff=0.5)
    )
    raise FieldNotInCatalog(key, candidates)


def fields_for_domain(domain: str) -> tuple[str, ...]:
    """Catalog fields bound to ``domain``, sorted. Deterministic."""
    return tuple(sorted(b.target_field for b in _BINDINGS if b.domain == domain))


def fields_for_dimension_table(table_name: str) -> tuple[str, ...]:
    """Catalog fields attached to ``table_name``, sorted. Deterministic."""
    return tuple(
        sorted(b.target_field for b in _BINDINGS if b.dimension_table == table_name)
    )


def domains() -> tuple[str, ...]:
    """Every domain in the catalog, sorted."""
    return tuple(sorted(DOMAIN_TO_DIM_TABLE))


def iter_bindings() -> Iterator[FieldBinding]:
    """Iterate bindings in sorted field order (never declaration order)."""
    for name in sorted(_BY_FIELD):
        yield _BY_FIELD[name]


def _references_concept(table) -> bool:
    """Whether ``table``'s ``concept_id`` column is a foreign key to ``dim_concept``.

    A column *named* ``concept_id`` is not enough. ``dim_concept.concept_id`` is
    that table's primary key — the identity of a concept — so it has no foreign
    keys at all and is correctly reported as not referencing a concept.
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
    """Every ``dim_*`` table that carries a ``concept_id`` FK to ``dim_concept``.

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
    """Fail import if a concept-stampable dimension has no catalog binding.

    This is the drift guard: add a ``dim_*`` table with a ``concept_id`` foreign
    key and forget the catalog, and the process will not start.
    """
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
    "domains",
    "fields_for_dimension_table",
    "fields_for_domain",
    "iter_bindings",
    "resolve_field",
]