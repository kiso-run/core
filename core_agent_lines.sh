#!/usr/bin/env bash
# Count Python lines in the core agent code (./kiso directory).
# Adapted from nanobot's core_agent_lines.sh for Kiso's structure.
set -euo pipefail

KISO_DIR="$(cd "$(dirname "$0")/kiso" && pwd)"

total=0

printf "%-30s %s\n" "Directory" "Lines"
printf "%-30s %s\n" "------------------------------" "-----"

# Count lines in top-level .py files
top_lines=0
for f in "$KISO_DIR"/*.py; do
    [ -f "$f" ] && top_lines=$((top_lines + $(wc -l < "$f")))
done
printf "%-30s %5d\n" "kiso/*.py" "$top_lines"
total=$((total + top_lines))

# Count lines in each subdirectory
for dir in "$KISO_DIR"/*/; do
    [ -d "$dir" ] || continue
    dirname=$(basename "$dir")
    # Skip non-code directories
    [[ "$dirname" == "__pycache__" ]] && continue
    [[ "$dirname" == "reference" ]] && continue

    dir_lines=0
    while IFS= read -r -d '' f; do
        dir_lines=$((dir_lines + $(wc -l < "$f")))
    done < <(find "$dir" -name '*.py' -print0 2>/dev/null)

    # Also count .md files in roles/
    if [ "$dirname" = "roles" ]; then
        while IFS= read -r -d '' f; do
            dir_lines=$((dir_lines + $(wc -l < "$f")))
        done < <(find "$dir" -name '*.md' -print0 2>/dev/null)
    fi

    if [ "$dir_lines" -gt 0 ]; then
        printf "%-30s %5d\n" "kiso/$dirname/" "$dir_lines"
        total=$((total + dir_lines))
    fi
done

printf "%-30s %s\n" "------------------------------" "-----"
printf "%-30s %5d\n" "Total" "$total"
