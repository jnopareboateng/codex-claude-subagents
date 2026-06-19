# codex-claude-subagents

Codex leads; Claude works; logs stay out of git.

A Codex skill that lets Codex orchestrate resumable Claude CLI workers as scoped subagents — each worker runs with a defined write scope, logs to `.agent-runs/claude/`, and can be resumed by session ID.

## Install

```bash
cp -R skills/claude-subagents ~/.codex/skills/
```

Restart your Codex session after installing — Codex does not discover new skills until a new session starts.

## Quickstart

### Read-only audit

```bash
python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --task audit-security \
  --prompt examples/prompts/read-only-audit.md
```

### Scoped fix

```bash
python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --task fix-auth \
  --prompt examples/prompts/scoped-fix.md \
  --write-scope src/auth
```

### Resume a previous run

```bash
python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --task fix-auth \
  --prompt examples/prompts/scoped-fix.md \
  --session-id <session-id> \
  --write-scope src/auth
```

## How it works

1. Codex invokes `scripts/run_claude_subagent.py` with a task name, prompt file, and optional write scope.
2. The script injects a worker contract (Codex = lead, Claude = scoped worker), launches `claude --model sonnet --effort high`, and redirects output to `.agent-runs/claude/`.
3. On completion the script writes a `ledger.json` entry and a `<task>.summary.md`.
4. Pass `--session-id` to resume an interrupted run from where it left off.

## Logs

```
.agent-runs/claude/
  ledger.json              # all runs, indexed by task + session
  <task>.jsonl             # structured output stream
  <task>.stderr.log        # stderr from claude process
  <task>.prompt.md         # injected prompt (contract + user prompt)
  <task>.summary.md        # final summary written by the worker
```

`.agent-runs/` is automatically added to `.gitignore`.

## Limitations

- Requires [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated.
- Codex does not automatically discover newly installed skills — start a new session after `cp`.
- Broad `--write-scope` values (e.g. `.` or `/`) are unsafe; always scope to the minimum required directory.
- Resumption depends on Claude CLI session-id support; behaviour may vary across CLI versions.
