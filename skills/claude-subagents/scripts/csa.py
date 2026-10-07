#!/usr/bin/env python3
"""csa: start and supervise Claude Code CLI workers for a Codex orchestrator.

Linux/WSL only, stdlib only. Verbs: start, resume, status, result, cancel, list,
doctor. Every run lives in <repo>/.agent-runs/claude/<task>/ (see SKILL.md).
"""
from __future__ import annotations

import argparse
import codecs
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows; main() refuses to run there
    fcntl = None  # type: ignore[assignment]

VERSION = "2.0.0"
TASK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TERMINAL = ("complete", "failed", "timeout", "cancelled", "interrupted", "scope-violation")
DEFAULT_CAPS = {"max_turns": 80, "budget_usd": 5.0, "timeout_min": 45.0}
KILL_GRACE_S = float(os.environ.get("CSA_KILL_GRACE_S", "15"))  # SIGINT -> SIGTERM
HARD_KILL_S = float(os.environ.get("CSA_HARD_KILL_S", "10"))  # SIGTERM -> SIGKILL
KILL_WAIT_S = 5.0  # SIGKILL -> process group empty
READONLY_BASH = ("git status", "git diff", "git log", "git show", "git blame", "git grep",
                 "git ls-files", "git rev-parse", "rg", "ls", "find", "wc", "head", "tail")
# Flags that turn the allowed read-only commands into file writers or command runners.
EXEC_FLAG_DENY = ("Bash(git *--output*)", "Bash(git grep *-O*)",
                  "Bash(git grep *--open-files-in-pager*)", "Bash(rg *--pre*)")
REVIEW_DENY = ("Edit", "Write", "NotebookEdit")
# Network egress, and orchestration that outlives or escapes the run (the init tool list of
# claude 2.1.290 still offers these with CLAUDE_CODE_DISABLE_CRON/_BACKGROUND_TASKS set). Deny
# beats allow from every settings scope, so the user's own allow rules cannot re-enable them.
ALWAYS_DENY = ("WebFetch", "WebSearch", "DesignSync", "PushNotification", "RemoteTrigger",
               "SendMessage", "Monitor", "ScheduleWakeup", "EnterWorktree", "Workflow")
# Hooks off; file tools and the built-in read-only Bash commands refuse paths outside the
# working directory in every mode.
WORKER_SETTINGS = {"disableAllHooks": True,
                   "permissions": {"blockReadsOutsideWorkingDirectories": True}}
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # claude --help, 2.1.290
# --browser: an exact, npm-verified server version, so the tool set below cannot change under us.
# Every tool it exposes (default caps, WebMCP off) is classified. Allowed: navigation and page
# interaction. Denied: code execution (run_code_unsafe runs JS in the server process), local file
# reads (file_upload, drop), and every tool with a `filename` argument: this version resolves an
# explicit filename against the MCP workspace root (claude advertises its cwd), not --output-dir,
# so it would write anywhere in the repo, including ignored files and .git. Page snapshots of
# navigation and input tools land in a<N>/browser/ and are read from there.
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"
BROWSER_ALLOW = ("browser_navigate", "browser_navigate_back", "browser_click", "browser_hover",
                 "browser_drag", "browser_type", "browser_press_key", "browser_select_option",
                 "browser_fill_form", "browser_handle_dialog", "browser_wait_for",
                 "browser_resize", "browser_emulate_media", "browser_tabs", "browser_close")
BROWSER_DENY = ("browser_run_code_unsafe", "browser_evaluate", "browser_file_upload",
                "browser_drop", "browser_snapshot", "browser_take_screenshot",
                "browser_console_messages", "browser_network_requests",
                "browser_network_request", "browser_find")
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request-changes", "inconclusive"]},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
                "title": {"type": "string"},
                "file": {"type": "string"},
                "line": {"type": ["integer", "null"]},
                "evidence": {"type": "string"},
                "recommendation": {"type": "string"},
            },
            "required": ["severity", "title", "file", "line", "evidence", "recommendation"],
        }},
        "verification": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "findings", "verification", "risks"],
}
CONTRACT = (
    "You are a non-interactive worker started by an orchestrator. Nobody can answer questions "
    "or approve actions; a denied action stays denied, do not retry it. Mode: {mode}. "
    "Working directory: {cwd}. {rule} If docs/memory/MEMORY.md exists, read it first and open "
    "relevant entries. Run shell commands plainly. Do not start background processes you would "
    "need to wait for. Do not commit, push, or write memory, logs, notes or report files. "
    "{deliverable}"
)
PROSE_DELIVERABLE = ("Your final message is the deliverable and is saved verbatim. Structure it: "
                     "Outcome; Evidence (file:line); Changes (write mode); Verification (commands "
                     "and results); Risks; Next.")
SCHEMA_DELIVERABLE = ("Return the deliverable through the structured output tool; put evidence "
                      "in its fields.")
REVIEW_RULE = "Do not modify any file, including via shell."
WRITE_RULE = ("You may modify only: {scopes}. Everything else is denied and a post-run diff check "
              "flags it; if the task needs a change outside scope, stop and say so in your final "
              "message.")

_signaled = 0


class CsaError(Exception):
    pass


# ---------------------------------------------------------------- utilities

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value):
    return datetime.fromisoformat(value).timestamp() if value else None


def emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    # ASCII-only: git paths are decoded with surrogateescape, and a lone surrogate (a non-UTF-8
    # file name) must become a \udcXX escape, not an encode error that wedges the run.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="ascii")
    os.replace(tmp, path)


def read_text(raw: str) -> str:
    """A user-supplied text file: UTF-8 (BOM stripped) or UTF-16 with a BOM."""
    data = Path(to_linux_path(raw)).read_bytes()
    try:
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return data.decode("utf-16")
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsaError(f"{raw} is not UTF-8 ({exc.reason} at byte {exc.start}); "
                       "save it as UTF-8") from None


def to_linux_path(raw: str) -> str:
    """Translate \\\\wsl.localhost\\<distro>\\..., //wsl.localhost/..., \\\\?\\UNC\\wsl.localhost\\...
    and C:\\... to Linux paths; anything else is returned unchanged."""
    s = raw.strip()
    fwd = s.replace("\\", "/")
    # A command string run via powershell.exe -Command reaches wsl.exe with each "\\" collapsed
    # to "\": accept one leading separator when the value was written with backslashes.
    lead = "/{1,2}" if s.startswith("\\") else "//"
    m = re.match("^" + lead + r"(?:\?/unc/)?(wsl\.localhost|wsl\$)/([^/]+)(/.*)?$", fwd, re.I)
    if m:
        here = os.environ.get("WSL_DISTRO_NAME")
        if here and m.group(2).lower() != here.lower():
            raise CsaError(f"{raw!r} is in WSL distro {m.group(2)!r}, but this is {here!r}")
        return posixpath.normpath(m.group(3) or "/")
    m = re.match(r"^([A-Za-z]):(/.*)?$", fwd)
    if m:
        return posixpath.normpath(f"/mnt/{m.group(1).lower()}{m.group(2) or '/'}")
    return s


