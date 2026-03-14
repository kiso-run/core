A job is currently running with this goal: "{plan_goal}"
The user sent a new message: "{new_message}"

Classify the intent of the new message into exactly one category:
- stop: user wants to cancel or abort the current job
- update: user is modifying parameters of the current job (e.g. "use port 8080 instead")
- independent: unrelated request that can wait until the current job finishes
- conflict: contradicts or replaces the current job entirely (e.g. "no, do X instead")

Respond with ONLY the category word (stop/update/independent/conflict), nothing else.
