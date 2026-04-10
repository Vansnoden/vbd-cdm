# generate_er_horizontal_simple.py
import os
from pathlib import Path
import sys
import subprocess

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from database.session import engine
from database import models  # noqa: F401

from eralchemy import render_er

# Output file name
output_file = "er_diagram.png"

# Convert engine to database URL string
db_url = str(engine.url)

# First generate a temporary DOT file
temp_dot = 'temp_diagram.dot'
render_er(db_url, temp_dot)

# Read the DOT file and add rankdir=LR
with open(temp_dot, 'r') as f:
    dot_content = f.read()

# Insert rankdir after the opening line
dot_content = dot_content.replace(
    'digraph {',
    'digraph {\n    rankdir=LR;'
)

# Write back the modified DOT
with open(temp_dot, 'w') as f:
    f.write(dot_content)

# Use Graphviz to render the PNG
try:
    subprocess.run(['dot', '-Tpng', temp_dot, '-o', output_file], check=True)
    print(f"✅ Horizontal ER diagram saved to {output_file}")
    
    # Clean up temp file
    os.remove(temp_dot)
    
except FileNotFoundError:
    print("❌ Graphviz 'dot' command not found. Please install Graphviz")
    
except subprocess.CalledProcessError as e:
    print(f"❌ Error rendering diagram: {e}")