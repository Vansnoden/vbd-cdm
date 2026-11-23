import pytest

from utilities.burden_expansion import BURDEN_KERNEL_KEY, load_burden_kernel
from utilities.config.source_spec import load_source_specs
from tests.helpers import PackRegistry, parameter_pack


def _write(tmp_path, body):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_matching_and_columns_together_fails(tmp_path):
    path = _write(tmp_path, """
data_sources:
  broken:
    source_name: broken
    extension: csv
    path: data.csv
    matching: mappings/x.mapping.csv
    columns:
      - target_table: dim_source
        target_column: source_code
        static_value: SRC
""")

    with pytest.raises(ValueError, match="both `matching:` and"):
        load_source_specs(path)


def test_neither_matching_nor_columns_fails(tmp_path):
    path = _write(tmp_path, """
data_sources:
  broken:
    source_name: broken
    extension: csv
    path: data.csv
""")

    with pytest.raises(ValueError, match="neither `matching:` nor"):
        load_source_specs(path)


def test_vocabulary_key_under_data_sources_fails(tmp_path):
    path = _write(tmp_path, """
data_sources:
  broken:
    source_name: broken
    extension: csv
    path: data.csv
    vocabulary:
      pack: gadm
    columns:
      - target_table: dim_source
        target_column: source_code
        static_value: SRC
""")

    with pytest.raises(ValueError, match="vocabulary configuration is not accepted"):
        load_source_specs(path)


def test_unknown_burden_expansion_key_fails():
    registry = PackRegistry([
        parameter_pack({BURDEN_KERNEL_KEY: {
            "seasonality": {"1": 1.2},
            "national_alocation": True,
        }})])

    with pytest.raises(ValueError, match="national_alocation"):
        load_burden_kernel(registry)


def test_unknown_burden_expansion_section_key_fails():
    registry = PackRegistry([
        parameter_pack({BURDEN_KERNEL_KEY: {
            "seasonality": {"1": 1.2},
            "write": {"grains": "admin2"},
        }})])

    with pytest.raises(ValueError, match="grains"):
        load_burden_kernel(registry)


def test_minimal_spec_parses(tmp_path):
    path = _write(tmp_path, """
data_sources:
  ok:
    source_name: ok
    extension: csv
    path: data.csv
    columns:
      - target_table: dim_source
        target_column: source_code
        static_value: SRC
""")

    specs = load_source_specs(path)

    assert list(specs) == ["ok"]
    assert specs["ok"].uses_inline_columns