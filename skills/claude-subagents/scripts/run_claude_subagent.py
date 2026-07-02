#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl

    def _lock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _unlock(fh) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)
except ImportError:  # Windows
    import msvcrt

    def _lock(fh) -> None:
        fh.seek(0, 2)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]

    def _unlock(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]

_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
_SAFE_PERMISSION_MODES = ("default", "acceptEdits", "autoEdit")
_SESSION_LOCKED_RE = re.compile(r"session id .* already in use", re.IGNORECASE)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def ledger_lock(ledger_path: Path):
    """Serialize ledger read-modify-write across concurrent launcher processes."""
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        _lock(fh)
        try:
            yield
        finally:
            _unlock(fh)


def load_ledger(path: Path) -> dict:
    if not path.exists():
        return {"runs": []}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"runs": []}


def save_ledger(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-process/unique name: two concurrent writers must never share a tmp path.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def ensure_agent_runs_ignored(cwd: Path) -> None:
    gitignore = cwd / ".gitignore"
    entry = ".agent-runs/"
    if gitignore.exists():
        text = gitignore.read_text()
        if any(line.strip().rstrip("/") == ".agent-runs" for line in text.splitlines()):
            return
        suffix = "" if text.endswith("\n") else "\n"
        with gitignore.open("a") as handle:
            handle.write(f"{suffix}{entry}\n")
        return
    gitignore.write_text(f"{entry}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a resumable Claude worker with file-backed logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Trust boundary: prompt files are treated as trusted input.\n"
            "Vet any user-supplied prompt content before passing it through."
        ),
    )
    parser.add_argument("--cwd", default=".", help="Repository/worktree root for the Claude run.")
    parser.add_argument(
        "--task-id", "--task", required=True,
        help="Stable kebab-case task id [a-z0-9][a-z0-9-]{0,63}.",
    )
    parser.add_argument("--prompt-file", "--prompt", required=True, help="Prompt markdown file.")
    parser.add_argument(
        "--session-id", default="",
        help="Existing UUID to resume; a fresh one is generated when omitted.",
    )
    parser.add_argument("--name", default="", help="Claude session display name; defaults to task id.")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument(
        "--effort", default="high",
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    parser.add_argument(
        "--write-scope", action="append", default=[],
        help="Relative path Claude may edit (repeatable). Omit for read-only.",
    )
    parser.add_argument(
        "--permission-mode", default="acceptEdits",
        choices=list(_SAFE_PERMISSION_MODES),
        help="Claude permission mode. bypassPermissions is intentionally excluded.",
    )
    args = parser.parse_args()

    # ── pre-flight checks ────────────────────────────────────────────────────
    if not shutil.which("claude"):
        print(
            "claude CLI not found in PATH.\n"
            "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code",
            file=sys.stderr,
        )
        return 2

    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        print(f"cwd does not exist: {cwd}", file=sys.stderr)
        return 2

    prompt_path = Path(args.prompt_file).resolve()
    if not prompt_path.exists():
        print(f"prompt file not found: {prompt_path}", file=sys.stderr)
        return 2

    # ── task-id validation (path-traversal guard) ────────────────────────────
    task_id = args.task_id.strip().lower().replace(" ", "-")
    if not _TASK_ID_RE.fullmatch(task_id):
        print(
            f"invalid task-id {args.task_id!r}.\n"
            "Must match [a-z0-9][a-z0-9-]{0,63} (kebab-case, no dots or slashes).",
            file=sys.stderr,
        )
        return 2

    # ── session-id validation ────────────────────────────────────────────────
    if args.session_id:
        try:
            uuid.UUID(args.session_id)
            session_id = args.session_id
        except ValueError:
            print(f"invalid session-id {args.session_id!r}: must be a UUID.", file=sys.stderr)
            return 2
    else:
        session_id = str(uuid.uuid4())

    # ── write-scope validation (no escaping cwd) ─────────────────────────────
    write_scope: list[str] = []
    for raw in args.write_scope:
        p = raw.strip()
        if not p:
            continue
        resolved = (cwd / p).resolve()
        try:
            resolved.relative_to(cwd)
        except ValueError:
            print(
                f"write-scope {raw!r} resolves outside the working directory.\n"
                "Scope must be a relative path inside the repo root.",
                file=sys.stderr,
            )
            return 2
        write_scope.append(p)

    # ── set up run directory ─────────────────────────────────────────────────
    ensure_agent_runs_ignored(cwd)
    run_dir = cwd / ".agent-runs" / "claude"
    run_dir.mkdir(parents=True, exist_ok=True)

    name = args.name or task_id
    log_path = run_dir / f"{task_id}.jsonl"
    stderr_path = run_dir / f"{task_id}.stderr.log"
    summary_path = run_dir / f"{task_id}.summary.md"
    stored_prompt_path = run_dir / f"{task_id}.prompt.md"
    ledger_path = run_dir / "ledger.json"

    scope_text = (
        "\n".join(f"- {p}" for p in write_scope)
        if write_scope
        else "- read-only; do not edit any files"
    )
    base_prompt = prompt_path.read_text()
    full_prompt = f"""{base_prompt.rstrip()}

---

CLAUDE WORKER CONTRACT
- Codex is lead/orchestrator. You are a scoped worker.
- Session id: {session_id}
- Task id: {task_id}
- Allowed write scope:
{scope_text}
- Raw logs are redirected by the launcher; do not summarize to stdout.
- Write your final compact summary to: {summary_path}
- If allowed to edit, stay strictly inside the write scope. If a broader edit is unavoidable, stop and explain why before proceeding.
- Summary sections required: Outcome, Files Inspected, Files Changed, Verification, Risks, Next.
"""
    stored_prompt_path.write_text(full_prompt)

    # ── ledger: record run as started ────────────────────────────────────────
    run = {
        "task_id": task_id,
        "session_id": session_id,
        "name": name,
        "status": "running",
        "cwd": str(cwd),
        "write_scope": write_scope,
        "prompt_path": str(stored_prompt_path),
        "log_path": str(log_path),
        "stderr_path": str(stderr_path),
        "summary_path": str(summary_path),
        "started_at": now(),
        "finished_at": None,
        "returncode": None,
    }
    with ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        ledger["runs"] = [r for r in ledger.get("runs", []) if r.get("task_id") != task_id]
        ledger["runs"].append(run)
        save_ledger(ledger_path, ledger)

    # ── launch claude worker ─────────────────────────────────────────────────
    cmd = [
        "claude", "-p",
        "--model", args.model,
        "--effort", args.effort,
        "--name", name,
        "--session-id", session_id,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", args.permission_mode,
        full_prompt,
    ]

    try:
        with log_path.open("w") as out, stderr_path.open("w") as err:
            proc = subprocess.run(cmd, cwd=cwd, stdout=out, stderr=err, check=False)
    except FileNotFoundError:
        print("claude binary disappeared mid-run; is Claude Code installed?", file=sys.stderr)
        return 2

    # ── ledger: record completion ────────────────────────────────────────────
    stderr_text = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
    session_locked = bool(_SESSION_LOCKED_RE.search(stderr_text))
    if session_locked:
        status = "locked"
    elif proc.returncode == 0 and summary_path.exists():
        status = "complete"
    else:
        status = "needs-attention"

    with ledger_lock(ledger_path):
        ledger = load_ledger(ledger_path)
        for item in ledger.get("runs", []):
            if item.get("task_id") == task_id:
                item["status"] = status
                item["finished_at"] = now()
                item["returncode"] = proc.returncode
                item["summary_exists"] = summary_path.exists()
                break
        save_ledger(ledger_path, ledger)

    if session_locked:
        print(
            f"session {session_id} is already in use by another Claude process.\n"
            "Wait for it to exit, then resume with the same --session-id, or start "
            "a new task with a fresh session id.",
            file=sys.stderr,
        )

    print(json.dumps({
        "task_id": task_id,
        "session_id": session_id,
        "status": status,
        "returncode": proc.returncode,
        "log_path": str(log_path),
        "stderr_path": str(stderr_path),
        "summary_path": str(summary_path),
        "summary_exists": summary_path.exists(),
        "resume": f"--session-id {session_id}",
    }, indent=2))

    if session_locked:
        return 3
    return 0 if proc.returncode == 0 and summary_path.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
