# Execution Hooks

Hooks are shell commands that run before/after exec task execution.
They provide deterministic validation, logging, and blocking capabilities
beyond LLM-evaluated safety rules.

## Configuration

Add hooks to your `config.toml`:

```toml
[[hooks.pre_exec]]
command = "/usr/local/bin/validate-command.sh"
blocking = true  # non-zero exit → task blocked

[[hooks.post_exec]]
command = "/usr/local/bin/log-exec.sh"
# post-exec hooks are always non-blocking
```

## Hook Context

Hooks receive a JSON object via stdin:

### Pre-exec context
```json
{
  "event": "pre_exec",
  "command": "cat /etc/passwd",
  "detail": "Read system users file",
  "session": "host@user",
  "task_id": 42
}
```

### Post-exec context
```json
{
  "event": "post_exec",
  "command": "ls -la",
  "detail": "List directory contents",
  "session": "host@user",
  "task_id": 42,
  "exit_code": 0,
  "stdout": "...",
  "stderr": ""
}
```

## Behavior

- **Pre-exec hooks**: Run sequentially before the subprocess. If `blocking = true`
  and the hook exits non-zero, the task fails with "Blocked by pre-exec hook".
  The reviewer sees this and can replan.
- **Post-exec hooks**: Run asynchronously (fire-and-forget) after the subprocess.
  Failures are logged but don't affect the pipeline.
- **Timeout**: 10 seconds per hook. Timeout = pass (hook failure doesn't block).
- **Multiple hooks**: Executed in order. First blocking deny stops execution.

## Use Cases

- Command whitelist/blacklist validation
- Audit logging to external systems
- Cost tracking per command
- Custom sandboxing rules
