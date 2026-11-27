import pytest
from pydantic import BaseModel

from database import models, schemas


def _max_length(field_info) -> int | None:
    """The declared ``max_length`` on a Pydantic v2 field, if any."""
    for constraint in getattr(field_info, "metadata", ()) or ():
        if type(constraint).__name__ == "MaxLen":
            return int(constraint.max_length)
    return None


def _create_schemas():
    """Every ``*Create`` schema defined in ``database.schemas``."""
    for name in sorted(dir(schemas)):
        if not name.endswith("Create"):
            continue
        candidate = getattr(schemas, name)
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            yield name, candidate


def test_every_create_schema_has_a_model():
    """The test below is only meaningful if every Create schema maps to a table."""
    missing = [name for name, _ in _create_schemas()
               if not hasattr(models, name[:-len("Create")])]
    assert missing == []


@pytest.mark.parametrize("name,schema", list(_create_schemas()),
                         ids=lambda value: value if isinstance(value, str) else "")
def test_declared_max_length_is_at_least_the_column_length(name, schema):
    """No ``*Create`` field may be narrower than its DDL column."""
    model = getattr(models, name[:-len("Create")])
    columns = model.__table__.columns
    narrowed = []
    for field_name, field_info in schema.model_fields.items():
        declared = _max_length(field_info)
        column = columns.get(field_name)
        if declared is None or column is None:
            continue
        column_length = getattr(column.type, "length", None)
        if column_length is None:
            continue
        if declared < int(column_length):
            narrowed.append((field_name, declared, int(column_length)))
    assert narrowed == [], (
        f"{name}: field(s) narrower than the DDL column "
        f"(field, max_length, String(...)): {narrowed}")


def test_negative_concentration_is_rejected():
    """A sentinel must not be constructible: NULL is the only 'no concentration'."""
    with pytest.raises(ValueError, match="concentration cannot be negative"):
        schemas.FactInsecticideResistanceCreate(
            location_id=1, date_id="2020-01-01", vector_code="V1",
            insecticide_code="I1", test_type_code="T1", source_code="S1",
            concentration=-1.0)