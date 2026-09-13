# TASK-T22 — Custom automation App proposal (superseded)

This proposal was never implemented and is superseded by Issue #87 /
TASK-T23. The repository now deliberately uses Claude Code Action's
standard authentication path and does not mint a separate custom GitHub
App installation token.

No custom App credentials are required. The only Claude authentication
secret used by the workflow is `CLAUDE_CODE_OAUTH_TOKEN`.

See `TASK-T23-simplify-claude-workflow.md` for the current design and
validation plan.
