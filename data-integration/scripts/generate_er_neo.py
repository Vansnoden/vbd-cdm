"""Render the publication ER diagram from a hand-arranged DOT grid."""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from sqlalchemy import MetaData  # noqa: E402

from database import models  # noqa: E402,F401
from database.session import engine  # noqa: E402

OUTPUT_FILE = "er_diagram_publication.png"
TEMP_DOT = "temp_schema.dot"

EXCLUDE_TABLES = {"alembic_version"}

GRID_ROWS = [
    ["dim_location", "dim_time", "dim_source"],
    ["dim_disease", "dim_identification_method", "dim_socioeconomic_indicator"],
    ["fact_disease_case", "fact_disease_burden", "fact_health_facility"],
    ["fact_intervention", "fact_environment", "fact_socioeconomic_condition"],
    ["dim_vector", "dim_insecticide", "dim_resistance_test_type"],
    ["fact_vector_observation", "fact_bionomics", "fact_insecticide_resistance"],
]

DOT_HEADER = [
    "digraph ERD {",
    "    rankdir=TB;",
    "    newrank=true;",
    "    graph [",
    '        splines=ortho,',
    '        nodesep=0.55,',
    '        ranksep=1.0,',
    '        fontname="Helvetica",',
    '        dpi=300',
    "    ];",
    "    node [",
    '        shape=rectangle,',
    '        style=filled,',
    '        fillcolor="#F8F9FA",',
    '        color="#333333",',
    '        fontname="Helvetica-Bold",',
    '        fontsize=8,',
    '        penwidth=1.0,',
    '        margin="0.12,0.06"',
    "    ];",
    "    edge [",
    '        color="#666666",',
    '        arrowsize=0.5,',
    '        penwidth=0.8,',
    '        fontname="Helvetica",',
    '        fontsize=7',
    "    ];",
    "",
]


def build_dot(metadata: MetaData) -> str:
    """The DOT source for ``metadata``: nodes, rank rows, then FK edges."""
    lines = list(DOT_HEADER)

    for table_name, table in metadata.tables.items():
        if table_name in EXCLUDE_TABLES:
            continue
        pk_columns = [c.name for c in table.primary_key.columns]
        pk_suffix = f"\\n({', '.join(pk_columns)})" if pk_columns else ""
        fill = "#E9ECEF" if table_name.startswith("fact_") else "#FFFFFF"
        lines.append(
            f'    "{table_name}" '
            f'[label="{table_name}{pk_suffix}", fillcolor="{fill}"];')

    lines.append("")

    for row in GRID_ROWS:
        present = [t for t in row if t in metadata.tables]
        if not present:
            continue
        lines.append(f"    {{ rank=same; {' '.join(present)} }}")
        for left, right in zip(present, present[1:]):
            lines.append(
                f'    "{left}" -> "{right}" [style=invis, constraint=false];')

    lines.append("")

    for table_name, table in metadata.tables.items():
        if table_name in EXCLUDE_TABLES:
            continue
        for foreign_key in table.foreign_keys:
            target_table = foreign_key.column.table.name
            if target_table in EXCLUDE_TABLES:
                continue
            lines.append(f'    "{target_table}" -> "{table_name}";')

    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    """Render the diagram. Returns a process exit status."""
    metadata = MetaData()
    metadata.reflect(bind=engine)

    with open(TEMP_DOT, "w") as handle:
        handle.write(build_dot(metadata))

    try:
        subprocess.run(
            ["dot", "-Tpng", TEMP_DOT, "-o", OUTPUT_FILE],
            check=True)
        print(f"✅ ER diagram with obstacle-avoiding orthogonal routing: "
              f"{OUTPUT_FILE}")
        return 0
    finally:
        if os.path.exists(TEMP_DOT):
            os.remove(TEMP_DOT)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())