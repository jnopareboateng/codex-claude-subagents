#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_ledger(path: Path) -> dict:
    if not path.exists():
        return {"runs": []}
    return json.loads(path.read_text())


def save_ledger(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
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
    parser = argparse.ArgumentParser(description="Run a resumable Claude worker with file-backed logs.")
    parser.add_argument("--cwd", default=".", help="Repository/worktree root for the Claude run.")
    parser.add_argument("--task-id", "--task", required=True, help="Stable lowercase task id.")
    parser.add_argument("--prompt-file", "--prompt", required=True, help="Prompt markdown file.")
    parser.add_argument("--session-id", default="", help="Existing UUID to resume/reuse; generated when omitted.")
    parser.add_argument("--name", default="", help="Claude session display name; defaults to task id.")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--write-scope", action="append", default=[], help="Relative path Claude may edit.")
    parser.add_argument("--permission-mode", default="acceptEdits")
    args = parser.parse_args()

    cwd = Path(args.cwd).resolve()
    prompt_path = Path(args.prompt_file).resolve()
    if not prompt_path.exists():
        print(f"prompt file not found: {prompt_path}", file=sys.stderr)
        return 2

    ensure_agent_runs_ignored(cwd)
    run_dir = cwd / ".agent-runs" / "claude"
    run_dir.mkdir(parents=True, exist_ok=True)

    task_id = args.task_id.strip().replace(" ", "-")
    session_id = args.session_id or str(uuid.uuid4())
    name = args.name or task_id

    log_path = run_dir / f"{task_id}.jsonl"
    stderr_path = run_dir / f"{task_id}.stderr.log"
    summary_path = run_dir / f"{task_id}.summary.md"
    stored_prompt_path = run_dir / f"{task_id}.prompt.md"
    ledger_path = run_dir / "ledger.json"

    write_scope = [p.strip() for p in args.write_scope if p.strip()]
    scope_text = "\n".join(f"- {p}" for p in write_scope) if write_scope else "- read-only; do not edit files"
    base_prompt = prompt_path.read_text()
    full_prompt = f"""{base_prompt.rstrip()}

---

CLAUDE WORKER CONTRACT
- Codex is lead/orchestrator. You are a scoped worker.
- Session id: {session_id}
- Task id: {task_id}
- Allowed write scope:
{scope_text}
- Raw logs are redirected by the launcher; do not summarize in stdout.
- Write your final compact summary to: {summary_path}
- If allowed to edit, stay inside the write scope unless you stop and explain why a broader edit is required.
- Summary sections required: Outcome, Files Inspected, Files Changed, Verification, Risks, Next.
"""
    stored_prompt_path.write_text(full_prompt)

    ledger = load_ledger(ledger_path)
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
    ledger["runs"] = [r for r in ledger.get("runs", []) if r.get("task_id") != task_id]
    ledger["runs"].append(run)
    save_ledger(ledger_path, ledger)

    cmd = [
        "claude",
        "-p",
        "--model",
        args.model,
        "--effort",
        args.effort,
        "--name",
        name,
        "--session-id",
        session_id,
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        args.permission_mode,
        full_prompt,
    ]

    with log_path.open("w") as out, stderr_path.open("w") as err:
        proc = subprocess.run(cmd, cwd=cwd, stdout=out, stderr=err, check=False)

    ledger = load_ledger(ledger_path)
    for item in ledger.get("runs", []):
        if item.get("task_id") == task_id:
            item["status"] = "complete" if proc.returncode == 0 and summary_path.exists() else "needs-attention"
            item["finished_at"] = now()
            item["returncode"] = proc.returncode
            item["summary_exists"] = summary_path.exists()
            break
    save_ledger(ledger_path, ledger)

    print(json.dumps({
        "task_id": task_id,
        "session_id": session_id,
        "returncode": proc.returncode,
        "log_path": str(log_path),
        "stderr_path": str(stderr_path),
        "summary_path": str(summary_path),
        "summary_exists": summary_path.exists(),
        "resume": f"claude --resume {session_id}",
    }, indent=2))
    return 0 if proc.returncode == 0 and summary_path.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
