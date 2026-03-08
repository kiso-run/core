# Model Selection Guide

kiso uses 10 LLM roles in its pipeline. Each role has different requirements
for intelligence, speed, and cost. This guide documents the default choices,
the data behind them, and alternative configurations.

## Benchmark Data

All models accessed via OpenRouter. Prices in USD per million tokens.

| Model | t/s | Input $/M | Output $/M | Context | MMLU | LiveCodeBench |
|---|---|---|---|---|---|---|
| google/gemini-2.5-flash-lite | ~150 | 0.10 | 0.40 | 1M | 70 | 59 |
| z-ai/glm-4.7 | ~130 | 0.38 | 1.98 | 200K | 83 | 85 |
| moonshotai/kimi-k2.5 | ~100 | 0.45 | 2.20 | 262K | 85 | 84 |
| qwen/qwen3.5-flash-02-23 | ~90 | ~0.20 | ~0.60 | 1M | 82 | 82 |
| stepfun/step-3.5-flash | ~85 | 0.10 | 0.30 | 256K | 80 | 86 |
| google/gemini-2.5-flash | ~70 | 0.30 | 2.50 | 1M | 81 | 80 |
| google/gemini-3-flash-preview | ~65 | 0.50 | 3.00 | 1M | 86 | 91 |
| llama-3.1-70b-instruct | ~45 | ~0.90 | ~2.40 | 128K | 79 | 73 |
| qwen/qwen-3-235b-thinking | ~35 | ~0.80 | ~3.00 | 262K | 88 | 88 |
| deepseek/deepseek-v3.2 | ~25 | 0.28 | 0.42 | 163K | 79 | 60 |
| deepseek/deepseek-r1 | ~20 | 0.70 | 2.50 | 64K | 90 | 92 |
| llama-3.1-8b-instruct | ~200 | ~0.05 | ~0.10 | 128K | 66 | 35 |

**MMLU** = Massive Multitask Language Understanding (general intelligence).
**LiveCodeBench** = code generation benchmark (coding ability).

## Default Configuration

```toml
briefer     = "google/gemini-2.5-flash-lite"
classifier  = "google/gemini-2.5-flash-lite"
planner     = "z-ai/glm-4.7"
reviewer    = "stepfun/step-3.5-flash"
curator     = "google/gemini-2.5-flash-lite"
worker      = "stepfun/step-3.5-flash"
summarizer  = "google/gemini-2.5-flash-lite"
paraphraser = "google/gemini-2.5-flash-lite"
messenger   = "qwen/qwen3.5-flash-02-23"
searcher    = "perplexity/sonar"
```

## Per-Role Rationale

### Briefer — `gemini-2.5-flash-lite`

**Requirement:** speed + low cost. Runs before every planner/messenger/worker call.
Does context selection (classification), not generation. The cheapest, fastest model
is ideal — 150 t/s, $0.10 input. MMLU 70 is sufficient for "which modules and
skills are relevant to this request?".

### Classifier — `gemini-2.5-flash-lite`

**Requirement:** speed. Binary classification (plan vs chat). Even simpler than
the briefer — any model works, so pick the fastest and cheapest.

### Planner — `glm-4.7`

**Requirement:** strong reasoning + structured output. This is the hardest task —
understand user intent, select skills, produce valid multi-step JSON plans. Runs
once per message, so cost per call matters less than quality.

GLM-4.7 has MMLU 83 (strongest non-reasoning model in the cheap tier), LCB 85,
and is fast at 130 t/s. The $0.38/$1.98 cost is higher than flash models but
the planner runs once per message, so the absolute cost is small (~$0.0004/call).

**Why not step-3.5-flash?** LCB 86 vs 85 (negligible), but MMLU 80 vs 83. Planning
needs understanding more than raw coding, so MMLU matters more here.

**Why not deepseek-v3.2?** Dominated by step-3.5-flash: MMLU 80>79, LCB 86>60,
speed 85>25 t/s, input cost $0.10<$0.28. No advantage in any dimension.

### Worker — `step-3.5-flash`

**Requirement:** coding ability. Translates task descriptions to shell commands.
Runs 3+ times per plan, so speed and cost matter. Step-3.5-flash has the highest
LCB (86) among the affordable models at $0.10/$0.30.

### Reviewer — `step-3.5-flash`

**Requirement:** judgment + structured output. Evaluates if task output matches
expectations, decides ok/replan. Runs once per task (3+ per plan). LCB 86 helps
when reviewing code output; MMLU 80 provides adequate judgment.

### Curator — `gemini-2.5-flash-lite`

**Requirement:** fast classification. Evaluates learnings as promote/ask/discard —
a straightforward classification task. Runs asynchronously, so speed and cost
matter more than intelligence.

### Summarizer — `gemini-2.5-flash-lite`

**Requirement:** fast, cheap. Compresses conversation history periodically. Runs
asynchronously with no latency pressure. Summarization is well-handled by fast
models — no need for high MMLU.

### Paraphraser — `gemini-2.5-flash-lite`

**Requirement:** speed. On the critical path for untrusted messages (prompt
injection defense). Must be fast. The rewriting task is simple — restructure
input without interpreting instructions.

