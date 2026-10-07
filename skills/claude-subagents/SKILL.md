---
name: claude-subagents
description: Delegate a bounded task to a Claude Code CLI worker from Codex - cross-vendor review, scoped implementation in a disjoint path, or a second opinion - with enforced permissions, a post-run scope check, and file-backed runs Codex polls and resumes.
---

# claude-subagents

Codex leads. A Claude worker does one bounded task non-interactively and returns a final
message. `scripts/csa.py` (stdlib Python, WSL/Linux only) starts, supervises, reconciles and
records every run. The worker never asks questions: anything not pre-approved is denied.

## When to delegate

- Cross-vendor review of Codex's own changes (a worker never approves its own output).
- Bounded implementation in a scope disjoint from what Codex is editing.
- Second opinion on a design, bug, or plan.
- Not: native-app GUI checks (Codex Desktop computer use), anything needing a human approval
  mid-task, work that needs the web, or work that is faster to do inline. Keep those with Codex.

## Invocation (exactly two forms)

WSL shell:
```bash
python3 ~/.codex/skills/claude-subagents/scripts/csa.py <verb> --cwd /home/<you>/projects/<repo> ...
```
Windows Codex Desktop (PowerShell):
```powershell
wsl.exe -d Ubuntu-22.04 -e python3 /home/<you>/.codex/skills/claude-subagents/scripts/csa.py <verb> --cwd /home/<you>/projects/<repo> ...
```

`-e` runs no shell, so nothing is expanded: use absolute Linux paths, never `~` or `$VAR`.
`--cwd`, `--prompt` and `--schema` also accept `\\wsl.localhost\Ubuntu-22.04\...` paths (one or
two leading backslashes). Write each prompt as UTF-8 to `<repo>/.agent-runs/prompts/<task>.md`
(git-ignored) and pass that path; `--prompt -` reads stdin (WSL shells). JSON output is ASCII-only.
A `--cwd` on `/mnt/<drive>/` or `C:\` is refused unless `--allow-windows-fs`.

## Verbs

| Verb | Use |
|---|---|
| `start --task ID --prompt FILE\|- [--mode review\|write] [--scope REL]... [--allow-bash PREFIX]... [--worktree] [--schema FILE\|none] [--browser] [--model M] [--effort E] [--max-turns N] [--budget-usd X] [--timeout-min M] [--detach]` | New task. ID matches `^[a-z0-9][a-z0-9-]{0,63}$`; a used ID is refused (use `resume` or a new ID). |
| `resume --task ID --prompt FILE\|- [--allow-bash PREFIX]... [--browser] [--model M] [--effort E] [caps] [--detach]` | New attempt in the same Claude session (`claude --resume`). Terminal tasks only. `--allow-bash`/`--browser` are added for this and later attempts; each attempt records its own in `run.json`. |
| `status [--task ID] [--wait SEC] [--json]` | Reconciles liveness, then reports. `--wait` long-polls and returns on the first transition, or at once if nothing is running. |
| `result --task ID [--json]` | Final message and `structured_output` of the latest attempt. |
| `cancel --task ID` | Only the attempt running when called: SIGINT to its process group, SIGTERM after 15 s, SIGKILL 10 s later; status `cancelled`. |
| `list [--active] [--json]` | All tasks of the repo. |
| `doctor` | Checks every flag and option value the launcher emits against the installed `claude` (no API calls; permission-rule strings are not validated). |

All verbs except `doctor` take `--cwd`. Exit 0 means the verb worked; the run outcome is in the
JSON on stdout. Refusals print `{"error": ...}` and exit 1; malformed arguments print usage to
stderr and exit 2. A task with unreadable files is `unknown` with `error`, active if `lock_held`.

## Review mode (default)

Edit, Write and NotebookEdit are removed. Bash runs Claude Code's built-in read-only commands
(`cat`, `grep`, `find`, `ls`, `diff`, `stat`, read-only `git`, ...; not configurable) plus
`git status/diff/log/show/blame/grep/ls-files/rev-parse`, `rg`, `wc`, `head`, `tail` and each
`--allow-bash` prefix. Output is forced into the review preset `{verdict: approve|request-changes|inconclusive,
findings: [{severity, title, file, line, evidence, recommendation}], verification: [], risks: []}`,
returned as `structured_output`; `--schema FILE` swaps it, `--schema none` gives prose. Any
change git can see outside `.agent-runs/` makes the run `scope-violation`.

## Write mode and parallel runs

`--mode write --scope src/auth [--scope docs/auth.md]` allows Edit/Write only under those paths
(relative to `--cwd`; a path inside a submodule is refused), plus the review-mode Bash set with
its `--allow-bash "pytest -q"` prefixes. After the run, every changed path outside the scopes is
listed in `changes.json` and the status becomes `scope-violation`. Nothing is ever reverted.
Not a sandbox: `--allow-bash` commands are trusted code; they and whatever they run (a test
suite's `conftest.py`) have network access and your full user permissions, and Edit/Write rules
do not bind them. The scope check sees only what git sees: never ignored files, `.git` internals,
paths outside the repo, edits inside an already-dirty submodule, or changes undone before exit.

The diff check cannot tell who changed a file, so a write run needs the main working tree to
itself: it is refused while any other run is active there, and a review while a main-tree writer
runs (reviews may overlap). Start the second run with `--worktree`: Claude then runs in
`<repo>/.agent-runs/wt/<task>` on branch `csa/<task>` from HEAD (no uncommitted changes). Workers
never commit: review the diff in that tree, commit there yourself, merge `csa/<task>`, verify,
then `git worktree remove` it (never `--force` over uncommitted work). Hands off while it runs.

## Browser checks

`--browser` adds a headless, isolated `@playwright/mcp@0.0.83` server (pinned; `npx -y`): network
access, the browser loads any URL and can submit forms. Allowed: navigate(_back), click, hover,
drag, type, press_key, select_option, fill_form, handle_dialog, wait_for, resize, emulate_media,
tabs, close. Denied: run_code_unsafe, evaluate, file_upload, drop, and the tools whose `filename`
writes anywhere in the repo (snapshot, take_screenshot, console_messages, network_request(s),
find). Actions save the page snapshot and console log to `a<N>/browser/` and return their paths
for the worker to read; no screenshots. `file://` is blocked: put a dev server's URL in the prompt.

