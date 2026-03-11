#!/usr/bin/env bash
set -euo pipefail

# Signal handling convention:
# - Ctrl+C must NOT close the terminal. Use `exit 130`, never `kill -INT $$`.
# - Use EXIT trap for cleanup (temp files, backups).
# - Use INT trap only for a graceful message + exit 130.

# summary.sh — compact a verbose Kiso CLI session into deduplicated JSON.
#
# The script parses the terminal output produced by /verbose-on and:
#   1. Splits it into text blocks and LLM call boxes (╭─ … ╰─)
#   2. Deduplicates identical content via SHA1 (defs + event refs)
#   3. Redacts likely API keys / tokens
#   4. Summarises long blocks with head/tail previews
#
# The resulting JSON has three sections:
#   meta   — compression stats (blk, ud, ev, cb/ca, tb/ta)
#   defs   — unique content definitions (D00001, D00002, …)
#   events — ordered list referencing defs by id (t=type, l=lines, r=ref)

IN_TMP="$(mktemp -t flow_in.XXXXXX)"
OUT_TMP="$(mktemp -t flow_out.XXXXXX)"
cleanup() { rm -f "$IN_TMP" "$OUT_TMP"; }
trap cleanup EXIT
trap 'printf "\nInterrupted.\n" >&2; exit 130' INT

# -------- CLI flags --------
DO_CLEAR=0
OUT_FILE=""
PRETTY=""  # "" = auto (pretty on TTY), "1" = force pretty, "0" = force minified

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clear) DO_CLEAR=1; shift ;;
    --out) OUT_FILE="${2:-}"; shift 2 ;;
    --pretty) PRETTY=1; shift ;;
    --no-pretty) PRETTY=0; shift ;;
    --no-color) NO_COLOR=1; shift ;;
    --help|-h)
      cat <<'USAGE'
summary.sh — compact verbose Kiso CLI output into deduplicated JSON.

Usage:
  ./summary.sh [file] [options]

Modes:
  With [file]:    read input from file
  Without file:   paste input, finish with CTRL-D

Options:
  --clear          clear the screen before printing output
  --out FILE       write output to FILE
  --pretty         pretty-printed JSON (indented)
  --no-pretty      minified JSON (default)
  --no-color       disable colors even on TTY
  -h, --help       show this help

Examples:
  # Paste a verbose session and view compact summary
  ./summary.sh

  # Compact a saved file, save result
  ./summary.sh session.log --out flow.json

  # Pretty-print for reading
  ./summary.sh session.log --pretty | less
USAGE
      exit 0
      ;;
    *)
      if [[ -z "${INPUT_FILE:-}" && -f "$1" ]]; then
        INPUT_FILE="$1"
        shift
      else
        echo "Unknown argument: $1" >&2
        exit 2
      fi
      ;;
  esac
done

# -------- Colors (auto-disable if not TTY or --no-color) --------
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'
  C_DIM=$'\033[2m'
  C_BOLD=$'\033[1m'
  C_IN=$'\033[38;5;39m'
  C_OUT=$'\033[38;5;82m'
else
  C_RESET=""; C_DIM=""; C_BOLD=""; C_IN=""; C_OUT=""
fi

hr() {
  printf "%s\n" "──────────────────────────────────────────────────────────────────────────────" >&2
}

section() {
  local color="$1"; shift
  local title="$1"; shift
  hr
  printf "%s%s%s%s\n" "${color}" "${C_BOLD}" "$title" "${C_RESET}" >&2
  hr
}

# -------- Resolve pretty mode --------
if [[ -z "$PRETTY" ]]; then
  PRETTY=0  # compact by default; use --pretty for indented output
fi

# -------- Read input --------
if [[ -n "${INPUT_FILE:-}" ]]; then
  section "$C_IN" "INPUT (from file: ${INPUT_FILE})"
  cat "$INPUT_FILE" > "$IN_TMP"
  echo "${C_DIM}Loaded file into temp buffer.${C_RESET}" >&2
else
  section "$C_IN" "INPUT (paste mode)"
  echo "Paste the verbose Kiso output below." >&2
  echo "Finish input with CTRL-D (on an empty line)." >&2
  hr
  cat > "$IN_TMP"
  hr
  echo "${C_DIM}Input captured.${C_RESET}" >&2
fi

# -------- Summarize --------
python3 - "$IN_TMP" "$PRETTY" > "$OUT_TMP" <<'PY'
import sys, re, json, hashlib

path = sys.argv[1]
pretty = sys.argv[2] == "1"
raw = open(path, "r", encoding="utf-8", errors="replace").read()

SEP_CHARS = set("─━═-")
BOX_TOP = "╭"
BOX_BOTTOM = "╰"

RE_BOX_HEADER = re.compile(
    r"^╭─\s*(?P<role>\w+)\s*→\s*(?P<model>[^\s]+)(?:\s*\((?P<intok>[\d,]+)→(?P<outtok>[\d,]+)\))?"
)

# Redact patterns that look like API keys, not generic hex/hashes.
# Targets: sk-xxx, key-xxx, Bearer tokens, long base64-ish strings with mixed case.
RE_SECRET = re.compile(
    r"\b(?:"
    r"sk-[A-Za-z0-9]{20,}"           # OpenAI-style
    r"|key-[A-Za-z0-9]{20,}"         # generic key- prefix
    r"|xox[bpsar]-[A-Za-z0-9\-]{20,}" # Slack tokens
    r"|ghp_[A-Za-z0-9]{20,}"         # GitHub PAT
    r"|glpat-[A-Za-z0-9\-]{20,}"     # GitLab PAT
    r"|Bearer\s+[A-Za-z0-9\-_.]{20,}" # Bearer tokens
    r")\b"
)

