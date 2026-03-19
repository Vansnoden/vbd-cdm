#!/usr/bin/env python3
"""
generate_er_horizontal.py
Generates a horizontally oriented ER diagram from SQLAlchemy models.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from database.session import engine, Base
from database import models  # noqa: F401 – ensures all models are loaded

from sqlalchemy_schemadisplay import create_schema_graph

def main():
    # Create the graph object from database schema
    graph = create_schema_graph(
        engine,
        metadata=Base.metadata,
        show_datatypes=True,      # show column types
        show_indexes=False,       # hide indexes for cleaner diagram
    )

    # --- Graph layout settings for horizontal orientation ---
    graph.set_rankdir('LR')          # left-to-right
    graph.set_splines('ortho')       # orthogonal edges (straight lines)
    graph.set_nodesep(0.8)           # horizontal space between nodes
    graph.set_ranksep(1.0)           # vertical space between ranks

    # Optional styling
    graph.set_node_defaults(shape='box', fontname='Arial', fontsize='10')
    graph.set_edge_defaults(arrowsize='0.8')

    # Save to file
    output_file = "er_diagram_horizontal.png"
    graph.write_png(output_file)
    print(f"✅ Horizontal ER diagram saved to {output_file}")

if __name__ == "__main__":
    main()