## Monitoring flow

1. `start --detach` each task; it returns in under a second with `{task, attempt, pid, pgid, run_dir}`.
2. In the same turn, run `status --wait 540 --json` with a shell-tool timeout of at least
   600000 ms; if the tool yields before the command returns, keep polling that session.
3. Report each entry of `transitions`; run `result --task ID --json` for every terminal task.
4. Repeat 2-3 while `active > 0`. Only for runs expected to exceed ~30 min, create a Codex
   Desktop heartbeat automation that runs `status --json` and deletes itself once `active` is 0.

From Windows, a detached run outlives `wsl.exe` only while the distro stays up: WSL stops an
idle distro after 15 s unless `%UserProfile%\.wslconfig` has `[general]` `instanceIdleTimeout=-1`.
A run lost that way reports `interrupted` with `launcher_lost`. Only `status: complete` is
success; never treat a partial `stream.jsonl` as a result. Continue only with `resume`.

| Status | Meaning | Next |
|---|---|---|
| `running` | launcher or worker alive (`worker_alive`, `idle_s` in status) | wait |
| `complete` | exit 0 (unknown if `launcher_lost`), result `success`, not `is_error`, no scope violation | use the result |
| `failed` | error result (API error, unknown model, max turns, budget), no result, or the scope check could not run (`error`) | read `result`; `resume` or new task |
| `timeout` | hit `--timeout-min` | `resume` with a larger cap or a narrower prompt |
| `cancelled` | `cancel` was run | - |
| `interrupted` | launcher killed, or worker died without a result | `resume` |
| `scope-violation` | files changed outside scope (any change in review mode) | inspect `changes.json`; never auto-revert |

## Models and caps

Without `--model`, Claude's configured default is used. Prefer the CLI aliases `sonnet`, `opus`,
`haiku` (current release) or a full Claude model id, passed verbatim. Never pass Codex/OpenAI
model names: an unknown model fails the run (the result names it); check `model_resolved`.
`--effort` (low|medium|high|xhigh|max) is passed only when given. Caps default to `--max-turns 80
--budget-usd 5 --timeout-min 45` (positive, finite); `resume` inherits them unless overridden. The
budget is Claude's estimate; on `resume`, `cost_usd` includes earlier attempts; a cancelled turn: 0.

## Run layout

`<repo>/.agent-runs/claude/<task>/` holds `run.json` (atomic state: task, mode, scopes,
session_id, caps, status, attempts[]), `active.lock` (flock the launcher holds until the worker's
process group is empty) and one never-overwritten `a<N>/` per attempt (`prompt.md stream.jsonl
stderr.log result.md result.json changes.json [mcp.json browser/]`). `--worktree` checkouts are in
`<repo>/.agent-runs/wt/<task>/`. `.agent-runs/` goes into `.git/info/exclude`; a v1 `ledger.json`
is ignored.

## Worker environment

`claude -p --output-format stream-json --verbose`, prompt on stdin, `--permission-mode dontAsk`,
`--strict-mcp-config`, settings `disableAllHooks` and `blockReadsOutsideWorkingDirectories`
(file tools and read-only Bash refuse paths outside the working directory: pass the repo root as
`--cwd` if the worker must read all of it). Always denied: WebFetch, WebSearch, DesignSync,
PushNotification, RemoteTrigger, SendMessage, Monitor, ScheduleWakeup, EnterWorktree, Workflow.
Auto memory, cron and background tasks are off. Still inherited: user and project CLAUDE.md,
plugins, skills, settings `env`/`model`, and allow rules from every settings file (a deny always
wins). Appended contract: no questions, no retrying denials, no commits or report files, and the
deliverable (structured output, or a prose final message).

## Troubleshooting

- `doctor` first: it parses `claude --help` and runs every argv variant with empty stdin.
- Denied tool calls: `result --json` shows `denial_details`; `resume --allow-bash PREFIX`.
- `failed` with `is_error`: the result text is Claude's error (API, auth, model, budget).
- Raw evidence: `a<N>/stream.jsonl`, `a<N>/stderr.log`, `a<N>/launcher.log` (detached runs).
