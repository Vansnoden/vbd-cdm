#!/bin/bash
# bundle_py.sh – Bundle all Python files (.py) from the project into one text file.

OUTPUT_FILE="python_bundle.txt"
> "$OUTPUT_FILE"   # clear/create the output file

# Check if we are inside a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "Error: Not inside a git repository. Cannot use git ls-files."
    exit 1
fi

echo "Generating bundle of Python files from git-tracked and untracked (non-ignored) files..."

# List all files:
#   --cached       : tracked files
#   --others       : untracked files
#   --exclude-standard : respect .gitignore
git ls-files --cached --others --exclude-standard | while IFS= read -r file; do
    # Only include files with .py extension
    if [[ "$file" == *.py ]]; then
        if [ -f "$file" ]; then
            # Write a comment with the relative file path
            echo "# $file" >> "$OUTPUT_FILE"
            # Append the file content
            cat "$file" >> "$OUTPUT_FILE"
            # Add a blank line for separation
            echo >> "$OUTPUT_FILE"
        fi
    fi
done

echo "Bundle created: $(pwd)/$OUTPUT_FILE"