def approx_tokens(s: str) -> int:
    return max(1, len(s) // 4)

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()

def redact(s: str) -> str:
    return RE_SECRET.sub(lambda m: f"<REDACTED:{m.group(0)[:6]}…>", s)

def is_separator_line(line: str) -> bool:
    t = line.strip()
    return len(t) >= 20 and all(c in SEP_CHARS for c in t)

def clean_box_line(line: str) -> str:
    if not line.startswith("│"):
        return line.rstrip("\n")
    s = line[1:]
    if s.endswith("│"):
        s = s[:-1]
    return s.rstrip()

def summarize_text(text: str, head_lines: int = 14, tail_lines: int = 8):
    ls = text.splitlines()
    if len(ls) <= head_lines + tail_lines + 2:
        return {"mode": "full", "text": text}
    return {
        "mode": "head_tail",
        "head": "\n".join(ls[:head_lines]),
        "tail": "\n".join(ls[-tail_lines:]),
        "omit": max(0, len(ls) - head_lines - tail_lines),
    }

def split_blocks(lines):
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        if lines[i].startswith(BOX_TOP):
            j = i + 1
            while j < n and not lines[j].startswith(BOX_BOTTOM):
                j += 1
            if j < n:
                j += 1
            blocks.append({"kind": "box", "start": i+1, "end": j, "lines": lines[i:j]})
            i = j
        else:
            j = i
            while j < n and not lines[j].startswith(BOX_TOP):
                j += 1
            blocks.append({"kind": "text", "start": i+1, "end": j, "lines": lines[i:j]})
            i = j
    return blocks

raw = redact(raw)
lines = raw.splitlines()
blocks = split_blocks([l + "\n" for l in lines])

defs = {}
def_ids = {}
events = []
next_def = 1

def make_def_id(k: int) -> str:
    return f"D{k:05d}"

for idx, b in enumerate(blocks, start=1):
    kind = b["kind"]
    start, end = b["start"], b["end"]

    if kind == "text":
        cleaned = []
        for ln in b["lines"]:
            s = ln.rstrip("\n")
            if is_separator_line(s):
                continue
            cleaned.append(s)
        text = "\n".join(cleaned).strip("\n")
        if not text.strip():
            continue

        h = sha1(text)
        ref = def_ids.get(h)
        if not ref:
            ref = make_def_id(next_def); next_def += 1
            def_ids[h] = ref
            defs[ref] = {
                "kind": "text",
                "tok": approx_tokens(text),
                "p": summarize_text(text),
            }
        events.append({"i": idx, "t": "text", "l": [start, end], "r": ref})
        continue

    # box
    box_lines = [ln.rstrip("\n") for ln in b["lines"]]
    header = box_lines[0] if box_lines else ""
    m = RE_BOX_HEADER.match(header)
    role = m.group("role") if m else None
    model = m.group("model") if m else None
    intok = m.group("intok") if (m and m.group("intok")) else None
    outtok = m.group("outtok") if (m and m.group("outtok")) else None

    inner = [clean_box_line(ln) for ln in box_lines[1:-1]]
    content = "\n".join(inner).strip()
    if not content:
        continue

    h = sha1(content)
    ref = def_ids.get(h)
    if not ref:
        ref = make_def_id(next_def); next_def += 1
        def_ids[h] = ref
        defs[ref] = {
            "kind": "box",
            "role": role,
            "model": model,
            "tok": approx_tokens(content),
            "p": summarize_text(content),
        }

    events.append({
        "i": idx, "t": "box", "l": [start, end],
        "role": role, "model": model, "in_tok": intok, "out_tok": outtok,
        "r": ref
    })

meta = {
    "stats": {
        "blk": len(blocks),
        "ev": len(events),
        "ud": len(defs),
        "cb": len(raw),
        "ca": 0,  # filled after serialization
        "tb": approx_tokens(raw),
        "ta": 0,
    }
}
out = {"meta": meta, "defs": defs, "events": events}

if pretty:
    text = json.dumps(out, ensure_ascii=False, indent=2)
else:
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))

# Update post-serialization stats
out["meta"]["stats"]["ca"] = len(text)
out["meta"]["stats"]["ta"] = approx_tokens(text)
if pretty:
    text = json.dumps(out, ensure_ascii=False, indent=2)
else:
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))

print(text)
PY

# Optional: save to file
if [[ -n "$OUT_FILE" ]]; then
  cp "$OUT_TMP" "$OUT_FILE"
fi

# Clear if requested
if [[ "$DO_CLEAR" -eq 1 ]]; then
  printf '\033[2J\033[H'
fi

section "$C_OUT" "OUTPUT (JSON)"
cat "$OUT_TMP"
echo >&2
hr

# Show stats hint
STATS=$(python3 -c "
import json, sys
d = json.load(open('$OUT_TMP'))
s = d['meta']['stats']
ratio = 100 * (1 - s['ca'] / max(1, s['cb']))
print(f\"{s['ud']} unique defs, {s['ev']} events, {ratio:.0f}% reduction\")
" 2>/dev/null || true)
if [[ -n "$STATS" ]]; then
  echo "${C_DIM}${STATS}${C_RESET}" >&2
fi
