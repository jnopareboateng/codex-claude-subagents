<div align="center">

# codex-claude-subagents

### Use Claude as subagents in Codex.

Spawn scoped, resumable Claude CLI workers from inside a Codex session —
no manual context-shuttling, no framework, stdlib only.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Requires Claude CLI](https://img.shields.io/badge/requires-claude%20CLI-blueviolet)](https://docs.anthropic.com/en/docs/claude-code)

<img src="assets/architecture.png" alt="Architecture" width="720">

</div>

---

## Why

Codex orchestrates; Claude does the deep, careful work. This skill lets Codex
launch Claude CLI as a scoped worker — directory-limited, session-resumable,
every run recorded in a ledger Codex can audit or resume from later. OpenAI
ships the Codex-in-Claude-Code direction; this is the reverse.

## Install

```bash
cp -R skills/claude-subagents ~/.codex/skills/
```

Restart Codex afterward — skills are discovered at session start.

**Requires:** [Codex CLI](https://github.com/openai/codex), [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (authenticated), Python 3.9+.

## Quickstart

```bash
# read-only audit
python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --task audit-security --prompt examples/prompts/read-only-audit.md

# scoped fix
python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --task fix-auth --prompt examples/prompts/scoped-fix.md --write-scope src/auth

# resume by session id (see .agent-runs/claude/ledger.json)
python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --task fix-auth --prompt examples/prompts/scoped-fix.md \
  --session-id <id> --write-scope src/auth
```

From inside Codex, just ask: *"Delegate the auth refactor to a Claude worker
scoped to `src/auth`."*

## CLI reference

| Flag | Required | Meaning |
|---|---|---|
| `--task` | yes | kebab-case task id — names the run's log directory |
| `--prompt` | yes | markdown prompt file sent to the worker |
| `--write-scope` | no | dir Claude may edit (repeatable); omit = read-only |
| `--session-id` | no | resume a previous Claude session |
| `--model` / `--effort` | no | default `sonnet` / `high` |
| `--permission-mode` | no | default `acceptEdits` — `bypassPermissions` is rejected |

## Logs and the worker contract

Every run writes to `.agent-runs/claude/<task>/` (auto-gitignored): `stream.jsonl`,
`stderr.log`, `prompt.md`, and `summary.md` (the worker's own report). A single
`ledger.json` at `.agent-runs/claude/` indexes every task by session id, status,
and path.

Every prompt is prepended with a contract: Codex leads, Claude stays inside
`--write-scope`, and the worker must leave a summary covering Outcome, Files
Changed, Verification, Risks, and Next.

## FAQ

**Can workers run in parallel?**
Yes — background multiple launcher calls with distinct `--task` ids. Ledger
writes are file-locked, so concurrent completions can't race or drop entries.

**What happens if a session is already in use?**
The run is marked `status: locked` in the ledger and exits `3`, instead of
failing silently.

**Can Codex see progress mid-run?**
Not live — feedback is post-hoc via the summary and ledger. For detached,
long-running workers, Codex uses a bounded monitoring automation instead of
polling a held-open shell call (see `SKILL.md`); you can still tail
`.agent-runs/claude/<task>/stream.jsonl` from another shell for raw visibility.

**Why is `bypassPermissions` not an option?**
Deliberately excluded. Only `default`, `acceptEdits`, and `autoEdit` are accepted.

## Contributing

Issues and PRs welcome — keep it stdlib-only, no new runtime dependencies.
