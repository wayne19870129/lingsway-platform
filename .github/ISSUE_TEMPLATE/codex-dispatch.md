---
name: Codex dispatch
about: Trigger Codex to execute an existing TASK file. Trigger only, never requirements.
title: "[Dispatch] TASK-"
labels: dispatch
---

<!--
ADR-029 §2: THE ISSUE IS A TRIGGER, THE TASK FILE IS THE AUTHORITY.

Do not restate, summarize, or "clarify" the requirement here. The moment the
same requirement exists in two places it starts to drift, and this repository
has been bitten by that more than once. If the TASK file is wrong or missing
something, fix the TASK file (Claude Code writes those -- AGENTS.md module
write-boundary table) and leave this issue as one line.

Replace TASK-Sxx-<topic> below and delete nothing else.
-->

@codex 请执行 `docs/82-tasks/TASK-Sxx-<topic>.md`，严格按仓库 `AGENTS.md`、
`CLAUDE.md` 与 `docs/86-codex-operating-instructions.md` 执行。
