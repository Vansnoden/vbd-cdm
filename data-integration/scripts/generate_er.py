"""Render a compact ER diagram of the CDM with ERAlchemy and Graphviz."""

import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from eralchemy import render_er  # noqa: E402

from database import models  # noqa: E402,F401
from database.session import engine  # noqa: E402

OUTPUT_FILE = PROJECT_ROOT / "er_diagram.png"
TEMP_DOT = PROJECT_ROOT / "temp_diagram.dot"
TEMP_UNFLATTENED = PROJECT_ROOT / "temp_unflattened.dot"

GRAPH_ATTRIBUTES = """
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


def main() -> int:
    """Render the diagram. Returns a process exit status."""
    render_er(str(engine.url), str(TEMP_DOT))

    with open(TEMP_DOT, "r") as handle:
        dot_content = handle.read()

    dot_content = re.sub(r'rankdir\s*=\s*["\']?\w+["\']?;?', '', dot_content)
    dot_content = dot_content.replace("digraph {", f"digraph {{\n{GRAPH_ATTRIBUTES}")

    with open(TEMP_DOT, "w") as handle:
        handle.write(dot_content)

    try:
        subprocess.run(
            ["unflatten", "-f", "-l3", "-c3", str(TEMP_DOT),
             "-o", str(TEMP_UNFLATTENED)],
            check=True)
        subprocess.run(
            ["dot", "-Tpng", str(TEMP_UNFLATTENED), "-o", str(OUTPUT_FILE)],
            check=True)
        print(f"✅ Grid-optimized ER diagram saved to {OUTPUT_FILE}")
        return 0
    finally:
        for temporary in (TEMP_DOT, TEMP_UNFLATTENED):
            if temporary.exists():
                temporary.unlink()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())