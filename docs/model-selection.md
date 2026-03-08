# Model Selection Guide

kiso uses multiple LLM roles in its pipeline. Each role has different requirements
for intelligence, speed, and cost. This guide explains the default model choices
and how to customize them.

## Model Comparison

All models are accessed via OpenRouter. Prices and performance may change.

| Model | AAII | Coding | Speed (t/s) | Input $/M | Output $/M | Context |
|---|---|---|---|---|---|---|
| qwen/qwen-3.5-flash | 45 | 42 | 320 | 0.05 | 0.20 | 128K |
| step/step-3.5-flash | 48 | 59 | 180 | 0.07 | 0.28 | 128K |
| glm/glm-4.7 | 52 | 55 | 90 | 0.10 | 0.40 | 128K |
| kimi/kimi-k2.5 | 50 | 45 | 110 | 0.10 | 0.40 | 128K |
| deepseek/deepseek-v3.2 | 32 | 59 | 25 | 0.30 | 0.88 | 128K |
| perplexity/sonar | — | — | — | 1.00 | 1.00 | — |

*AAII = Artificial Analysis Intelligence Index. Coding = HumanEval-style benchmarks.*

## Per-Role Analysis

### Briefer (`qwen/qwen-3.5-flash`)
**Needs:** Speed, low cost. Runs before every planner/messenger/worker call.
**Why:** Briefer does classification + selection, not generation. Fast flash model
is ideal — latency and cost matter more than raw intelligence.

### Classifier (`step/step-3.5-flash`)
**Needs:** Speed, structured output. Classifies messages as plan vs chat.
**Why:** Binary classification task. Step's structured output is reliable at flash speed.

### Planner (`glm/glm-4.7`)
**Needs:** Strong reasoning, structured JSON output.
**Why:** Planning is the hardest task — must understand user intent, available skills,
and produce valid multi-step plans. GLM-4.7 has the best reasoning-to-cost ratio.

### Reviewer (`step/step-3.5-flash`)
**Needs:** Fast, structured output (ok/replan verdict).
**Why:** Reviews are mostly pattern-matching (did the output match expectations?).
Flash speed keeps the pipeline fast.

### Curator (`step/step-3.5-flash`)
**Needs:** Fast, structured output (promote/ask/discard).
**Why:** Evaluates learnings — straightforward classification with simple rules.

### Worker (`step/step-3.5-flash`)
**Needs:** Fast, code-aware. Translates task descriptions to shell commands.
**Why:** Good coding score (59) at flash speed. Most translations are simple.

### Summarizer (`qwen/qwen-3.5-flash`)
**Needs:** Fast, low cost. Runs periodically on conversation history.
**Why:** Summarization is well-handled by fast models. Runs asynchronously,
so latency is less critical than cost.

### Paraphraser (`step/step-3.5-flash`)
**Needs:** Fast. Rewrites untrusted external messages to neutralize injection.
**Why:** Must be fast (on the critical path) and reliable at structured rewriting.

### Messenger (`kimi/kimi-k2.5`)
**Needs:** Natural language quality, multilingual.
**Why:** User-facing responses need to sound natural. Kimi excels at conversational
output and handles Italian/English well. Slightly slower but worth it for UX.

### Searcher (`perplexity/sonar`)
**Needs:** Native web search capability.
**Why:** Sonar is a search-augmented model — no alternative in the pipeline.

## Cost Estimation

**Per-request breakdown** (typical plan with 3 tasks):

| Call | Model | Input tokens | Output tokens | Cost |
|---|---|---|---|---|
| Briefer (planner) | qwen-3.5-flash | 800 | 200 | $0.00008 |
| Planner | glm-4.7 | 1500 | 500 | $0.00035 |
| Worker x3 | step-3.5-flash | 500 x3 | 100 x3 | $0.00019 |
| Reviewer x3 | step-3.5-flash | 400 x3 | 100 x3 | $0.00015 |
| Messenger | kimi-k2.5 | 1000 | 300 | $0.00022 |
| **Total** | | | | **~$0.001** |

**Comparison with all-deepseek baseline:** ~$0.003/request → 3x cheaper.

## Alternative Configurations

### Budget mode
All roles use the cheapest flash model. Good for development and testing.

```toml
[models]
briefer     = "qwen/qwen-3.5-flash"
classifier  = "qwen/qwen-3.5-flash"
planner     = "step/step-3.5-flash"
reviewer    = "qwen/qwen-3.5-flash"
curator     = "qwen/qwen-3.5-flash"
worker      = "qwen/qwen-3.5-flash"
summarizer  = "qwen/qwen-3.5-flash"
paraphraser = "qwen/qwen-3.5-flash"
messenger   = "qwen/qwen-3.5-flash"
searcher    = "perplexity/sonar"
```

### Quality mode
Strongest models for critical roles. Higher cost, better results.

```toml
[models]
briefer     = "step/step-3.5-flash"
classifier  = "step/step-3.5-flash"
planner     = "glm/glm-4.7"
reviewer    = "glm/glm-4.7"
curator     = "step/step-3.5-flash"
worker      = "step/step-3.5-flash"
summarizer  = "qwen/qwen-3.5-flash"
paraphraser = "step/step-3.5-flash"
messenger   = "kimi/kimi-k2.5"
searcher    = "perplexity/sonar"
```

### Speed mode
Fastest models everywhere. Best for real-time interactive use.

```toml
[models]
briefer     = "qwen/qwen-3.5-flash"
classifier  = "qwen/qwen-3.5-flash"
planner     = "step/step-3.5-flash"
reviewer    = "qwen/qwen-3.5-flash"
curator     = "qwen/qwen-3.5-flash"
worker      = "qwen/qwen-3.5-flash"
summarizer  = "qwen/qwen-3.5-flash"
paraphraser = "qwen/qwen-3.5-flash"
messenger   = "qwen/qwen-3.5-flash"
searcher    = "perplexity/sonar"
```

## How to Override

Edit `~/.kiso/config.toml`:

```toml
[models]
planner = "deepseek/deepseek-v3.2"   # override just the planner
```

All models route through your configured provider (typically OpenRouter).
The model string is sent as-is to the API — use whatever model identifiers
your provider supports.

To use a different provider for a specific model, use the `provider:model` syntax:

```toml
[providers.openrouter]
base_url = "https://openrouter.ai/api/v1"

[providers.ollama]
base_url = "http://localhost:11434/v1"

[models]
worker = "ollama:codellama"   # route worker through local Ollama
planner = "glm/glm-4.7"      # route planner through OpenRouter (first provider)
```
