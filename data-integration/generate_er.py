import os
from pathlib import Path
import sys
import subprocess
import re

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from database.session import engine
from database import models  # noqa: F401

from eralchemy import render_er

output_file = "er_diagram.png"
db_url = str(engine.url)

temp_dot = "temp_diagram.dot"
temp_unflattened = "temp_unflattened.dot"

# 1. Generate initial DOT file
render_er(db_url, temp_dot)

# 2. Read and modify DOT content
with open(temp_dot, "r") as f:
    dot_content = f.read()

# Remove existing rankdir definitions inserted by ERAlchemy
dot_content = re.sub(r'rankdir\s*=\s*["\']?\w+["\']?;?', '', dot_content)

# Attributes to force a compact, high-DPI layout suited for A4
graph_attributes = """
    /* Global graph attributes */
    graph [
        ratio="fill",
        size="8.0,11.0!",
        dpi=300,
        nodesep=0.4,
        ranksep=0.5,
        overlap=false,
        splines=polyline,
        pack=true,
        packmode="node"
    ];
    node [fontsize=10];
"""

# Inject attributes right after 'digraph {'
dot_content = dot_content.replace("digraph {", f"digraph {{\n{graph_attributes}")

with open(temp_dot, "w") as f:
    f.write(dot_content)

try:
    # 3. Run Graphviz 'unflatten' to break long vertical stacks into a balanced grid
    # -f: enables staggering
    # -l 3: limits max rank length to 3 nodes wide/deep
    # -c 3: connects staggered ranks efficiently
    subprocess.run(
        ["unflatten", "-f", "-l3", "-c3", temp_dot, "-o", temp_unflattened],
        check=True
    )

    # 4. Render to final PNG using dot
    subprocess.run(
        ["dot", "-Tpng", temp_unflattened, "-o", output_file],
        check=True
    )
    
    print(f"✅ Grid-optimized ER diagram saved to {output_file}")

finally:
    # Cleanup temporary files
    for temp_file in [temp_dot, temp_unflattened]:
        if os.path.exists(temp_file):
            os.remove(temp_file)
