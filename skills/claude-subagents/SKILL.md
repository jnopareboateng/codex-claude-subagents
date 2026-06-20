---
name: claude-subagents
description: Use when Codex should orchestrate Claude CLI as resumable, scoped subagents with file-backed logs kept out of git.
---

# claude-subagents

Orchestrate resumable Claude CLI workers as scoped subagents from within Codex.

## Overview

Codex is always the lead orchestrator. Claude workers are scoped — each one knows its allowed write path and reports results back via structured logs.

## Usage

```bash
python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --task <task-id> \
  --prompt <path-to-prompt.md> \
  [--write-scope <directory>] \
  [--session-id <id>]
```

| Argument | Required | Description |
|---|---|---|
| `--task` | yes | Unique task identifier used for log filenames |
| `--prompt` | yes | Path to the prompt file Codex provides |
| `--write-scope` | no | Directory the worker is allowed to write to (empty = read-only) |
| `--session-id` | no | Resume a previous Claude session by ID |
| `--model` | no | Claude model, default `sonnet` |
| `--effort` | no | Reasoning effort, default `high` |

## Logs

All logs are written under `.agent-runs/claude/` in the current working directory. This path is automatically added to `.gitignore`.

| File | Contents |
|---|---|
| `ledger.json` | Indexed record of all runs (task, session ID, timestamp, exit code) |
| `<task>.jsonl` | Streaming structured output from claude |
| `<task>.stderr.log` | stderr from the claude process |
| `<task>.prompt.md` | The full injected prompt (worker contract + user prompt) |
| `<task>.summary.md` | Final summary written by the Claude worker |

## Worker contract

The launcher injects a preamble into every prompt:

- Codex is lead orchestrator; Claude is a scoped worker.
- Writes are restricted to `--write-scope`.
- Raw logs are handled by the launcher; the worker must not summarise to stdout.
- The worker must write its final compact summary to `.agent-runs/claude/<task>.summary.md`.
- The launcher defaults to Sonnet with high reasoning effort.

## Concurrency and feedback model

Each launcher call blocks until the Claude worker exits — one worker at a time per call. To run workers in parallel, background multiple launcher calls with distinct `--task` IDs. Per-task log files do not collide, but `ledger.json` has no write lock; simultaneous completions can lose a ledger entry.

Feedback is post-hoc: Codex receives nothing until the worker finishes and writes its summary. There is no mid-run signalling in either direction. To monitor progress during a long run, tail `.agent-runs/claude/<task>.jsonl` from a separate shell.

## Requirements

- Claude CLI installed and authenticated (`claude --version` should succeed).
- Python 3.9+ (stdlib only, no extra packages).

## Example prompts

See `examples/prompts/` for ready-made audit and fix prompts.