### Messenger — `qwen-3.5-flash`

**Requirement:** natural language quality. User-facing responses need to sound
natural and handle multilingual output well. Qwen-3.5-flash has MMLU 82 (better
language understanding) at 90 t/s and reasonable cost. The slight cost increase
over gemini-flash-lite is worth it for UX quality.

### Searcher — `perplexity/sonar`

**Requirement:** native web search. Sonar is a search-augmented model — no
alternative in the pipeline for web search tasks.

## Cost Estimation

Typical request with 3 tasks:

| Call | Model | Input tok | Output tok | Cost |
|---|---|---|---|---|
| Briefer | gemini-flash-lite | 800 | 200 | $0.00016 |
| Planner | glm-4.7 | 1500 | 500 | $0.00156 |
| Worker x3 | step-3.5-flash | 500 x3 | 100 x3 | $0.00024 |
| Reviewer x3 | step-3.5-flash | 400 x3 | 100 x3 | $0.00021 |
| Messenger | qwen-3.5-flash | 1000 | 300 | $0.00038 |
| **Total** | | | | **~$0.0026** |

Comparison: all-deepseek-v3.2 baseline costs ~$0.003 per request with
significantly worse quality (MMLU 79, LCB 60) and 6x slower planner.

## Alternative Configurations

### Budget — minimize cost

Use gemini-flash-lite everywhere except planner (step for cheapest reasonable
quality) and searcher.

```toml
briefer     = "google/gemini-2.5-flash-lite"
classifier  = "google/gemini-2.5-flash-lite"
planner     = "stepfun/step-3.5-flash"            # LCB 86, $0.10/$0.30
reviewer    = "google/gemini-2.5-flash-lite"
curator     = "google/gemini-2.5-flash-lite"
worker      = "google/gemini-2.5-flash-lite"
summarizer  = "google/gemini-2.5-flash-lite"
paraphraser = "google/gemini-2.5-flash-lite"
messenger   = "google/gemini-2.5-flash-lite"
searcher    = "perplexity/sonar"
```

~$0.0008/request. Trade-off: worker and reviewer lose coding ability (LCB 59 vs 86).

### Quality — maximize intelligence

Strongest models for critical roles. Higher cost, better results for complex tasks.

```toml
briefer     = "stepfun/step-3.5-flash"
classifier  = "google/gemini-2.5-flash-lite"
planner     = "moonshotai/kimi-k2.5"                 # MMLU 85, LCB 84
reviewer    = "z-ai/glm-4.7"                    # MMLU 83 for better judgment
curator     = "stepfun/step-3.5-flash"
worker      = "stepfun/step-3.5-flash"
summarizer  = "google/gemini-2.5-flash-lite"
paraphraser = "google/gemini-2.5-flash-lite"
messenger   = "moonshotai/kimi-k2.5"                 # MMLU 85 for best language
searcher    = "perplexity/sonar"
```

~$0.005/request. Worth it for tasks requiring complex planning and nuanced review.

### Max quality — reasoning models

For the most demanding tasks. Use reasoning models for planner. Very slow but
highest accuracy.

```toml
briefer     = "stepfun/step-3.5-flash"
classifier  = "google/gemini-2.5-flash-lite"
planner     = "deepseek/deepseek-r1"            # MMLU 90, LCB 92 (slow: 20 t/s)
reviewer    = "z-ai/glm-4.7"
curator     = "stepfun/step-3.5-flash"
worker      = "stepfun/step-3.5-flash"
summarizer  = "google/gemini-2.5-flash-lite"
paraphraser = "google/gemini-2.5-flash-lite"
messenger   = "moonshotai/kimi-k2.5"
searcher    = "perplexity/sonar"
```

~$0.008/request. Planner will be slow (20 t/s) but produces the best plans.

### All-deepseek — legacy / compatibility

The previous default. Works but is dominated by faster, cheaper models.

```toml
briefer     = "deepseek/deepseek-v3.2"
classifier  = "deepseek/deepseek-v3.2"
planner     = "deepseek/deepseek-v3.2"
reviewer    = "deepseek/deepseek-v3.2"
curator     = "deepseek/deepseek-v3.2"
worker      = "deepseek/deepseek-v3.2"
summarizer  = "deepseek/deepseek-v3.2"
paraphraser = "deepseek/deepseek-v3.2"
messenger   = "deepseek/deepseek-v3.2"
searcher    = "perplexity/sonar"
```

~$0.003/request. Slow (25 t/s), MMLU 79, LCB 60. Not recommended — step-3.5-flash
is better in every dimension at lower cost.

## How to Override

Edit `~/.kiso/config.toml`:

```toml
[models]
planner = "deepseek/deepseek-r1"   # override just the planner
```

All models route through your configured provider (typically OpenRouter).
The model string is sent as-is to the API — use whatever identifiers your
provider supports.

To use a different provider for a specific model, use `provider:model`:

```toml
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[providers.ollama]
base_url = "http://localhost:11434/v1"

[models]
worker = "ollama:codellama"   # route worker through local Ollama
planner = "z-ai/glm-4.7"      # route through first provider (OpenRouter)
```
