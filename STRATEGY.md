# kiso — Strategy

> Companion to [VISION.md](VISION.md). VISION is the contract:
> what kiso is and what it must remain. STRATEGY is the bet: who
> we serve, how we compete, where the first wins come from.
> Strategy is revisable as evidence arrives; the four invariants
> in VISION are not.

## Positioning in one sentence

kiso is a **general-purpose agent runtime substrate** built to
replace ForgeCode and Goose for teams that need agents in
**production-grade pipelines** — with audit, determinism, and
recoverable failure modes as defaults, not bolt-ons.

kiso is **generalist by design**. Domain-specific configuration
(EU AI Act compliance, PII filtering, sector compliance, custom
governance) lives in the **consumer** of kiso (cerase is the
first real example), not in kiso itself. kiso is the backbone;
verticals are the build.

## Who we compete with directly

Three runtime substrates that today own the production-agent
conversation:

- **ForgeCode** (Apache 2.0, Rust, antinomyhq) — single-agent
  ReAct loop, deterministic safety rails, multi-provider native,
  lifecycle hooks. Strengths: performance, license, benchmark
  numbers. Weakness: no structural plan validation, default
  allow-all permissions, audit is log-shaped not query-shaped.
- **Goose** (Apache 2.0, Rust, LF / AAIF / Block) — single-agent
  ReAct + MCP-first + recipes + on-demand sub-agents. Strengths:
  MCP-native, ACP-native, neutral governance, K8s-friendly.
  Weakness: permission judge is LLM-based not state-machine,
  audit relies on session-store introspection not always-on
  structured trace.
- **LangGraph / LangChain Agents** (MIT, Python) — framework
  primitives. Strengths: flexibility, ecosystem. Weakness:
  framework not runtime; you build the agent, kiso *is* the
  agent.

Indirect competition (not the same shape but absorbs the same
buying intent): Claude Code SDK (proprietary, vendor-locked),
CrewAI / AutoGen (research-stage multi-agent).

## How we beat them

Three structural differentiators, all already in kiso's DNA:

1. **State-machine consent over LLM judges.** Install / capability
   acquisition / privileged-action approval is a deterministic
   state transition (`needs_install` → user approves → exec
   literal), not a model classification call. Audit-friendly,
   replay-able, regulator-defensible. Forge has allow-all; Goose
   has an LLM judge. Neither holds up under audit.
2. **Always-on structured audit trail.** Every LLM call, tool
   dispatch, rule-decision and state transition is recorded in a
   queryable format (not free-text logs). A downstream operator
   can answer "why did the agent do X at turn N" without reading
   our source. Goose has session-store introspection; Forge has
   JSONL logs; neither is queryable out of the box at the
   "reconstruct any past decision" level kiso targets.
3. **Determinism budget per turn.** Rule-based pre/post-filtering
   absorbs decisions that don't need an LLM (ranking, routing,
   schema validation, structural checks). The LLM call count on
   the common turn is bounded (architect + editor, 2 calls); the
   prompt prefix is cache-first; the same input yields the same
   output ≥95% of the time. Forge and Goose are not engineered
   to this contract — they trust the model to be consistent,
   which it isn't.

The fourth invariant (efficiency) is competitive parity, not
moat. Forge and Goose are also efficient; kiso must be too.

## Who we serve (target adopter profiles)

In order of how easy they are to win, not of market size:

1. **Verticals building agent products under compliance pressure**
   — fintech, legaltech, healthtech, regtech, GDPR-bound EU SaaS,
   sector-regulated SMB tools. Cerase is the canonical example.
   The pitch: "we hand you the runtime; you handle the vertical
   data plane and the compliance posture on top".
2. **Agencies and integrators delivering custom agents to
   enterprise clients.** They need a runtime they can demo, harden,
   and hand off without becoming the only person who understands
   the agent. kiso's audit trail is the demo.
3. **Enterprise platform teams** putting agents in production
   pipelines (back-office automation, ops co-pilot, data
   stewardship). Internal audit is a hard requirement; they reject
   Claude Code (vendor lock) and find LangGraph too low-level.
