---
name: claude-subagents
description: Use when Codex should orchestrate Claude CLI as resumable, scoped subagents with file-backed logs kept out of git.
---

# claude-subagents

Orchestrate resumable Claude CLI workers as scoped subagents from within Codex.
Claude executes bounded work; Codex owns orchestration, monitoring, wakeups,
state transitions, and user-facing completion reporting.

## Overview

Codex is always the lead orchestrator. Claude workers are scoped — each one knows its allowed write path and reports results back via structured logs.

## Usage

```bash
python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --cwd <repo-root> \
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

Always pass an explicit `--cwd`. Use the smallest possible write scope. Review
workers should be read-only; workers that must write summaries may write only
`.agent-runs/claude`.

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

The foreground launcher blocks until the Claude worker exits. For work that may
outlive one tool-call window, detach it and persist the launcher PID:

```bash
repo=/path/to/repo
task=modelmeta-spec-debate
run_dir="$repo/.agent-runs/claude"
mkdir -p "$run_dir"
nohup python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
  --cwd "$repo" \
  --task "$task" \
  --prompt /path/to/prompt.md \
  --model sonnet \
  --effort high \
  --write-scope .agent-runs/claude \
  >"$run_dir/$task.launcher.log" 2>&1 &
echo $! >"$run_dir/$task.launcher.pid"
```

Per-task logs do not collide; `ledger.json` is file-locked. A session ID must
never be shared by two active workers.

If a session is already in use, the run is marked `status: locked` and the
launcher exits `3`. Codex receives the worker's result through the summary and
ledger rather than by holding a shell call open.

## Parallel fan-out

Independent tasks can be launched in parallel from one Codex shell call. Give
each task its own prompt, task ID, session, and disjoint write scope:

```bash
repo=/path/to/repo
run_dir="$repo/.agent-runs/claude"
mkdir -p "$run_dir"

tasks=(schema-review hashing-review cli-review)
prompts=(
  "$repo/.agent-runs/prompts/schema-review.md"
  "$repo/.agent-runs/prompts/hashing-review.md"
  "$repo/.agent-runs/prompts/cli-review.md"
)
scopes=(
  ".agent-runs/claude/schema-review"
  ".agent-runs/claude/hashing-review"
  ".agent-runs/claude/cli-review"
)

for i in "${!tasks[@]}"; do
  mkdir -p "$repo/${scopes[$i]}"
  nohup python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \
    --cwd "$repo" \
    --task "${tasks[$i]}" \
    --prompt "${prompts[$i]}" \
    --model sonnet \
    --effort high \
    --write-scope "${scopes[$i]}" \
    >"$run_dir/${tasks[$i]}.launcher.log" 2>&1 &
  echo $! >"$run_dir/${tasks[$i]}.launcher.pid"
done
```

Do not fan out tasks with overlapping write scopes. If tasks need to modify the
same file, serialize them or make the first workers read-only and integrate
their findings in Codex.

## Requirements

- Claude CLI installed and authenticated (`claude --version` should succeed).
- Python 3.9+ (stdlib only, no extra packages).

## Codex-owned monitoring

Claude Code is not the monitoring loop. Codex uses Monitor plus a
ScheduledWakeup/heartbeat automation only while one or more detached workers
are active.

The monitor reads the launcher ledger, PID files, stderr, and summary files and
models each task with these states:

| State | Evidence | Action |
|---|---|---|
| `running` | launcher/worker PID is alive and ledger is active | wait for the next wakeup |
| `complete` | summary exists, ledger is `complete`, exit code is zero | report result |
| `needs-attention` | process exited, nonzero exit, or summary missing | inspect logs; do not claim success |
| `locked` | stderr reports an active session lock | do not duplicate; wait or resume deliberately |

On a wakeup, Codex checks all active task IDs in one bounded snapshot, reports
only meaningful state changes, and retires the automation when no active run
remains. The automation must not edit project source files. It may request a
resume or restart only after confirming the old process is gone and the task's
session/ledger state makes that safe.

This separation is normative: Claude workers do the work, while Codex
Monitor/ScheduledWakeup owns waiting. Never hold a single shell call open for
the entire worker lifetime and never treat a partial JSONL stream as completion.

## Example prompts

See `examples/prompts/` for ready-made audit and fix prompts.
