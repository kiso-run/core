# Model Selection Guide

kiso uses 11 LLM roles in its pipeline. Each role has different requirements
for intelligence, speed, and cost. This guide documents the default choices,
the data behind them, and alternative configurations.

## Benchmark Data

All models accessed via OpenRouter. Prices in USD per million tokens.

| Model | t/s | Input $/M | Output $/M | Context | MMLU | LiveCodeBench |
|---|---|---|---|---|---|---|
| google/gemini-2.5-flash-lite | ~150 | 0.10 | 0.40 | 1M | 70 | 59 |
| google/gemini-2.5-flash | ~70 | 0.30 | 2.50 | 1M | 81 | 80 |
| deepseek/deepseek-v3.2 | ~25 | 0.28 | 0.42 | 163K | 79 | 60 |
| perplexity/sonar | — | 1.00 | 1.00 | 128K | — | — |
| minimax/minimax-m2.7 | ~40 | 0.20 | 0.80 | 1M | 78 | 70 |

**MMLU** = Massive Multitask Language Understanding (general intelligence).
**LiveCodeBench** = code generation benchmark (coding ability).

## Default Configuration

```toml
briefer      = "google/gemini-2.5-flash"       # context selection (json_schema native)
classifier   = "google/gemini-2.5-flash"       # message classification (fast, simple)
planner      = "deepseek/deepseek-v3.2"        # plan generation (structured output)
reviewer     = "google/gemini-2.5-flash-lite"  # output review (high frequency, json_schema)
curator      = "google/gemini-2.5-flash"       # knowledge curation (needs reliable json_schema)
worker       = "deepseek/deepseek-v3.2"        # command translation (strict output format)
summarizer   = "google/gemini-2.5-flash-lite"  # conversation summary (async, cheap)
paraphraser  = "google/gemini-2.5-flash-lite"  # prompt injection defense (critical path)
messenger    = "deepseek/deepseek-v3.2"        # user-facing responses (natural language)
searcher     = "perplexity/sonar"              # web search (native search API)
consolidator = "google/gemini-2.5-flash-lite"  # periodic knowledge review (async, cheap)
```

## Per-Role Rationale

### Briefer — `gemini-2.5-flash`

**Requirement:** reliable structured output. Runs before every planner/messenger
call to select relevant context modules, tools, and facts. Needs native
`json_schema` support for consistent structured responses. Flash (not flash-lite)
because briefer decisions directly affect planner quality.

### Classifier — `gemini-2.5-flash`

**Requirement:** fast classification. Single-word response (plan/chat/chat_kb).
Uses `json_schema` for consistent format. Flash provides reliable classification
across languages.

### Planner — `deepseek-v3.2`

**Requirement:** strong reasoning + structured output. Understands user intent,
selects tools, produces valid multi-step JSON plans. Runs once per message.
DeepSeek-v3.2 produces well-structured plans with low hallucination and
handles multilingual input well. Cost is low ($0.28/$0.42 per M tokens).

### Worker — `deepseek-v3.2`

**Requirement:** precise command translation. Translates task descriptions to
exact shell commands. Needs to handle file paths, heredocs, and tool-specific
syntax correctly. DeepSeek-v3.2's code understanding makes it reliable for
this role.

### Reviewer — `gemini-2.5-flash-lite`

**Requirement:** fast judgment + structured output. Runs on every task (highest
frequency role). Evaluates output against expectations with json_schema.
Flash-lite is sufficient — the reviewer checks concrete criteria (exit code,
expected output), not open-ended reasoning.

### Curator — `gemini-2.5-flash`

**Requirement:** reliable structured output with semantic reasoning. Must follow
complex rules: promote/ask/discard verdicts, entity assignment, tag reuse,
poisoning resistance. Flash (not flash-lite) because curator decisions affect
long-term knowledge quality and flash-lite doesn't follow verdict-binding
rules reliably enough.

### Messenger — `deepseek-v3.2`

**Requirement:** natural language quality. User-facing responses need to sound
natural, handle multilingual output, and faithfully relay task results.
DeepSeek-v3.2 produces fluent responses across languages.

### Summarizer / Paraphraser / Consolidator — `gemini-2.5-flash-lite`

**Requirement:** fast, cheap. These run asynchronously with no latency pressure.
Summarization, paraphrasing, and knowledge consolidation are well-handled by
fast models.

### Searcher — `perplexity/sonar`

**Requirement:** native web search. Sonar is a search-augmented model with
built-in web retrieval. No alternative in the pipeline for search tasks.

## Cost Estimation

Typical request with 3 tasks:

| Call | Model | Input tok | Output tok | Cost |
|---|---|---|---|---|
| Briefer | gemini-flash | 800 | 200 | $0.00029 |
| Classifier | gemini-flash | 700 | 10 | $0.00021 |
| Planner | deepseek-v3.2 | 1000 | 500 | $0.00049 |
| Worker x3 | deepseek-v3.2 | 500 x3 | 100 x3 | $0.00055 |
| Reviewer x3 | gemini-flash-lite | 400 x3 | 100 x3 | $0.00024 |
| Messenger | deepseek-v3.2 | 800 | 300 | $0.00035 |
| **Total** | | | | **~$0.002** |

## Fallback Models

When the primary model stalls or times out, kiso switches to a fallback:

- **Planner fallback:** `minimax/minimax-m2.7` (configurable via `planner_fallback_model`)
- **All text roles:** fallback triggers on SSE stall or HTTP timeout
- **Retry budget:** configurable via `max_llm_retries` (default 3)
