# Shell helper: idempotent `.env` loader used by utils/run_tests.sh.
#
# Source this file, then call `_load_env_file PATH`. The function:
#   - returns 0 silently if PATH does not exist
#   - skips comments (lines starting with `#`) and blank lines
#   - preserves `=` signs inside values (splits on first `=` only)
#   - never overwrites variables already present in the environment,
#     so parent-shell exports always win and multiple calls chain
#     safely with documented precedence (caller-defined ordering).

_load_env_file() {
    local env_file="$1"
    [[ -f "$env_file" ]] || return 0
    local key value
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        if [[ -z "${!key:-}" ]]; then
            export "$key"="$value"
        fi
    done < "$env_file"
}
