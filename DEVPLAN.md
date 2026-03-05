# DEVPLAN: Verbose Output Redesign — Split Input/Output Panels

## Problem

The current verbose mode (`/verbose-on`) renders each LLM call as a **single panel** that contains both the input messages AND the response together, separated only by a `─── response ───` line inside the same `╭─...╮` border. This makes it hard to distinguish what was sent vs what was received, and the flow doesn't follow the natural action lifecycle.

## Desired Flow

Each LLM call should render as a clear sequential flow:

```
1. ACTION HEADER    →  role → model (compact label)
2. INPUT PANEL      →  bordered panel with ONLY the messages sent to the LLM
3. TIMER            →  waiting/elapsed indicator while LLM is processing
4. TIMER CONCLUSION →  completed indicator with duration
5. OUTPUT PANEL     →  bordered panel with ONLY the response from the LLM
```

Key rules:
- **Input panel** = ONLY the call (system + user messages). No response.
- **Output panel** = ONLY the response (+ thinking if present). No input messages.
- **No duplication** — nothing should appear in both panels.
- Clear visual distinction between the two panels (different border colors or styles).

## Current Code

- `cli/render.py:601-675` — `render_llm_calls_verbose()`: builds a single Rich Panel per LLM call containing both input messages and response
- `cli/render.py:140-148` — `_verbose_title()`: builds the panel title
- `cli/render.py:128-137` — `_build_message_parts()`: formats input messages
- `cli/__init__.py:537-547` — `_emit_verbose_calls()`: incremental verbose rendering
- `cli/__init__.py:771-788` — plan-level verbose rendering (classifier/planner calls)
- `cli/__init__.py:684-686` — task-level verbose rendering
- `cli/__init__.py:867-875` — inflight LLM call indicator (already shows waiting state)

---

## Milestone 1: Split `render_llm_calls_verbose` into Input + Output panels ✅

**Goal:** Replace the single combined panel with two separate panels per LLM call.

### Tasks

1. **Refactor `render_llm_calls_verbose`** in `cli/render.py`:
   - Remove the current single-panel approach
   - For each LLM call, render TWO panels:
     - **Input panel**: title = `role → model (input_tokens) timestamp`, border = `dim cyan`, body = only the messages (system, user, etc.) — use existing `_build_message_parts()`
     - **Output panel**: title = `role → model (output_tokens, duration) timestamp`, border = `dim green`, body = thinking block (if any) + response text/JSON
   - Between the two panels, render a compact elapsed line: `  ✓ role  input→output  duration  model`

2. **Update `_verbose_title`** to accept a `direction` parameter (`"in"` / `"out"`) so the title can reflect whether it's the input or output panel. Input shows only input token count; output shows output token count + duration.

3. **Update tests** in `tests/` that assert on verbose panel output to match the new two-panel format.

### Acceptance Criteria
- [x] Each LLM call in verbose mode renders as two visually distinct panels (input, then output)
- [x] Input panel contains ONLY messages sent; output panel contains ONLY the response
- [x] No content is duplicated across the two panels
- [x] Non-verbose mode (`render_llm_calls`) is unchanged
- [x] All existing tests pass (2254 pass, 66 skipped)

### Deviations
- Did not extract a shared summary-line helper between `render_llm_calls` and the verbose summary; the differences (Rich escaping, elapsed format) make a shared helper more complex than the duplication.

---

## Milestone 2: Inflight indicator integration

**Goal:** The inflight indicator (already exists at `cli/__init__.py:867-875`) should appear between the input and output panels during live polling, completing the timer-in-the-middle flow.

### Tasks

1. **Adjust `_emit_verbose_calls`** to support incremental rendering:
   - When a call has messages but no response yet (inflight), render only the input panel
   - When the response arrives, render the elapsed line + output panel
   - Track per-call state: `verbose_shown` should distinguish "input shown" vs "fully shown"

2. **Update `_PollRenderState.verbose_shown`** from `dict[tid, int]` to track per-call granularity (e.g. `dict[tid, list[str]]` where each entry is `"input"` or `"full"`).

3. **Render the inflight waiting line** (`⏳ role → model (waiting...)`) between input and output panels during live polling, instead of as a standalone line.

### Acceptance Criteria
- During live execution, the user sees: input panel → waiting indicator → (response arrives) → elapsed line + output panel
- No panels are re-rendered or duplicated during incremental polling
- Works correctly for plan-level calls (classifier, planner) and task-level calls (translator, reviewer, searcher)
