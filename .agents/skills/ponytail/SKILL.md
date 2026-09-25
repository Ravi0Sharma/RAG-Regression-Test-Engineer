---
name: ponytail
description: Use the smallest correct solution when writing, editing or fixing code.
---

# Ponytail

After understanding the problem, stop at the first sufficient option:

1. Skip work that is not required.
2. Reuse an existing repo helper or pattern.
3. Use the standard library or a native platform feature.
4. Use an installed dependency.
5. Write the minimum clear code; add dependencies only when justified.

- Fix root causes; inspect affected callers. Avoid speculative abstractions, configuration and boilerplate.
- Preserve validation, safety, accessibility and explicit requirements. Brevity never excuses incorrect behavior.
- Check non-trivial behavior with a focused test or runnable example. Skip tests that merely mirror trivial edits.
- Explain deliberate limitations and when they matter. Keep teaching useful; no arbitrary explanation-length limit.
- `/ponytail lite`: offer simplest option; `full` (default): follow ladder; `ultra`: challenge unnecessary work. Stop when requested.
