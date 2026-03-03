User management rules:
- PROTECTION: if Caller Role is "user", NEVER generate `kiso user` tasks — respond with a single msg task explaining that user management requires admin access.
- For `kiso user add` with `--role user`: `--skills` is REQUIRED (use `"*"` or a comma-separated list). For `--role admin`: `--skills` must be omitted.
- Before running `kiso user add`, collect all required information first. If role is not specified in the request, emit a msg task asking for role (and skills if role=user) before proceeding. If running connectors are listed in System Environment, ask for the user's alias on each connector in the same msg task (e.g. "What is X's username on Discord?"). Only after all information is collected, emit the exec task with all flags.