def resolve_cwd(raw: str, allow_windows_fs: bool) -> Path:
    drive = re.match(r"^[A-Za-z]:", raw.strip()) is not None
    path = Path(to_linux_path(raw)).resolve()
    if (drive or re.match(r"^/mnt/[a-z](/|$)", str(path))) and not allow_windows_fs:
        raise CsaError(f"--cwd is on the Windows filesystem ({path}); use a repo under /home "
                       "or pass --allow-windows-fs")
    if not path.is_dir():
        raise CsaError(f"--cwd does not exist: {path}")
    return path


def find_claude() -> str:
    # Under wsl.exe -e, PATH carries Windows dirs (/mnt/c/...); a Windows npm shim there is not
    # the Linux CLI.
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                           if d and not d.startswith("/mnt/"))
    found = shutil.which("claude", path=path)
    fallback = Path.home() / ".local" / "bin" / "claude"
    if found:
        return found
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    raise CsaError("claude CLI not found on PATH or at ~/.local/bin/claude")


def claude_version(claude_bin: str) -> str:
    p = subprocess.run([claude_bin, "--version"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return (p.stdout or p.stderr).strip()


def worker_env() -> dict:
    env = dict(os.environ)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["CLAUDE_CODE_DISABLE_CRON"] = "1"  # no scheduled tasks outliving the run
    env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"  # no run_in_background processes
    return env


# ---------------------------------------------------------------------- git

def git(cwd, *args, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)
    if p.returncode != 0:
        if check:
            msg = p.stderr.decode("utf-8", "replace").strip()
            raise CsaError(f"git {' '.join(args)} failed in {cwd}: {msg}")
        return ""
    return p.stdout.decode("utf-8", "surrogateescape")


def repo_root(cwd: Path) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel").strip())


def ensure_excluded(cwd: Path) -> Path:
    """Add .agent-runs/ to <git-common-dir>/info/exclude; never touches .gitignore."""
    common = Path(git(cwd, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = (cwd / common).resolve()
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if any(ln.strip() in (".agent-runs/", ".agent-runs", "/.agent-runs/", "/.agent-runs")
           for ln in text.splitlines()):
        return exclude
    with exclude.open("a", encoding="utf-8") as fh:
        fh.write(("\n" if text and not text.endswith("\n") else "") + ".agent-runs/\n")
    return exclude


def _digest(path: Path) -> str:
    try:
        if path.is_symlink():
            return "l:" + os.readlink(path)
        if path.is_dir():
            return "d"
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return "-"
    except OSError as exc:
        return f"err:{exc.errno}"


def staged(root: Path) -> dict:
    """Index entries (mode, object id, stage) by path. Unlike `diff --cached --raw`, this also
    sees edits to conflicted entries (stages 1-3), which that reports with all-zero ids."""
    entries = {}
    for rec in git(root, "ls-files", "-s", "-z").split("\0"):  # "<mode> <oid> <stage>\t<path>"
        if "\t" in rec:
            meta, path = rec.split("\t", 1)
            entries.setdefault(path, []).append(meta.replace(" ", ","))
    return {path: ";".join(metas) for path, metas in entries.items()}


def gitlinks(root: Path) -> list:
    """Submodule paths (mode 160000 index entries)."""
    return [rec.split("\t", 1)[1] for rec in git(root, "ls-files", "-s", "-z").split("\0")
            if rec.startswith("160000 ")]


def snapshot(root: Path) -> dict:
    """HEAD plus every dirty/untracked path with its status code, content hash and, when staged,
    its index object id (a change to the index alone keeps the status code and file content)."""
    head = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip() or None
    raw = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    index = staged(root)
    parts, entries, i = raw.split("\0"), {}, 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if len(item) < 4:
            continue
        xy, paths = item[:2], [item[3:]]
        if "R" in xy or "C" in xy:  # rename/copy: "XY new\0old\0"
            if i < len(parts):
                paths.append(parts[i])
            i += 1
        for p in paths:
            entries[p] = f"{xy}:{_digest(root / p)}:{index.get(p, '-')}"
    return {"head": head, "entries": entries}


def changed_paths(root: Path, before: dict, after: dict) -> list:
    b, a = before.get("entries", {}), after.get("entries", {})
    changed = {p for p in set(b) | set(a) if b.get(p) != a.get(p)}
    if after.get("head") and before.get("head") != after.get("head"):
        if before.get("head"):
            out = git(root, "diff", "--name-only", "-z", before["head"], after["head"], check=False)
        else:
            out = git(root, "ls-tree", "-r", "-z", "--name-only", after["head"], check=False)
        changed.update(x for x in out.split("\0") if x)
    return sorted(p for p in changed if p != ".agent-runs" and not p.startswith(".agent-runs/"))


def in_scope(path: str, scopes) -> bool:
    return any(path == s or path.startswith(s + "/") for s in scopes)


# --------------------------------------------------------- locks + liveness

def try_lock(path: Path, attempts: int = 1):
    """Exclusive non-blocking flock; returns the fd or None. Held by the launcher for the run."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    for i in range(attempts):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if i + 1 < attempts:
                time.sleep(0.1)
    os.close(fd)
    return None


def _proc_stat(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return fields[0].decode(), int(fields[19])  # state, starttime (clock ticks)
    except (OSError, IndexError, ValueError):
        return None


def leader_alive(att: dict) -> bool:
    pid = att.get("pid")
    st = _proc_stat(pid) if pid else None
    if not st or st[0] == "Z":
        return False
    return not att.get("start_ticks") or st[1] == att["start_ticks"]


def killpg(pgid, sig) -> None:
    if not pgid:
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def group_alive(pgid) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def drain_group(pgid) -> bool:
    """SIGTERM everything left in the process group, SIGKILL it after HARD_KILL_S, and wait (up
    to KILL_WAIT_S) for the group to be empty. Blocking. True once it is empty."""
    for sig, wait in ((signal.SIGTERM, HARD_KILL_S), (signal.SIGKILL, KILL_WAIT_S)):
        if not group_alive(pgid):
            return True
        killpg(pgid, sig)
        end = time.time() + wait
        while time.time() < end and group_alive(pgid):
            time.sleep(0.1)
    return not group_alive(pgid)


def reap_group(att: dict) -> bool:
    """Drain the worker's process group: the worker if it outlived SIGINT, and every child it
    left, including ones that ignore SIGTERM. Call before the final snapshot and before the lock
    is released. Skipped if the leader's pid now belongs to another process: its group id may
    have been reused. False if the group still has members."""
    st = _proc_stat(att["pid"]) if att.get("pid") else None
    if st is None or st[1] == att.get("start_ticks"):
        return drain_group(att.get("pgid"))
    return True


def stop_group(att: dict) -> None:
    """SIGINT (lets claude end the turn) and wait for the leader; reap_group does the rest."""
    killpg(att.get("pgid"), signal.SIGINT)
    end = time.time() + KILL_GRACE_S
    while time.time() < end and leader_alive(att):
        time.sleep(0.2)


# ------------------------------------------------------------------ results

def parse_stream(path: Path) -> dict:
    init, result, denied = None, None, []
    if path.exists():
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue  # partial last line of a killed run
                if not isinstance(ev, dict):
                    continue
                kind, sub = ev.get("type"), ev.get("subtype")
                if kind == "system" and sub == "init" and init is None:
                    init = ev
                elif kind == "system" and sub == "permission_denied":
                    denied.append({"tool_name": ev.get("tool_name"), "tool_input": ev.get("message")})
                elif kind == "result":
                    result = ev  # the last result event is the final one
    if result is not None and isinstance(result.get("permission_denials"), list):
        denied = result["permission_denials"]
    denials = [{"tool": d.get("tool_name"),
                "input": (d.get("tool_input") if isinstance(d.get("tool_input"), str)
                          else json.dumps(d.get("tool_input"), ensure_ascii=False))[:200]}
               for d in denied if isinstance(d, dict)]
    return {"init": init, "result": result, "denials": denials}


def classify(exit_code, result, out_of_scope, reason=None, launcher_lost=False) -> str:
    if out_of_scope:
        return "scope-violation"
    if reason in ("cancelled", "timeout", "interrupted"):
        return reason
    if out_of_scope is None:
        return "failed"  # the scope check could not run; never report success blind
    if result is None:
        killed = exit_code is None or exit_code < 0
        return "interrupted" if (launcher_lost or killed) else "failed"
    if exit_code not in (0, None):
        return "failed"
    if result.get("subtype") == "success" and not result.get("is_error"):
        return "complete"
    return "failed"


def result_text(result: dict) -> str:
    text = result.get("result") if isinstance(result.get("result"), str) else ""
    if not text.strip() and result.get("structured_output") is not None:
        text = json.dumps(result["structured_output"], indent=2, ensure_ascii=False)
    if not text.strip() and result.get("errors"):
        text = "\n".join(str(e) for e in result["errors"])
    return text


# --------------------------------------------------------------- run state

def runs_dir_for(cwd: Path) -> Path:
    return repo_root(cwd) / ".agent-runs" / "claude"


def task_dir_for(cwd: Path, task: str) -> Path:
    if not TASK_RE.match(task or ""):
        raise CsaError(f"invalid task id {task!r}; must match {TASK_RE.pattern}")
    return runs_dir_for(cwd) / task


def load_run(task_dir: Path):
    path = task_dir / "run.json"
    return read_json(path) if path.exists() else None


def save_run(task_dir: Path, run: dict) -> None:
    write_json(task_dir / "run.json", run)


def contract(run: dict) -> str:
    rule = REVIEW_RULE if run["mode"] == "review" else WRITE_RULE.format(
        scopes=", ".join(s + "/" if (Path(run["workdir"]) / s).is_dir() else s for s in run["scopes"]))
    deliverable = SCHEMA_DELIVERABLE if run.get("json_schema") is not None else PROSE_DELIVERABLE
    return CONTRACT.format(mode=run["mode"], cwd=run["workdir"], rule=rule, deliverable=deliverable)


def build_cmd(run: dict, att: dict, claude_bin: str, att_dir: Path) -> list:
    caps = run["caps"]
    session = ["--session-id", run["session_id"]] if att["kind"] == "start" else ["--resume", run["session_id"]]
    cmd = [claude_bin, "-p", "--output-format", "stream-json", "--verbose", *session,
           "--name", f"csa-{run['task']}",
           "--append-system-prompt", contract(run),
           "--permission-mode", "dontAsk",
           "--max-turns", str(caps["max_turns"]),
           "--max-budget-usd", f"{caps['budget_usd']:g}",
           "--strict-mcp-config",
           "--settings", json.dumps(WORKER_SETTINGS, separators=(",", ":"))]
    if run.get("model_requested"):
        cmd += ["--model", run["model_requested"]]
    if run.get("effort_requested"):
        cmd += ["--effort", run["effort_requested"]]
    if run.get("json_schema") is not None:
        cmd += ["--json-schema", json.dumps(run["json_schema"], separators=(",", ":"))]
    allow = [f"Bash({c}:*)" for c in READONLY_BASH] + [f"Bash({p}:*)" for p in run["allow_bash"]]
    if run["mode"] == "write":
        for s in run["scopes"]:
            allow += [f"Edit(/{s})", f"Edit(/{s}/**)"]  # '/x' anchors at the working directory
    deny = [*ALWAYS_DENY, *EXEC_FLAG_DENY, *(REVIEW_DENY if run["mode"] == "review" else ())]
    variadic = []
    if run["browser"]:
        cmd += ["--mcp-config", str(att_dir / "mcp.json")]
        allow += [f"mcp__playwright__{t}" for t in BROWSER_ALLOW]
        deny += [f"mcp__playwright__{t}" for t in BROWSER_DENY]
        variadic = ["--add-dir", str(att_dir / "browser")]  # page snapshots, even from a worktree
    # Variadic flags last, each with its values grouped; the prompt goes on stdin.
    return cmd + variadic + ["--allowedTools", *allow, "--disallowedTools", *deny]


def playwright_chromium():
    """Newest Chromium bundled with the global Playwright install, if any (CSA_BROWSER overrides).
    Preferred over the system Chrome, which may be missing or broken under WSL."""
    explicit = os.environ.get("CSA_BROWSER")
    if explicit:
        return explicit
    found = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
                   key=lambda p: int(re.sub(r"\D", "", p.parts[-3]) or 0))
    return str(found[-1]) if found else None


def mcp_config(att_dir: Path) -> dict:
    args = ["-y", PLAYWRIGHT_MCP, "--headless", "--isolated", "--no-webmcp",
            "--output-dir", str(att_dir / "browser")]
    browser = playwright_chromium()
    if browser:
        args += ["--executable-path", browser]
    return {"mcpServers": {"playwright": {"command": "npx", "args": args}}}


def finalize(task_dir: Path, run: dict, exit_code, reason=None, launcher_lost=False) -> None:
    """Write the attempt's terminal state. The run always leaves 'running': evidence that cannot
    be read or recorded fails the attempt with the error instead of wedging it."""
    att = run["attempts"][-1]
    try:
        record_outcome(task_dir, run, att, exit_code, reason, launcher_lost)
    except Exception as exc:  # noqa: BLE001 - see docstring
        att.update(finished_at=now_iso(), exit_code=exit_code, status="failed",
                   error=f"finalize failed: {type(exc).__name__}: {exc}")
        run["status"] = "failed"
    if launcher_lost:
        att["launcher_lost"] = True
    save_run(task_dir, run)


def record_outcome(task_dir: Path, run: dict, att: dict, exit_code, reason, launcher_lost) -> None:
    att_dir = task_dir / f"a{att['n']}"
    parsed = parse_stream(att_dir / "stream.jsonl")
    res, init = parsed["result"], parsed["init"] or {}
    changes_path = att_dir / "changes.json"
    changes, out_of_scope = {}, None
    try:
        changes = read_json(changes_path) if changes_path.exists() else {}
        workdir = Path(run["workdir"])
        root = repo_root(workdir)
        changed = changed_paths(root, changes.get("baseline", {}), snapshot(root))
        rel = os.path.relpath(workdir, root)
        scopes = [posixpath.normpath(posixpath.join(rel, s)) if rel != "." else s for s in run["scopes"]]
        out_of_scope = changed if run["mode"] == "review" else [p for p in changed if not in_scope(p, scopes)]
        changes.update(mode=run["mode"], scopes=scopes, changed=changed, out_of_scope=out_of_scope)
    except Exception as exc:  # noqa: BLE001 - no scope verdict means the run cannot be complete
        changes["error"] = f"scope check failed: {type(exc).__name__}: {exc}"
    write_json(changes_path, changes)
    if res is not None:
        write_json(att_dir / "result.json", res)
        (att_dir / "result.md").write_text(result_text(res), encoding="utf-8")
    status = classify(exit_code, res, out_of_scope, reason, launcher_lost)
    res = res or {}
    att.update(finished_at=now_iso(), exit_code=exit_code, status=status,
               result_subtype=res.get("subtype"), is_error=res.get("is_error"),
               num_turns=res.get("num_turns"), cost_usd=res.get("total_cost_usd"),
               usage=res.get("usage"), model_resolved=init.get("model"),
               permission_mode=init.get("permissionMode"), denials=parsed["denials"][:20],
               out_of_scope=out_of_scope)
    if "error" in changes:
        att.setdefault("error", changes["error"])
    run["status"] = status


def reconcile(task_dir: Path):
    """Bring a 'running' run.json in line with reality when its launcher is gone."""
    run = load_run(task_dir)
    if not run or run.get("status") != "running":
        return run
    fd = try_lock(task_dir / "active.lock")
    if fd is None:
        return run  # launcher alive and supervising
    try:
        run = load_run(task_dir)
        if run.get("status") != "running":
            return run
        att = run["attempts"][-1]
        cancel = (task_dir / f"a{att['n']}" / "cancel").exists()
        reason = "cancelled" if cancel else None
        if leader_alive(att):
            started = parse_iso(att.get("started_at")) or time.time()
            overdue = time.time() - started > run["caps"]["timeout_min"] * 60
            if not (cancel or overdue):
                return run  # orphaned worker still working; launcher died
            stop_group(att)
            reason = reason or "timeout"
        if not reap_group(att):  # whatever a lost launcher never cleaned up
            att["error"] = f"process group {att.get('pgid')} not empty after SIGKILL"
        finalize(task_dir, run, None, reason, launcher_lost=True)
        return run
    finally:
        os.close(fd)


def summary(task_dir: Path, run: dict) -> dict:
    att = run["attempts"][-1]
    att_dir = task_dir / f"a{att['n']}"
    started = parse_iso(att.get("started_at"))
    end = parse_iso(att.get("finished_at")) or time.time()
    out = {"task": run["task"], "status": run["status"], "mode": run["mode"], "attempt": att["n"],
           "kind": att["kind"], "started_at": att.get("started_at"),
           "finished_at": att.get("finished_at"),
           "elapsed_s": round(end - started) if started else None,
           "pid": att.get("pid"), "pgid": att.get("pgid"), "exit_code": att.get("exit_code"),
           "result_subtype": att.get("result_subtype"), "is_error": att.get("is_error"),
           "num_turns": att.get("num_turns"), "cost_usd": att.get("cost_usd"),
           "model_resolved": att.get("model_resolved"),
           "denials": len(att.get("denials") or []), "out_of_scope": att.get("out_of_scope"),
           "workdir": run["workdir"], "run_dir": str(task_dir), "attempt_dir": str(att_dir)}
    if att.get("error"):
        out["error"] = att["error"]
    if att.get("launcher_lost"):
        out["launcher_lost"] = True
    if run["status"] == "running":
        stream = att_dir / "stream.jsonl"
        st = stream.stat() if stream.exists() else None
        out.update(worker_alive=leader_alive(att), stream_bytes=st.st_size if st else 0,
                   idle_s=round(time.time() - st.st_mtime) if st else None)
    return out


# ------------------------------------------------------------- the launcher

def _on_signal(signum, _frame):
    global _signaled
    _signaled = signum


def supervise(proc, att: dict, att_dir: Path, deadline: float):
    """Wait for claude; enforce timeout, cancel marker and launcher signals. -> (rc, reason)"""
    reason, stage, t_stop = None, 0, 0.0
    while True:
        try:
            return proc.wait(timeout=0.25), reason
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if reason is None:
            if _signaled:
                reason = "interrupted"
            elif (att_dir / "cancel").exists():
                reason = "cancelled"
            elif now >= deadline:
                reason = "timeout"
            if reason:
                killpg(att["pgid"], signal.SIGINT)
                stage, t_stop = 1, now
        elif stage == 1 and now - t_stop >= KILL_GRACE_S:
            killpg(att["pgid"], signal.SIGTERM)
            stage = 2
        elif stage == 2 and now - t_stop >= KILL_GRACE_S + HARD_KILL_S:
            killpg(att["pgid"], signal.SIGKILL)
            stage = 3


def run_attempt(task_dir: Path, run: dict, report_fd=None) -> None:
    """Spawn claude for the latest attempt, supervise it, write the final state."""
    def report(obj):
        """Best effort: the initiating parent may be gone (BrokenPipeError); the worker it
        reports on is still ours to supervise."""
        nonlocal report_fd
        if report_fd is not None:
            try:
                os.write(report_fd, json.dumps(obj).encode())
            except OSError as exc:
                try:
                    print(f"spawn report not delivered: {exc}", file=sys.stderr, flush=True)
                except OSError:
                    pass  # e.g. launcher.log unwritable: never let a diagnostic skip supervision
            finally:
                os.close(report_fd)
                report_fd = None

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    att = run["attempts"][-1]
    att_dir = task_dir / f"a{att['n']}"
    try:
        with open(att_dir / "prompt.md", "rb") as stdin, open(att_dir / "stream.jsonl", "xb") as out, \
                open(att_dir / "stderr.log", "xb") as err:
            proc = subprocess.Popen(att["argv"], cwd=run["workdir"], stdin=stdin, stdout=out,
                                    stderr=err, env=worker_env(), start_new_session=True)
    except OSError as exc:
        att["error"] = f"spawn failed: {exc}"
        finalize(task_dir, run, 127)
        report({"error": att["error"], "task": run["task"], "status": run["status"]})
        return
    st = _proc_stat(proc.pid)
    att.update(pid=proc.pid, pgid=os.getpgid(proc.pid), start_ticks=st[1] if st else None,
               launcher_pid=os.getpid())
    try:
        save_run(task_dir, run)
    except OSError as exc:  # the worker runs either way: keep supervising it
        att["error"] = f"could not record the worker pid: {exc}"
    report({"task": run["task"], "attempt": att["n"], "status": "running", "pid": att["pid"],
            "pgid": att["pgid"], "launcher_pid": att["launcher_pid"], "run_dir": str(task_dir)})
    rc, reason = supervise(proc, att, att_dir, time.time() + run["caps"]["timeout_min"] * 60)
    if not reap_group(att):  # leftover children of the worker, before the final snapshot
        att["error"] = f"process group {att['pgid']} not empty after SIGKILL"
    finalize(task_dir, run, rc, reason)


def launch(task_dir: Path, run: dict, lock_fd: int, detach: bool) -> int:
    if not detach:
        run_attempt(task_dir, run)
        emit(summary(task_dir, run))
        return 0
    rfd, wfd = os.pipe()
    if os.fork() > 0:  # parent: wait for the grandchild's spawn report, then return
        os.close(wfd)
        os.close(lock_fd)  # the grandchild keeps its inherited copy
        chunks = []
        while True:
            chunk = os.read(rfd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(rfd)
        os.wait()
        try:
            info = json.loads(b"".join(chunks) or b"{}")
        except ValueError:
            info = {}
        if not info:
            info = {"error": "launcher exited before reporting; see launcher.log",
                    "run_dir": str(task_dir)}
        emit(info)
        return 1 if "error" in info else 0
    os.close(rfd)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    att_dir = task_dir / f"a{run['attempts'][-1]['n']}"
    try:
        null = os.open(os.devnull, os.O_RDWR)
        log = os.open(att_dir / "launcher.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        os.dup2(null, 0)
        os.dup2(log, 1)
        os.dup2(log, 2)
        run_attempt(task_dir, run, report_fd=wfd)
    except BaseException:  # noqa: BLE001 - last-resort log for a daemon with no terminal
        traceback.print_exc()
    finally:
        os._exit(0)


# -------------------------------------------------------------------- verbs

def read_prompt(raw: str) -> str:
    text = sys.stdin.read().lstrip("\ufeff") if raw == "-" else read_text(raw)
    if not text.strip():
        raise CsaError("prompt is empty")
    return text


def normalize_scope(raw: str, workdir: Path) -> str:
    s = raw.strip().replace("\\", "/")
    if not s or s.startswith(("/", "-")) or re.search(r"[\s,()*?\[\]\"'`$]", s):
        raise CsaError(f"invalid --scope {raw!r}: use a plain path relative to --cwd")
    n = posixpath.normpath(s)
    if n in (".", "..") or n.startswith("../") or n.split("/")[0] in (".git", ".agent-runs"):
        raise CsaError(f"--scope {raw!r} must name a path inside --cwd (not ., .git or .agent-runs)")
    real, base = os.path.realpath(workdir / n), os.path.realpath(workdir)
    if real != base and not real.startswith(base + os.sep):
        raise CsaError(f"--scope {raw!r} resolves outside --cwd")
    return n


def normalize_prefix(raw: str) -> str:
    s = " ".join(raw.split())
    if not s or s.startswith("-") or re.search(r"[()]", s):
        raise CsaError(f"invalid --allow-bash {raw!r}: give a command prefix such as 'pytest'")
    return s


def check_caps(caps: dict) -> dict:
    # nan and inf pass a plain > 0 test and would disable the timeout (now >= nan is never true)
    if caps["max_turns"] < 1 or not all(math.isfinite(caps[k]) and caps[k] > 0
                                        for k in ("budget_usd", "timeout_min")):
        raise CsaError("--max-turns, --budget-usd and --timeout-min must be positive and finite")
    return caps


def lock_held(task_dir: Path) -> bool:
    fd = try_lock(task_dir / "active.lock")
    if fd is None:
        return True
    os.close(fd)
    return False


def tree_conflict(runs_dir: Path, skip: str, mode: str):
    """An active run in the repo's main working tree whose file changes the post-run diff could
    not tell apart from this run's: any other run when this one writes, a writer when it reviews.
    Call with the start lock held."""
    # Every task dir, not only those with a run.json: a held lock means a live launcher whatever
    # state its run.json is in (missing, null, {}, malformed).
    task_dirs = sorted(p for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.exists() else []
    for d in task_dirs:
        if d.name == skip:
            continue
        try:
            run = reconcile(d)
            problem = None if isinstance(run, dict) and "status" in run else "has no usable state"
        except Exception as exc:  # noqa: BLE001 - unreadable state blocks only while its lock is held
            run, problem = None, f"cannot be read ({type(exc).__name__})"
        if problem:
            if (d / "active.lock").exists() and lock_held(d):  # unknown mode and tree: assume a writer
                return (f"run {d.name!r} is active (its lock is held) but its run.json {problem}; "
                        "its mode and tree are unknown. Wait for it (status --wait)")
            continue
        if run.get("status") == "running" and not run.get("worktree") and \
                "write" in (mode, run.get("mode")):
            return (f"{run.get('mode')} run {run.get('task')!r} is active in the main working "
                    "tree; the post-run diff could not tell its changes from this run's. Wait "
                    "for it (status --wait)")
    return None


def start_lock(runs_dir: Path) -> int:
    """Serializes the tree-conflict check with run registration across start and resume."""
    fd = os.open(runs_dir / ".start.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def new_attempt(task_dir: Path, run: dict, kind: str, prompt: str, claude_bin: str) -> dict:
    version = claude_version(claude_bin)
    baseline = snapshot(repo_root(Path(run["workdir"])))
    n = len(run["attempts"]) + 1
    att_dir = task_dir / f"a{n}"
    att_dir.mkdir(exist_ok=True)  # may hold leftovers of a launch that died before run.json
    (att_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    if run["browser"]:
        write_json(att_dir / "mcp.json", mcp_config(att_dir))
        (att_dir / "browser").mkdir(exist_ok=True)  # --add-dir needs it to exist
    write_json(att_dir / "changes.json", {"baseline": baseline})
    att = {"n": n, "kind": kind, "started_at": now_iso(), "finished_at": None, "pid": None,
           "pgid": None, "exit_code": None, "status": "running", "result_subtype": None,
           "is_error": None, "num_turns": None, "cost_usd": None, "usage": None,
           "model_resolved": None, "permission_mode": None, "denials": [], "out_of_scope": None,
           "allow_bash": list(run["allow_bash"]), "browser": run["browser"]}
    run["attempts"].append(att)
    att["argv"] = build_cmd(run, att, claude_bin, att_dir)
    run["status"] = "running"
    run["claude_bin"], run["claude_version"] = claude_bin, version
    save_run(task_dir, run)
    return att


def cmd_start(args) -> int:
    prompt = read_prompt(args.prompt)
    cwd = resolve_cwd(args.cwd, args.allow_windows_fs)
    task_dir = task_dir_for(cwd, args.task)
    if args.mode == "write" and not args.scope:
        raise CsaError("--mode write requires at least one --scope")
    if args.mode == "review" and args.scope:
        raise CsaError("--scope only applies to --mode write")
    scopes = [normalize_scope(s, cwd) for s in args.scope]
    allow_bash = [normalize_prefix(p) for p in args.allow_bash]
    caps = check_caps({"max_turns": args.max_turns, "budget_usd": args.budget_usd,
                       "timeout_min": args.timeout_min})
    schema = REVIEW_SCHEMA if args.mode == "review" else None
    if args.schema == "none":
        schema = None
    elif args.schema:
        schema = json.loads(read_text(args.schema))
        if not isinstance(schema, dict):
            raise CsaError("--schema must contain a JSON object")
    claude_bin = find_claude()
    root = repo_root(cwd)
    rel = os.path.relpath(cwd, root)
    subs = gitlinks(root)
    for s in scopes:  # git reports a submodule only as its own path, never what changed inside
        p = posixpath.normpath(posixpath.join(rel, s))
        sub = next((g for g in subs if p.startswith(g + "/")), None)
        if sub:
            raise CsaError(f"--scope {s!r} is inside submodule {sub!r}, which the scope check sees "
                           "only as a whole: scope the submodule path, or run with --cwd inside it")
    ensure_excluded(cwd)
    runs_dir = root / ".agent-runs" / "claude"
    if task_dir.exists() and not (task_dir / "run.json").exists() and \
            any(p.is_file() and p.name != "active.lock" for p in task_dir.iterdir()):
        raise CsaError(f"{task_dir} holds v1 run files; pick another task id")
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = try_lock(task_dir / "active.lock", attempts=20)
    if lock_fd is None:
        raise CsaError(f"task {args.task!r} is active (its lock is held)")
    start_fd = start_lock(runs_dir)
    try:
        if (task_dir / "run.json").exists():
            raise CsaError(f"task {args.task!r} already exists; use resume, or pick a new task id")
        workdir, worktree = cwd, None
        if not args.worktree:
            conflict = tree_conflict(runs_dir, args.task, args.mode)
            if conflict:
                raise CsaError(conflict + " or start this run with --worktree")
        else:
            wt = root / ".agent-runs" / "wt" / args.task
            git(root, "worktree", "add", "-q", str(wt), "-b", f"csa/{args.task}")
            worktree = str(wt)
            workdir = wt / rel if rel != "." else wt
        run = {"schema": 1, "task": args.task, "cwd": str(cwd), "worktree": worktree,
               "workdir": str(workdir), "mode": args.mode, "scopes": scopes,
               "allow_bash": allow_bash, "browser": args.browser, "session_id": str(uuid.uuid4()),
               "claude_bin": claude_bin, "claude_version": None,
               "model_requested": args.model, "effort_requested": args.effort, "caps": caps,
               "json_schema": schema, "status": "running", "attempts": []}
        try:
            new_attempt(task_dir, run, "start", prompt, claude_bin)
        except BaseException:
            if worktree:  # an unregistered worktree + branch would block this task id for good
                git(root, "worktree", "remove", "--force", worktree, check=False)
                git(root, "branch", "-D", f"csa/{args.task}", check=False)
            raise
    finally:
        os.close(start_fd)
    return launch(task_dir, run, lock_fd, args.detach)


def cmd_resume(args) -> int:
    prompt = read_prompt(args.prompt)
    cwd = resolve_cwd(args.cwd, args.allow_windows_fs)
    task_dir = task_dir_for(cwd, args.task)
    extra_bash = [normalize_prefix(p) for p in args.allow_bash]
    if load_run(task_dir) is None:
        raise CsaError(f"no run for task {args.task!r}")
    run = reconcile(task_dir)
    if run["status"] not in TERMINAL:
        raise CsaError(f"task {args.task!r} is {run['status']}; resume only terminal tasks")
    lock_fd = try_lock(task_dir / "active.lock", attempts=20)
    if lock_fd is None:
        raise CsaError(f"task {args.task!r} is active (its lock is held)")
    run = load_run(task_dir)
    if run["status"] not in TERMINAL:
        raise CsaError(f"task {args.task!r} is {run['status']}; resume only terminal tasks")
    for key, val in (("max_turns", args.max_turns), ("budget_usd", args.budget_usd),
                     ("timeout_min", args.timeout_min)):
        if val is not None:
            run["caps"][key] = val
    check_caps(run["caps"])
    if args.model:
        run["model_requested"] = args.model
    if args.effort:
        run["effort_requested"] = args.effort
    # Added for this and later attempts (like caps); each attempt records what it ran with.
    for p in extra_bash:
        if p not in run["allow_bash"]:
            run["allow_bash"].append(p)
    run["browser"] = run["browser"] or args.browser
    claude_bin = find_claude()
    start_fd = start_lock(task_dir.parent)
    try:
        conflict = None if run["worktree"] else tree_conflict(task_dir.parent, args.task, run["mode"])
        if conflict:
            raise CsaError(conflict)
        new_attempt(task_dir, run, "resume", prompt, claude_bin)
    finally:
        os.close(start_fd)
    return launch(task_dir, run, lock_fd, args.detach)


def all_tasks(runs_dir: Path) -> list:
    if not runs_dir.exists():
        return []
    return [d for d in sorted(runs_dir.iterdir()) if (d / "run.json").exists()]


def legacy_note(runs_dir: Path):
    ledger = runs_dir / "ledger.json"
    return str(ledger) + " (v1 ledger, ignored)" if ledger.exists() else None


def task_row(task_dir: Path) -> dict:
    """Reconcile and summarize one task. A task whose state cannot be read is reported as
    'unknown' with the error; it never takes status or list down for the other tasks."""
    try:
        return summary(task_dir, reconcile(task_dir))
    except Exception as exc:  # noqa: BLE001 - see docstring
        try:
            held = lock_held(task_dir)
        except OSError:
            held = None
        return {"task": task_dir.name, "status": "unknown", "lock_held": held,
                "error": f"{type(exc).__name__}: {exc}", "run_dir": str(task_dir)}


def is_active(row: dict) -> bool:
    """Running, or unreadable while a launcher still holds its lock."""
    return row["status"] == "running" or bool(row.get("lock_held"))


def print_table(rows: list, legacy) -> None:
    for r in rows:
        if r["status"] == "unknown":
            print(f"{r['task']:<30} {r['status']:<15} {r['error']}")
            continue
        cost = f"${r['cost_usd']:.2f}" if isinstance(r.get("cost_usd"), (int, float)) else "-"
        extra = f" idle={r['idle_s']}s" if r.get("idle_s") is not None else ""
        print(f"{r['task']:<30} {r['status']:<15} {r['mode']:<6} a{r['attempt']} "
              f"{r['elapsed_s'] or 0:>6}s {cost:>7} turns={r.get('num_turns') or '-'}{extra}")
    if not rows:
        print("no runs")
    if legacy:
        print(f"note: {legacy}")


def cmd_status(args) -> int:
    cwd = resolve_cwd(args.cwd, args.allow_windows_fs)
    runs_dir = runs_dir_for(cwd)
    if args.task and not (task_dir_for(cwd, args.task) / "run.json").exists():
        raise CsaError(f"no run for task {args.task!r}")

    def poll():
        dirs = [task_dir_for(cwd, args.task)] if args.task else all_tasks(runs_dir)
        return [task_row(d) for d in dirs]

    def state(rows):
        return {r["task"]: (r["status"], r.get("attempt")) for r in rows}

    rows = poll()
    before = state(rows)
    t0 = time.time()
    while args.wait > 0 and time.time() - t0 < args.wait and any(is_active(r) for r in rows):
        time.sleep(min(2.0, max(0.0, args.wait - (time.time() - t0))))
        rows = poll()
        if state(rows) != before:
            break
    transitions = [{"task": t, "from": before.get(t, (None,))[0], "to": s[0]}
                   for t, s in state(rows).items() if before.get(t) != s]
    if args.json:
        emit({"tasks": rows, "active": sum(is_active(r) for r in rows),
              "transitions": transitions, "waited_s": round(time.time() - t0, 1),
              "legacy": legacy_note(runs_dir)})
    else:
        print_table(rows, legacy_note(runs_dir))
    return 0


def cmd_list(args) -> int:
    cwd = resolve_cwd(args.cwd, args.allow_windows_fs)
    runs_dir = runs_dir_for(cwd)
    rows = [task_row(d) for d in all_tasks(runs_dir)]
    if args.active:
        rows = [r for r in rows if is_active(r)]
    if args.json:
        emit({"tasks": rows, "legacy": legacy_note(runs_dir)})
    else:
        print_table(rows, legacy_note(runs_dir))
    return 0


def cmd_result(args) -> int:
    cwd = resolve_cwd(args.cwd, args.allow_windows_fs)
    task_dir = task_dir_for(cwd, args.task)
    if load_run(task_dir) is None:
        raise CsaError(f"no run for task {args.task!r}")
    run = reconcile(task_dir)
    info = summary(task_dir, run)
    att_dir = Path(info["attempt_dir"])
    res = read_json(att_dir / "result.json") if (att_dir / "result.json").exists() else None
    text = (att_dir / "result.md").read_text(encoding="utf-8") if (att_dir / "result.md").exists() else ""
    if args.json:
        info.update(result=text or None, structured_output=(res or {}).get("structured_output"),
                    terminal_reason=(res or {}).get("terminal_reason"),
                    denial_details=run["attempts"][-1].get("denials"))
        emit(info)
    else:
        if (res or {}).get("structured_output") is not None:
            text = json.dumps(res["structured_output"], indent=2, ensure_ascii=False)
        sys.stdout.write(f"[{run['task']} a{info['attempt']}: {run['status']}]\n{text}\n")
    return 0


def cmd_cancel(args) -> int:
    cwd = resolve_cwd(args.cwd, args.allow_windows_fs)
    task_dir = task_dir_for(cwd, args.task)
    if load_run(task_dir) is None:
        raise CsaError(f"no run for task {args.task!r}")
    run = reconcile(task_dir)
    if run["status"] in TERMINAL:
        emit({**summary(task_dir, run), "note": "already terminal"})
        return 0
    n = run["attempts"][-1]["n"]  # cancel acts on this attempt only, never on a later resume
    (task_dir / f"a{n}" / "cancel").write_text(now_iso() + "\n", encoding="utf-8")

    def marked(run):
        att = run["attempts"][-1]
        return att if att["n"] == n and run["status"] == "running" else None

    deadline = time.time() + KILL_GRACE_S + HARD_KILL_S + KILL_WAIT_S + 20
    while marked(run) and time.time() < deadline:  # the launcher (or reconcile) does the stopping
        time.sleep(0.5)
        run = reconcile(task_dir)
    if marked(run):
        run = reconcile(task_dir)  # re-check the attempt right before the only signal sent here
        att = marked(run)
        if att and leader_alive(att):
            killpg(att.get("pgid"), signal.SIGKILL)
            time.sleep(1)
            run = reconcile(task_dir)
    out = summary(task_dir, run)
    if run["attempts"][-1]["n"] != n:
        out["note"] = f"attempt {n} ended; attempt {out['attempt']} is newer and was not signalled"
    emit(out)
    return 0


def cmd_doctor(_args) -> int:
    checks, flags = [], {}

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    check("platform is Linux/WSL", sys.platform.startswith("linux"), sys.platform)
    check("git on PATH", shutil.which("git"), shutil.which("git") or "missing")
    check("npx on PATH (only for --browser)", True, shutil.which("npx") or "missing: --browser will fail")
    try:
        claude_bin = find_claude()
    except CsaError as exc:
        check("claude CLI found", False, str(exc))
        emit({"ok": False, "csa_version": VERSION, "checks": checks})
        return 1
    version = claude_version(claude_bin)
    check("claude CLI found", True, f"{claude_bin} ({version})")
    help_text = subprocess.run([claude_bin, "--help"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60).stdout
    help_flags = set(re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)", help_text))
    for flag, want in (("--permission-mode", "dontAsk"), ("--output-format", "stream-json")):
        m = re.search(re.escape(flag) + r" <\w+>.*?\(choices: ([^)]*)\)", help_text, re.S)
        choices = " ".join(m.group(1).split()) if m else "no choices listed"
        check(f"{flag} accepts {want}", f'"{want}"' in choices, choices)
    tmp = Path(tempfile.mkdtemp(prefix="csa-doctor-"))
    try:
        base = {"task": "doctor", "workdir": str(tmp), "allow_bash": [], "browser": False,
                "session_id": str(uuid.uuid4()), "caps": dict(DEFAULT_CAPS),
                "model_requested": None, "effort_requested": None}
        variants = {
            "review": {**base, "mode": "review", "scopes": [], "json_schema": REVIEW_SCHEMA},
            "write": {**base, "mode": "write", "scopes": ["src"], "allow_bash": ["pytest"],
                      "json_schema": None, "model_requested": "sonnet", "effort_requested": "high"},
            "browser": {**base, "mode": "review", "scopes": [], "json_schema": None, "browser": True},
        }
        cmds = {}
        for name, run in variants.items():
            cmds[name] = build_cmd(run, {"kind": "start"}, claude_bin, tmp)
        cmds["resume"] = build_cmd(variants["review"], {"kind": "resume"}, claude_bin, tmp)
        cmds["control"] = [claude_bin, "-p", "--csa-doctor-bogus-flag"]  # must be rejected
        write_json(tmp / "mcp.json", mcp_config(tmp))
        (tmp / "browser").mkdir()
        for name, cmd in cmds.items():
            for tok in cmd[1:] if name != "control" else []:
                if tok.startswith("-"):
                    flags[tok] = "in --help" if tok in help_flags else "not in --help; see probes"
        # No API call: with empty stdin claude parses the flags and option values (choices,
        # numbers, JSON schema, settings JSON), then stops before the first turn. Some bad values
        # only print "Warning: ... ignoring it", so any warning fails the probe. Permission-rule
        # strings are not validated here.
        procs = {n: subprocess.Popen(c, cwd=tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=worker_env())
                 for n, c in cmds.items()}
        for name, proc in procs.items():
            try:
                out = proc.communicate(timeout=90)[0].decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                proc.kill()
                out = "timed out"
            ok = "Input must be provided" in out or (name == "resume" and "No conversation found" in out)
            warn = next((ln.strip() for ln in out.splitlines() if "warning:" in ln.lower()), None)
            line = warn or next((ln for ln in out.splitlines() if re.search(
                r"Input must|No conversation found|error", ln, re.I)), out.strip())
            if name == "control":  # proves the probe still tells accepted from rejected flags
                check("argv probe rejects an unknown flag", "unknown option" in out, line[:300])
            else:
                check(f"argv probe: {name}", ok and not warn, line[:300])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ok = all(c["ok"] for c in checks)
    for flag, where in flags.items():
        if where != "in --help":
            flags[flag] = "not in --help; " + ("accepted by argv probes" if ok else "probe failed")
    emit({"ok": ok, "csa_version": VERSION, "claude_bin": claude_bin, "claude_version": version,
          "python": sys.version.split()[0], "checks": checks, "flags": flags})
    return 0 if ok else 1


# --------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="csa.py", description="Run Claude Code CLI workers for Codex.")
    p.add_argument("--version", action="version", version=f"csa {VERSION}")
    sub = p.add_subparsers(dest="verb", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", required=True, help="repo (or subdirectory) the worker runs in")
    common.add_argument("--allow-windows-fs", action="store_true",
                        help="permit a --cwd on the Windows filesystem (/mnt/<drive>/...)")

    def caps(sp, defaults: bool):
        d = DEFAULT_CAPS if defaults else {"max_turns": None, "budget_usd": None, "timeout_min": None}
        sp.add_argument("--model", help="Claude CLI model alias or id, passed verbatim")
        sp.add_argument("--effort", choices=EFFORTS, help="claude --effort level")
        sp.add_argument("--max-turns", type=int, default=d["max_turns"])
        sp.add_argument("--budget-usd", type=float, default=d["budget_usd"])
        sp.add_argument("--timeout-min", type=float, default=d["timeout_min"])
        sp.add_argument("--detach", action="store_true", help="return immediately; poll with status")

    s = sub.add_parser("start", parents=[common], help="start a new task")
    s.add_argument("--task", required=True)
    s.add_argument("--prompt", required=True, help="prompt file, or - for stdin")
    s.add_argument("--mode", choices=("review", "write"), default="review")
    s.add_argument("--scope", action="append", default=[], help="write-mode path (repeatable)")
    s.add_argument("--allow-bash", action="append", default=[], help="extra Bash prefix (repeatable)")
    s.add_argument("--worktree", action="store_true", help="run in .agent-runs/wt/<task> on csa/<task>")
    s.add_argument("--schema", help="JSON Schema file for structured output; 'none' disables the "
                                    "review preset")
    s.add_argument("--browser", action="store_true",
                   help=f"headless {PLAYWRIGHT_MCP} server (network access)")
    caps(s, True)
    r = sub.add_parser("resume", parents=[common], help="continue a terminal task's session")
    r.add_argument("--task", required=True)
    r.add_argument("--prompt", required=True)
    r.add_argument("--allow-bash", action="append", default=[],
                   help="add a Bash prefix for this and later attempts (repeatable)")
    r.add_argument("--browser", action="store_true",
                   help="add the Playwright MCP server for this and later attempts")
    caps(r, False)
    st = sub.add_parser("status", parents=[common], help="reconcile and show runs")
    st.add_argument("--task")
    st.add_argument("--wait", type=float, default=0.0, help="long-poll up to SEC for a transition")
    st.add_argument("--json", action="store_true")
    rs = sub.add_parser("result", parents=[common], help="final message of the latest attempt")
    rs.add_argument("--task", required=True)
    rs.add_argument("--json", action="store_true")
    c = sub.add_parser("cancel", parents=[common], help="stop a running task")
    c.add_argument("--task", required=True)
    ls = sub.add_parser("list", parents=[common], help="list tasks")
    ls.add_argument("--active", action="store_true")
    ls.add_argument("--json", action="store_true")
    sub.add_parser("doctor", help="verify the claude flags and option values this script emits "
                                  "(no API calls; permission rules are not checked)")
    return p


def main(argv=None) -> int:
    if not sys.platform.startswith("linux") or fcntl is None:
        sys.stderr.write("csa.py runs inside WSL/Linux only. From Windows: wsl.exe -d <distro> -e "
                         "python3 /home/<user>/.codex/skills/claude-subagents/scripts/csa.py ...\n")
        return 2
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    verbs = {"start": cmd_start, "resume": cmd_resume, "status": cmd_status, "result": cmd_result,
             "cancel": cmd_cancel, "list": cmd_list, "doctor": cmd_doctor}
    try:
        return verbs[args.verb](args)
    except (CsaError, OSError, ValueError, subprocess.SubprocessError) as exc:
        emit({"error": str(exc), "verb": args.verb})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
