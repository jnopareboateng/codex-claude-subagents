#!/usr/bin/env python3
"""Stand-in for the claude CLI used by test_csa.py. Never calls any API.

Records argv, stdin, cwd and env into $FAKE_DIR/calls.jsonl and received signals into
$FAKE_DIR/signals, emits canned stream-json, then exits. Behavior via env:
FAKE_SLEEP seconds, FAKE_RC exit code, FAKE_SUBTYPE / FAKE_IS_ERROR / FAKE_RESULT for the
result event, FAKE_NO_RESULT=1 to omit it, FAKE_WRITE=a,b files to write (relative to cwd),
FAKE_IGNORE_INT=1 to ignore SIGINT, FAKE_CHILD=1 to spawn a `sleep` child in its group
(FAKE_CHILD=stubborn: one that ignores SIGINT and SIGTERM and is left running at exit).
Doctor probes: --help prints a minimal help, an unknown --csa-* flag is rejected, empty stdin
fails like claude -p does, and FAKE_WARN=1 prints a "Warning:" line first.
"""
import json
import os
import signal
import subprocess
import sys
import time

argv = sys.argv[1:]
if "--version" in argv:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if "--help" in argv:
    print('  --output-format <format>  Output format (choices: "text", "json", "stream-json")\n'
          '  --permission-mode <mode>  Permission mode (choices: "acceptEdits", "dontAsk")')
    sys.exit(0)
bogus = [a for a in argv if a.startswith("--csa-")]
if bogus:
    print(f"error: unknown option '{bogus[0]}'")
    sys.exit(1)
if os.environ.get("FAKE_WARN"):
    print("Warning: Unknown --effort value 'x' - ignoring it")

fake_dir = os.environ["FAKE_DIR"]


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def note_signal(signum, _frame):
    """Mirror claude 2.1.290: SIGINT ends the turn with an aborted result and exit 0;
    SIGTERM exits 143 without a result."""
    with open(os.path.join(fake_dir, "signals"), "a", encoding="utf-8") as fh:
        fh.write(signal.Signals(signum).name + "\n")
    if signum == signal.SIGINT:
        if os.environ.get("FAKE_IGNORE_INT"):
            return
        emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}})
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "num_turns": 1, "total_cost_usd": 0, "terminal_reason": "aborted_streaming",
              "session_id": session, "permission_denials": []})
        sys.exit(0)
    sys.exit(143)


session = flag("--session-id") or flag("--resume")
signal.signal(signal.SIGINT, note_signal)
signal.signal(signal.SIGTERM, note_signal)
prompt = sys.stdin.read()
if not prompt:
    print("Error: Input must be provided either through stdin or as a prompt argument when "
          "using --print")
    sys.exit(1)
stubborn = os.environ.get("FAKE_CHILD") == "stubborn"
child = None
if stubborn:  # ignored dispositions survive exec, so sleep ignores both signals
    child = subprocess.Popen(["sh", "-c", "trap '' INT TERM; exec sleep 300"])
elif os.environ.get("FAKE_CHILD"):
    child = subprocess.Popen(["sleep", "300"])
env = {k: os.environ.get(k) for k in ("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_DISABLE_CRON",
                                      "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")}
with open(os.path.join(fake_dir, "calls.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": prompt, "cwd": os.getcwd(), "pid": os.getpid(),
                         "pgid": os.getpgid(0), "child_pid": child.pid if child else None,
                         "env": env}) + "\n")
emit({"type": "system", "subtype": "init", "model": "fake-model-1", "session_id": session,
      "permissionMode": flag("--permission-mode"), "cwd": os.getcwd()})
for rel in filter(None, os.environ.get("FAKE_WRITE", "").split(",")):
    os.makedirs(os.path.dirname(os.path.abspath(rel)), exist_ok=True)
    with open(rel, "a", encoding="utf-8") as fh:
        fh.write("changed by fake claude\n")
end = time.time() + float(os.environ.get("FAKE_SLEEP", "0"))
while time.time() < end:
    time.sleep(0.05)
emit({"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "working"}]}})
if not os.environ.get("FAKE_NO_RESULT"):
    emit({"type": "result", "subtype": os.environ.get("FAKE_SUBTYPE", "success"),
          "is_error": bool(os.environ.get("FAKE_IS_ERROR")), "num_turns": 2,
          "total_cost_usd": 0.0123, "usage": {"input_tokens": 10, "output_tokens": 5},
          "result": os.environ.get("FAKE_RESULT", "Outcome: done"), "session_id": session,
          "terminal_reason": "completed", "permission_denials": []})
if child and not stubborn:
    child.kill()
sys.exit(int(os.environ.get("FAKE_RC", "0")))
