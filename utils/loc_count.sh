#!/usr/bin/env bash
# Count lines of code in the kiso core project.
set -euo pipefail
trap 'exit 130' INT

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

count_py() {
    local dir="$1" recurse="${2:-false}" total=0
    if [[ "$recurse" == true ]]; then
        while IFS= read -r -d '' f; do
            total=$((total + $(wc -l < "$f")))
        done < <(find "$dir" -name '*.py' -not -path '*/__pycache__/*' -print0 2>/dev/null)
    else
        for f in "$dir"/*.py; do
            [[ -f "$f" ]] && total=$((total + $(wc -l < "$f")))
        done
    fi
    echo "$total"
}

count_ext() {
    local dir="$1" ext="$2" total=0
    while IFS= read -r -d '' f; do
        total=$((total + $(wc -l < "$f")))
    done < <(find "$dir" -name "*.$ext" -not -path '*/__pycache__/*' -print0 2>/dev/null)
    echo "$total"
}

row() { printf "%-35s %5d\n" "$1" "$2"; }
sep() { printf "%-35s %s\n" "───────────────────────────────────" "─────"; }

core_total=0
test_total=0

echo
echo "── Core (agent) ──"

n=$(count_py "$ROOT/kiso")
row "kiso/*.py" "$n"; core_total=$((core_total + n))

n=$(count_py "$ROOT/kiso/worker" true)
row "kiso/worker/" "$n"; core_total=$((core_total + n))

n=$(count_ext "$ROOT/kiso/roles" "md")
row "kiso/roles/ (.md)" "$n"; core_total=$((core_total + n))

n=$(count_ext "$ROOT/kiso/reference" "md")
row "kiso/reference/ (.md)" "$n"; core_total=$((core_total + n))

if [[ -d "$ROOT/kiso/completions" ]]; then
    n=0
    while IFS= read -r -d '' f; do
        n=$((n + $(wc -l < "$f")))
    done < <(find "$ROOT/kiso/completions" -type f -not -name '__*' -print0 2>/dev/null)
    row "kiso/completions/" "$n"; core_total=$((core_total + n))
fi

sep
row "Core total" "$core_total"

echo
echo "── CLI ──"
cli_total=$(count_py "$ROOT/cli" true)
row "cli/" "$cli_total"

echo
echo "── Tests ──"

n=$(count_py "$ROOT/tests")
row "tests/ (unit)" "$n"; test_total=$((test_total + n))

for sub in live functional docker; do
    if [[ -d "$ROOT/tests/$sub" ]]; then
        n=$(count_py "$ROOT/tests/$sub" true)
        row "tests/$sub/" "$n"; test_total=$((test_total + n))
    fi
done

if [[ -d "$ROOT/tests/bash" ]]; then
    n=$(count_ext "$ROOT/tests/bash" "bats")
    m=$(count_ext "$ROOT/tests/bash" "bash")
    row "tests/bash/ (.bats+.bash)" "$((n + m))"; test_total=$((test_total + n + m))
fi

sep
row "Tests total" "$test_total"

echo
echo "── Summary ──"
if [[ "$core_total" -gt 0 && "$test_total" -gt 0 ]]; then
    ratio=$(echo "scale=1; $test_total / $core_total" | bc 2>/dev/null || echo "?")
    printf "%-35s %4s\n" "Core:Test ratio" "1:$ratio"
else
    printf "%-35s %s\n" "Core:Test ratio" "N/A"
fi
echo
