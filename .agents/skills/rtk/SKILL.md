---
name: rtk
description: Reduce shell output with RTK for supported development commands.
---

# RTK

- Trigger before shell commands. Check `command -v rtk` once per session. If absent, report once and use native commands; a source folder is not an installation.
- When present, check `rtk --version` and `rtk --help`. Prefer supported equivalents, e.g. `rtk git status`, `rtk git diff`, `rtk pytest`, `rtk read FILE`.
- For search, prefer `rg`; use its RTK equivalent only when installed help/rewrite confirms the flags are supported.
- `rtk rewrite 'git status'` previews a mapping. Inspect the result; never blindly `eval` it or use it to bypass approval.
- Do not prefix every command mechanically. Unsupported commands, exact machine-readable output and complete file reads use native commands or `rtk proxy COMMAND`.
- Preserve exit status and actionable errors. Recover omitted detail from saved output; avoid rerunning mutating commands merely to obtain logs.
- Keep original SWE-bench test logs for evaluation. Compressed output alone cannot prove test execution or classify a bug.
- This is a prompt-level trigger, not an installed hook. Consult upstream before installing or configuring RTK; do not run copied installers automatically.

Source inspected: [rtk-ai/rtk](https://github.com/rtk-ai/rtk), local snapshot version `0.49.0`.
RTK compresses supported command output, not every command or tool call.