4. **OSS adopters evaluating LangGraph** who want runtime, not
   framework. The pitch: "you don't need to wire a graph; here's
   the agent loop with the safety rails already in".

We do **not** target consumer chat users, IDE-native coding tool
buyers (Cursor land), or hyperscale benchmark gaming.

## Where we don't play

- **Coding-only IDE integration.** Cursor, Cline, Continue own
  this. kiso supports coding as one workload; it does not compete
  for "best autocomplete in VS Code".
- **Multi-agent research demos.** Production-grade is single-agent
  with on-demand subagents. CrewAI/AutoGen-shaped pitches are
  outside the target.
- **Hyperscale leaderboard chasing.** Terminal-Bench / SWE-bench
  numbers are evidence kiso is competitive, not the product
  goal. We publish kiso's numbers when they're honest; we do not
  tune for them.

## Adoption sequence

**Phase 0 — current state.** kiso-run/plugins ship in cerase
production. That is the first proof point. The work is to make it
provable to outsiders: write the case study, publish the
architecture diagram of how cerase consumes kiso, get one
external observer to verify it.

**Phase 1.** Land kiso-run/core's first real consumer, ideally
cerase itself swapping from Goose to kiso-run/core for one
production agent template (Document Q&A is the natural pilot —
narrow scope, audit-heavy, clear contract). Publish docs:
getting-started, the four invariants in code, the audit-trail
query language. Knowledge-work benchmarks (document Q&A, summary
faithfulness, structured extraction) replace the M1669 Terminal-
Bench harness as the public number.

**Phase 2.** Two to three additional external consumers beyond
cerase. They surface naturally if Phase 1 is real: agencies and
regulated-vertical teams that find kiso through cerase visibility
and technical content. They are *not* cold sales; they are
inbound from the proof of Phase 1.

## First success criteria (measurable)

In order of binding force:

1. **kiso-run/core replaces Goose for at least one cerase
   production agent template.** Internal customer win. Without
   this, the substrate story is theoretical.
2. **Public knowledge-work benchmark suite** (document Q&A,
   summary, extraction) shipped with kiso numbers, replacing
   M1669's coding-shaped baseline. Without this, kiso has no
   honest external evidence of competence.
3. **Audit query interface** — a CLI / API that answers "why did
   the agent do X at turn N of session Y" from logs alone, with
   no source reading. The product manifestation of invariant III.
4. **At least one external (non-guidance) project declares
   kiso-run/core as runtime.** Validation that the strategy
   generalizes beyond cerase.

## Risks (honestly stated)

- **Cerase priorities pull faster than kiso-run/core matures.**
  Cerase ships customers; kiso-run/core ships releases. If cerase
  customers force feature work in cerase-internal code rather
  than upstream in kiso, kiso-run/core drifts. Mitigation:
  cerase's architecture explicitly treats kiso-run/plugins as
  upstream (§9.3); the same discipline must apply to core when
  it lands.
- **The B2B knowledge work agent market is still forming.**
  There may be no Phase 2 adopters because the market itself
  isn't ready. Mitigation: Phase 1 is internal (cerase swap),
  defensible regardless of external adoption.
- **Goose continues to improve and erodes the differentiator
  window.** AAIF / Block iterate fast. Mitigation: the four
  invariants are structural choices kiso made early; Goose
  cannot adopt them without an architectural shift it would not
  choose. The window stays open by construction, not by
  shipping speed.

## What this implies for v0.13

The current `devplan/v0.13-wip.md` (preserve-then-optimize) is
shaped around closing the latency gap vs Forge on coding-shaped
benchmarks. Under this strategy, v0.13 must instead be shaped
around the **four success criteria above**:

- The audit query interface (criterion 3) is the new headline.
- Knowledge-work benchmark suite (criterion 2) replaces M1669's
  Terminal-Bench / SWE-bench focus.
- The architecture refactor (architect + editor, 2 LLM calls,
  validate_plan preserved, structural audit trail always-on) is
  in service of criteria 1 and 3, not in service of beating
  Forge's numbers.

v0.13-wip.md will be rewritten from this strategy, not from the
prior "preserve multi-role and add caching" draft.
