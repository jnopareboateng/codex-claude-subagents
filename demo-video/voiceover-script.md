# Demo Video Voiceover Script

## Title

Codex leads. Claude works. The logs prove it.

## Script

Codex and Claude are both strong coding agents. The friction is everything around them: keeping two tools open, copying context back and forth, remembering what was delegated, and proving what actually happened afterward.

This project removes that glue work. It is a Codex skill that lets Codex launch Claude CLI as a scoped worker from inside the repo.

The contract is simple. Codex stays the orchestrator. Claude gets a task, an optional write scope, a session ID, and a required summary path. If no write scope is provided, the worker is read-only.

The launcher is deliberately small: Python standard library only. It validates task IDs, rejects unsafe permission modes, injects the worker contract, stores the full prompt, streams structured logs, and writes a ledger entry for each run.

Here is the proof from this repository. The first run bootstrapped the public repo files: README, license, skill instructions, the launcher, and the example prompts.

Then a read-only smoke audit ran through the same system. It inspected the project, wrote a compact summary, and left the raw transcript, stderr log, prompt, summary, and ledger in `.agent-runs/claude`.

The ledger is the handoff record. It captures task ID, status, return code, session ID, write scope, prompt path, log path, stderr path, and summary path. That means a Codex session can resume or audit the work without guessing.

The safety boundary is visible in the CLI itself. Task IDs are constrained to kebab case. Write scopes must stay inside the repo. Permission mode is limited to default, acceptEdits, or autoEdit. `bypassPermissions` is not accepted.

So the value is not just “run Claude from Codex.” The value is delegated work with boundaries, resumability, and evidence. Codex leads the workflow. Claude does the scoped work. The repo keeps the receipt.

Install the skill, restart Codex, and ask Codex to delegate a read-only audit or a scoped fix. That is the whole loop.
