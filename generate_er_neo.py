import os
from pathlib import Path
import sys
import subprocess

# Add project root to path
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

# 3-column x 6-row structured layout
GRID_LAYOUT = {
    # Row 0: Shared Dimensions (Top)
    "dim_location": (0, 0),
    "dim_time": (1, 0),
    "dim_source": (2, 0),

    # Row 1: Disease & Health Dimensions
    "dim_disease": (0, 1),
    "dim_identification_method": (1, 1),
    "dim_socioeconomic_indicator": (2, 1),

    # Row 2: Core Fact Tables
    "fact_disease_case": (0, 2),
    "fact_disease_burden": (1, 2),
    "fact_health_facility": (2, 2),

    # Row 3: Environmental & Intervention Facts
    "fact_intervention": (0, 3),
    "fact_environment": (1, 3),
    "fact_socioeconomic_condition": (2, 3),

    # Row 4: Vector & Insecticide Dimensions
    "dim_vector": (0, 4),
    "dim_insecticide": (1, 4),
    "dim_resistance_test_type": (2, 4),

    # Row 5: Vector Domain Facts (Bottom)
    "fact_vector_observation": (0, 5),
    "fact_bionomics": (1, 5),
    "fact_insecticide_resistance": (2, 5),
}

# Grid spacing in INCHES (Multiplied by 72 to convert to Graphviz points)
INCH = 72
X_SPACING = 2.6 * INCH   # ~187 points horizontally
Y_SPACING = 1.1 * INCH   # ~79 points vertically

# Canvas margin offset
X_OFFSET = 1.0 * INCH
Y_OFFSET = 1.0 * INCH

dot_lines = [
    "digraph ERD {",
    '    graph [',
    '        layout=neato,',
    '        dpi=300,',
    '        splines=true,            /* Smooth, non-overlapping edge curves */',
    '        overlap=false,',
    '        fontname="Helvetica"',
    '    ];',
    '    node [',
    '        fontname="Helvetica-Bold",',
    '        fontsize=8,',
    '        shape=rectangle,',
    '        style="filled",',
    '        fillcolor="#F8F9FA",',
    '        color="#333333",',
    '        penwidth=1.0,',
    '        margin="0.12,0.06"',
    '    ];',
    '    edge [',
    '        fontname="Helvetica",',
    '        fontsize=7,',
    '        color="#666666",',
    '        arrowsize=0.5,',
    '        penwidth=0.8',
    '    ];',
    ""
]

# 1. Add Nodes with exact coordinates in points
for table_name, table in metadata.tables.items():
    if table_name in EXCLUDE_TABLES:
        continue
    
    pk_cols = [c.name for c in table.primary_key.columns]
    pk_str = f"\\n({', '.join(pk_cols)})" if pk_cols else ""
    
    col, row = GRID_LAYOUT.get(table_name, (0, 0))
    pos_x = X_OFFSET + (col * X_SPACING)
    pos_y = Y_OFFSET + ((5 - row) * Y_SPACING)  # Invert Y so row 0 is top
    
    fill = "#E9ECEF" if table_name.startswith("fact_") else "#FFFFFF"
    
    dot_lines.append(
        f'    "{table_name}" ['
        f'label="{table_name}{pk_str}", '
        f'fillcolor="{fill}", '
        f'pos="{pos_x:.1f},{pos_y:.1f}!"'
        f'];'
    )

dot_lines.append("")

# 2. Add Edges
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
    # Render using neato -n2 (respects point coordinates and routes edges around boxes)
    subprocess.run(["neato", "-n2", "-Tpng", temp_dot, "-o", output_file], check=True)
    print(f"✅ Perfectly scaled 3-column A4 ER diagram saved to {output_file}")

finally:
    if os.path.exists(temp_dot):
        os.remove(temp_dot)
