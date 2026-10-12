import os
from pathlib import Path
import sys
import subprocess

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from database.session import engine
from database import models  # noqa: F401
from sqlalchemy import MetaData

output_file = "er_diagram_publication.png"
temp_dot = "temp_schema.dot"

metadata = MetaData()
metadata.reflect(bind=engine)

EXCLUDE_TABLES = {"alembic_version"}

# Each inner list is one row of the grid, left to right.
GRID_ROWS = [
    ["dim_location", "dim_time", "dim_source"],
    ["dim_disease", "dim_identification_method", "dim_socioeconomic_indicator"],
    ["fact_disease_case", "fact_disease_burden", "fact_health_facility"],
    ["fact_intervention", "fact_environment", "fact_socioeconomic_condition"],
    ["dim_vector", "dim_insecticide", "dim_resistance_test_type"],
    ["fact_vector_observation", "fact_bionomics", "fact_insecticide_resistance"],
]

dot_lines = [
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

# 1. Emit every table as a node, with its PKs in the label.
for table_name, table in metadata.tables.items():
    if table_name in EXCLUDE_TABLES:
        continue

    pk_cols = [c.name for c in table.primary_key.columns]
    pk_str = f"\\n({', '.join(pk_cols)})" if pk_cols else ""
    fill = "#E9ECEF" if table_name.startswith("fact_") else "#FFFFFF"

    dot_lines.append(
        f'    "{table_name}" '
        f'[label="{table_name}{pk_str}", fillcolor="{fill}"];'
    )

dot_lines.append("")

# 2. Rank groups: one rank=same block per row.
#    Invisible edges inside each block force left-to-right column order.
for row in GRID_ROWS:
    present = [t for t in row if t in metadata.tables]
    if not present:
        continue
    dot_lines.append(f"    {{ rank=same; {' '.join(present)} }}")
    # Force column order with invisible constraint-free edges.
    for left, right in zip(present, present[1:]):
        dot_lines.append(
            f'    "{left}" -> "{right}" [style=invis, constraint=false];'
        )

dot_lines.append("")

# 3. Real foreign-key edges.
for table_name, table in metadata.tables.items():
    if table_name in EXCLUDE_TABLES:
        continue
    for fk in table.foreign_keys:
        target_table = fk.column.table.name
        if target_table in EXCLUDE_TABLES:
            continue
        dot_lines.append(f'    "{target_table}" -> "{table_name}";')

dot_lines.append("}")

with open(temp_dot, "w") as f:
    f.write("\n".join(dot_lines))

try:
    subprocess.run(["dot", "-Tpng", temp_dot, "-o", output_file], check=True)
    print(f"✅ ER diagram with obstacle-avoiding orthogonal routing: {output_file}")
finally:
    if os.path.exists(temp_dot):
        os.remove(temp_dot)
