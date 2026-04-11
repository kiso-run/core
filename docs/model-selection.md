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

Current defaults are defined in `MODEL_DEFAULTS` in `kiso/config.py` and
documented in the `[models]` section of [config.md](config.md). This guide
explains the selection criteria — see config.md for the actual values.

## Per-Role Rationale

### Briefer

**Requirement:** reliable structured output. Runs before every planner/messenger
call to select relevant context modules, wrappers, and facts. Needs native
`json_schema` support for consistent structured responses. Flash (not flash-lite)
because briefer decisions directly affect planner quality.

### Classifier

**Requirement:** fast classification. Single-word response (plan/chat/chat_kb).
Uses `json_schema` for consistent format. Flash provides reliable classification
across languages.

### Planner

**Requirement:** strong reasoning + structured output. Understands user intent,
selects wrappers, produces valid multi-step JSON plans. Runs once per message.
DeepSeek-v3.2 produces well-structured plans with low hallucination and
handles multilingual input well. Cost is low ($0.28/$0.42 per M tokens).

### Worker

**Requirement:** precise command translation. Translates task descriptions to
exact shell commands. Needs to handle file paths, heredocs, and wrapper-specific
syntax correctly. DeepSeek-v3.2's code understanding makes it reliable for
this role.

### Reviewer

**Requirement:** fast judgment + structured output. Runs on every task (highest
frequency role). Evaluates output against expectations with json_schema.
Flash-lite is sufficient — the reviewer checks concrete criteria (exit code,
expected output), not open-ended reasoning.

### Curator

**Requirement:** reliable structured output with semantic reasoning. Must follow
complex rules: promote/ask/discard verdicts, entity assignment, tag reuse,
poisoning resistance. Flash (not flash-lite) because curator decisions affect
long-term knowledge quality and flash-lite doesn't follow verdict-binding
rules reliably enough.

### Messenger

**Requirement:** natural language quality. User-facing responses need to sound
natural, handle multilingual output, and faithfully relay task results.
DeepSeek-v3.2 produces fluent responses across languages.

### Summarizer / Paraphraser / Consolidator

**Requirement:** fast, cheap. These run asynchronously with no latency pressure.
Summarization, paraphrasing, and knowledge consolidation are well-handled by
fast models.

### Searcher

**Requirement:** native web search. Sonar is a search-augmented model with
built-in web retrieval. No alternative in the pipeline for search tasks.

## Cost Estimation

Typical request with 3 tasks:

| Call | Input tok | Output tok |
|---|---|---|
| Briefer | 800 | 200 |
| Classifier | 700 | 10 |
| Planner | 1000 | 500 |
| Worker x3 | 500 x3 | 100 x3 |
| Reviewer x3 | 400 x3 | 100 x3 |
| Messenger | 800 | 300 |

Actual cost depends on the configured models. See [config.md](config.md)
for current defaults and their per-token pricing.

## Fallback Models

When the primary model stalls or times out, kiso switches to a fallback
model. The fallback and retry settings are configurable — see
[config.md](config.md) for `planner_fallback_model` and `max_llm_retries`.
