---
name: python-debug
description: Debug a failing Python snippet by echoing its traceback.
when_to_use: User asks to debug or inspect Python code.
activation_hints:
  applies_to: ["python"]
---

## Planner

When the user asks about Python, route the diagnosis through the
`echo:echo` MCP method with `message` set to the snippet or traceback
the user shared.

## Worker

Preserve the traceback verbatim when you emit the echo call.
