<!-- Maintainer note (M1294, 2026-04-09): This role is deliberately kept separate from kiso/roles/classifier.md. The two prompts share <5% text, the categories are disjoint, and merging would force a single LLM call to choose between 8 categories (strictly worse for accuracy). See devplan/v0.9-wip.md M1294 before consolidating. -->

A job is currently running with this goal: "{plan_goal}"
{recent_conversation}The user sent a new message: "{new_message}"

Classify the intent of the new message into exactly one category:
- stop: user wants to cancel or abort the current job
- update: user is modifying parameters of the current job (e.g. "use port 8080 instead")
- independent: unrelated request that can wait until the current job finishes
- conflict: contradicts or replaces the current job entirely (e.g. "no, do X instead")

Respond with ONLY the category word (stop/update/independent/conflict), nothing else.
