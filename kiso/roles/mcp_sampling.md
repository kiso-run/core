# mcp_sampling

Documentation-only role file for `mcp_sampling`. The LLM call that
fulfils a `sampling/createMessage` request from an MCP server does
NOT load this file as its system prompt — the requesting server
supplies its own `systemPrompt` in the sampling request params and
`kiso.mcp.sampling.handle_sampling_request` forwards it verbatim to
`kiso.llm.call_llm` as the leading `role=system` message.

This file exists so the `kiso roles` registry convention (every
role in `_MODEL_METADATA` has a matching `kiso/roles/<name>.md`)
stays uniform, which in turn keeps the roles CLI, docs generation,
and trust-store surface consistent across all roles.

Configuration knobs live in `kiso/config.py`:

- `mcp_sampling_enabled` (default `true`) — when `false`, the
  client responds to every sampling request with the JSON-RPC
  `method not supported` error.
- `models.mcp_sampling` — model used to serve sampling requests.
  Defaults to `google/gemini-2.5-flash`.

Policy clamps live in `kiso/mcp/sampling.py`:

- `SAMPLING_MAX_TOKENS_CEILING` (4096) — hard upper bound applied
  to the server's requested `maxTokens`.
- Sampling calls count toward `max_llm_calls_per_message` via the
  shared `call_llm` budget context.
