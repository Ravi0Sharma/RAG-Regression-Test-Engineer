# Project instructions

- Learning repo. User chooses architecture and approves implementation; explain options before writing project code. Carry out explicitly requested edits.
- Read [README.md](README.md) for scope and workflows. Optional local planning notes live in `.local-docs/requirements.md`. Keep unresolved choices open.
- Always use caveman tone: concise Swedish, no filler. Preserve meaning, uncertainty and technical details. Expand when teaching needs it; follow explicit tone changes.
- User preference: Astra implements and reviews; delegate verification and fresh test-generation model packets to Luna high. Docker executes the actual tests. Preserve clean packet context for model experiments.

## Skills

Read the applicable `SKILL.md` once; load others only when triggered.

- [caveman](.agents/skills/caveman/SKILL.md): every response; default `full`.
- [karpathy-guidelines](.agents/skills/karpathy-guidelines/SKILL.md): design, planning, code review or unclear requirements.
- [ponytail](.agents/skills/ponytail/SKILL.md): code creation, edits, refactoring or fixes.
- [rtk](.agents/skills/rtk/SKILL.md): before shell commands; prefer supported RTK commands when installed. Otherwise use native commands and report missing RTK once.

## Boundaries

- One agent in the product. Keep fix/facit and fixed-version results outside test generation.
- Preserve raw test evidence for evaluation; RTK compression is for development output.
- Use lowercase kebab-case for skill folders, `SKILL.md` for skill entrypoints and `AGENTS.md` for repo instructions.
- Keep docs short, links valid and requirements intact. Avoid duplicate instructions and vendored tool source unless needed.
- Verify changes with `python3 -m unittest discover -s tests -v`; run `python3 -m rag_regression demo --out NEW_PATH` for the offline flow.
- Keep `.env` local and git-ignored. Never populate it unless the user asks; live API runs require the user's key.
