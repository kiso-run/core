"""Single source of truth for role metadata.

The registry is a small in-code table mapping each role to its
human-readable description, the model key it consumes (looked up in
``kiso.config._MODEL_METADATA`` at access time, never duplicated), the
prompt filename it loads from ``~/.kiso/roles/``, and the Python entry
point that invokes it.

It exists to back the ``kiso roles`` CLI surface and any
future docs generation. Adding a new role is a two-line change:
add the entry to ``_MODEL_METADATA`` in ``kiso/config.py`` and add
the entry below.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiso.config import MODEL_DEFAULTS


@dataclass(frozen=True)
class RoleMeta:
    """Metadata for a single role."""

    name: str
    """The user-facing role name (matches the .md filename stem)."""

    description: str
    """One-line human-readable description shown by ``kiso roles list``."""

    model_key: str
    """Key into ``MODEL_DEFAULTS`` (``kiso.config._MODEL_METADATA``)."""

    prompt_filename: str
    """The .md filename loaded by ``_load_system_prompt`` for this role."""

    python_entry: str
    """Dotted path to the Python function that invokes this role."""

    @property
    def default_model(self) -> str:
        """Resolve the default model from ``MODEL_DEFAULTS`` at access time."""
        return MODEL_DEFAULTS[self.model_key]


_ROLES: tuple[RoleMeta, ...] = (
    RoleMeta(
        name="briefer",
        description=(
            "Selects relevant context (modules, wrappers, facts) for "
            "downstream LLM roles."
        ),
        model_key="briefer",
        prompt_filename="briefer.md",
        python_entry="kiso.brain.common.run_briefer",
    ),
    RoleMeta(
        name="classifier",
        description=(
            "Routes user messages into plan, investigate, chat_kb, or chat."
        ),
        model_key="classifier",
        prompt_filename="classifier.md",
        python_entry="kiso.brain.common.run_classifier",
    ),
    RoleMeta(
        name="consolidator",
        description=(
            "Periodic knowledge quality review: dedupes and "
            "reconciles facts."
        ),
        model_key="consolidator",
        prompt_filename="consolidator.md",
        python_entry="kiso.brain.consolidator.run_consolidator",
    ),
    RoleMeta(
        name="curator",
        description=(
            "Evaluates task learnings and promotes them to durable facts."
        ),
        model_key="curator",
        prompt_filename="curator.md",
        python_entry="kiso.brain.curator.run_curator",
    ),
    RoleMeta(
        name="inflight-classifier",
        description=(
            "Classifies messages that arrive while a task is already "
            "running (stop / update / independent / conflict)."
        ),
        model_key="classifier",
        prompt_filename="inflight-classifier.md",
        python_entry="kiso.brain.common.run_inflight_classifier",
    ),
    RoleMeta(
        name="messenger",
        description=(
            "Generates the human-readable response sent back to the user."
        ),
        model_key="messenger",
        prompt_filename="messenger.md",
        python_entry="kiso.brain.text_roles.run_messenger",
    ),
    RoleMeta(
        name="paraphraser",
        description=(
            "Defense against prompt injection: rewrites untrusted "
            "external messages as summaries before they reach the planner."
        ),
        model_key="paraphraser",
        prompt_filename="paraphraser.md",
        python_entry="kiso.brain.text_roles.run_paraphraser",
    ),
    RoleMeta(
        name="planner",
        description=(
            "Interprets the user request and produces the JSON task plan."
        ),
        model_key="planner",
        prompt_filename="planner.md",
        python_entry="kiso.brain.planner.run_planner",
    ),
    RoleMeta(
        name="reviewer",
        description=(
            "Validates task outputs against expectations and decides "
            "replan vs done."
        ),
        model_key="reviewer",
        prompt_filename="reviewer.md",
        python_entry="kiso.brain.reviewer.run_reviewer",
    ),
    RoleMeta(
        name="summarizer",
        description=(
            "Compresses conversation history into structured summaries."
        ),
        model_key="summarizer",
        prompt_filename="summarizer.md",
        python_entry="kiso.brain.text_roles.run_summarizer",
    ),
    RoleMeta(
        name="worker",
        description=(
            "Translates task descriptions into shell commands for execution."
        ),
        model_key="worker",
        prompt_filename="worker.md",
        python_entry="kiso.brain.text_roles.run_worker",
    ),
    RoleMeta(
        name="sampler",
        description=(
            "Fulfils sampling/createMessage requests from MCP servers "
            "(LLM delegated to the kiso client; system prompt supplied "
            "by the requesting server)."
        ),
        model_key="sampler",
        prompt_filename="sampler.md",
        python_entry="kiso.mcp.sampling.handle_sampling_request",
    ),
    RoleMeta(
        name="mcp_repair",
        description=(
            "One-shot repair of invalid MCP call args against the "
            "method's input schema, before escalating to a replan."
        ),
        model_key="worker",
        prompt_filename="mcp_repair.md",
        python_entry="kiso.brain.mcp_repair.repair_mcp_args",
    ),
)


ROLES: dict[str, RoleMeta] = {r.name: r for r in _ROLES}


def list_roles() -> list[RoleMeta]:
    """Return all roles in declaration order."""
    return list(_ROLES)


def get_role(name: str) -> RoleMeta | None:
    """Return the metadata for a single role, or ``None`` if unknown."""
    return ROLES.get(name)
