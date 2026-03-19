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

code_total=0
test_total=0

echo
echo "── Code ──"

n=$(count_py "$ROOT/kiso")
row "kiso/*.py" "$n"; code_total=$((code_total + n))

n=$(count_py "$ROOT/kiso/worker" true)
row "kiso/worker/" "$n"; code_total=$((code_total + n))

n=$(count_ext "$ROOT/kiso/roles" "md")
row "kiso/roles/ (.md)" "$n"; code_total=$((code_total + n))

n=$(count_ext "$ROOT/kiso/reference" "md")
row "kiso/reference/ (.md)" "$n"; code_total=$((code_total + n))

if [[ -d "$ROOT/kiso/completions" ]]; then
    n=0
    while IFS= read -r -d '' f; do
        n=$((n + $(wc -l < "$f")))
    done < <(find "$ROOT/kiso/completions" -type f -not -name '__*' -print0 2>/dev/null)
    row "kiso/completions/" "$n"; code_total=$((code_total + n))
fi

n=$(count_py "$ROOT/cli" true)
row "cli/" "$n"; code_total=$((code_total + n))

sep
row "Code total" "$code_total"

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
if [[ "$test_total" -gt 0 ]]; then
    ratio=$(echo "scale=1; $code_total / $test_total" | bc 2>/dev/null || echo "?")
    printf "%-35s %4s:1\n" "Code:Test ratio" "$ratio"
else
    printf "%-35s %s\n" "Code:Test ratio" "N/A"
fi
echo
