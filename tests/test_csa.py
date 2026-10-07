"""Tests for csa.py. Stdlib unittest; no API calls (a fake claude stands in).

Run from the repo root inside WSL/Linux:  python3 -m unittest discover -s tests -v
"""
import argparse
import codecs
import contextlib
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "skills" / "claude-subagents" / "scripts" / "csa.py"
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(SCRIPT.parent))
sys.dont_write_bytecode = True  # keep __pycache__ out of the installable skill directory
import csa  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1"}
# Every tool @playwright/mcp@0.0.83 exposes with default caps (core, core-input, core-navigation,
# core-tabs; skill-only tools excluded), read from its coreBundle.js. A version bump must
# re-classify each one as allowed or denied.
PINNED_BROWSER_TOOLS = {
    "browser_click", "browser_close", "browser_console_messages", "browser_drag", "browser_drop",
    "browser_emulate_media", "browser_evaluate", "browser_file_upload", "browser_fill_form",
    "browser_find", "browser_handle_dialog", "browser_hover", "browser_navigate",
    "browser_navigate_back", "browser_network_request", "browser_network_requests",
    "browser_press_key", "browser_resize", "browser_run_code_unsafe", "browser_select_option",
    "browser_snapshot", "browser_tabs", "browser_take_screenshot", "browser_type",
    "browser_wait_for"}


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **GIT_ENV, "HOME": str(root.parent)}
    for cmd in (["git", "init", "-q", "-b", "main"],):
        subprocess.run(cmd, cwd=root, check=True, env=env)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "README.md").write_text("# demo\n")
    (root / ".gitignore").write_text("*.log\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, env=env)
    return root


def run_dict(**over):
    run = {"task": "t1", "workdir": "/repo", "mode": "review", "scopes": [], "allow_bash": [],
           "browser": False, "session_id": "11111111-1111-4111-8111-111111111111",
           "caps": dict(csa.DEFAULT_CAPS), "model_requested": None, "effort_requested": None,
           "json_schema": csa.REVIEW_SCHEMA, "worktree": None}
    run.update(over)
    return run


def values_after(cmd, flag):
    """Values of a variadic flag: everything up to the next token starting with '-'."""
    i = cmd.index(flag) + 1
    out = []
    while i < len(cmd) and not cmd[i].startswith("-"):
        out.append(cmd[i])
        i += 1
    return out


class BuildCmdTest(unittest.TestCase):
    def test_review_defaults(self):
        cmd = csa.build_cmd(run_dict(), {"kind": "start"}, "/bin/claude", Path("/x/a1"))
        self.assertEqual(cmd[:2], ["/bin/claude", "-p"])
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], run_dict()["session_id"])
        self.assertNotIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--name") + 1], "csa-t1")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(json.loads(cmd[cmd.index("--settings") + 1]),
                         {"disableAllHooks": True,
                          "permissions": {"blockReadsOutsideWorkingDirectories": True}})
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "80")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "5")
        self.assertNotIn("--model", cmd)
        self.assertNotIn("--effort", cmd)
        self.assertNotIn("--mcp-config", cmd)
        self.assertNotIn("--add-dir", cmd)
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), csa.REVIEW_SCHEMA)
        contract = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("Mode: review", contract)
        self.assertIn("Do not modify any file, including via shell.", contract)
        self.assertTrue(contract.endswith(csa.SCHEMA_DELIVERABLE), contract)
        self.assertNotIn("Structure it", contract)
        deny = values_after(cmd, "--disallowedTools")
        for tool in ("Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "PushNotification",
                     "RemoteTrigger", "SendMessage", "Monitor", "ScheduleWakeup", "EnterWorktree"):
            self.assertIn(tool, deny)
        self.assertNotIn("Task", deny)  # subagents stay: they inherit the same rules
        allow = values_after(cmd, "--allowedTools")
        for rule in ("Bash(git status:*)", "Bash(git diff:*)", "Bash(rg:*)", "Bash(tail:*)"):
            self.assertIn(rule, allow)
        self.assertFalse(any(r.startswith("Edit(") for r in allow))

    def test_no_prompt_in_argv_and_variadics_grouped(self):
        cmd = csa.build_cmd(run_dict(mode="write", scopes=["src"], allow_bash=["pytest -q"],
                                     browser=True, json_schema=None),
                            {"kind": "start"}, "/bin/claude", Path("/x/a1"))
        # each variadic flag appears once, and only rule-shaped values follow it
        for flag in ("--allowedTools", "--disallowedTools", "--mcp-config", "--add-dir"):
            self.assertEqual(cmd.count(flag), 1)
        rules = values_after(cmd, "--allowedTools") + values_after(cmd, "--disallowedTools")
        self.assertTrue(all("(" in r or r.startswith("mcp__") or r in csa.ALWAYS_DENY + csa.REVIEW_DENY
                            for r in rules), rules)
        self.assertEqual(cmd.index("--disallowedTools") + len(values_after(cmd, "--disallowedTools")) + 1,
                         len(cmd), "nothing may follow the last variadic group")
        self.assertEqual(values_after(cmd, "--add-dir"), ["/x/a1/browser"])

    def test_write_mode_rules(self):
        cmd = csa.build_cmd(run_dict(mode="write", scopes=["src", "docs/api"], allow_bash=["pytest"],
                                     json_schema=None), {"kind": "start"}, "/bin/claude", Path("/x/a1"))
        allow = values_after(cmd, "--allowedTools")
        for rule in ("Edit(/src)", "Edit(/src/**)", "Edit(/docs/api/**)", "Bash(pytest:*)",
                     "Bash(git log:*)"):
            self.assertIn(rule, allow)
        deny = values_after(cmd, "--disallowedTools")
        self.assertNotIn("Edit", deny)
        self.assertIn("WebFetch", deny)
        self.assertNotIn("--json-schema", cmd)
        contract = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("Mode: write", contract)
        self.assertIn("You may modify only: src, docs/api.", contract)
        self.assertTrue(contract.endswith(csa.PROSE_DELIVERABLE), contract)

    def test_resume_uses_resume_not_session_id(self):
        cmd = csa.build_cmd(run_dict(), {"kind": "resume"}, "/bin/claude", Path("/x/a2"))
        self.assertEqual(cmd[cmd.index("--resume") + 1], run_dict()["session_id"])
        self.assertNotIn("--session-id", cmd)

    def test_model_effort_verbatim(self):
        cmd = csa.build_cmd(run_dict(model_requested="claude-opus-4-1", effort_requested="xhigh"),
                            {"kind": "start"}, "/bin/claude", Path("/x/a1"))
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-4-1")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "xhigh")

    def test_browser_flags(self):
        att_dir = Path("/r/.agent-runs/claude/t1/a1")
        for mode, extra in (("review", {}), ("write", {"scopes": ["src"], "json_schema": None})):
            cmd = csa.build_cmd(run_dict(browser=True, mode=mode, **extra), {"kind": "start"},
                                "/bin/claude", att_dir)
            self.assertEqual(values_after(cmd, "--mcp-config"), [str(att_dir / "mcp.json")])
            self.assertEqual(values_after(cmd, "--add-dir"), [str(att_dir / "browser")])
            allow, deny = values_after(cmd, "--allowedTools"), values_after(cmd, "--disallowedTools")
            self.assertFalse([r for r in allow + deny if r.startswith("mcp__") and "*" in r])
            self.assertEqual({r for r in allow if r.startswith("mcp__")},
                             {"mcp__playwright__" + t for t in csa.BROWSER_ALLOW})
            for tool in ("browser_run_code_unsafe", "browser_evaluate", "browser_file_upload",
                         "browser_drop", "browser_snapshot", "browser_take_screenshot",
                         "browser_console_messages", "browser_network_requests"):
                self.assertIn("mcp__playwright__" + tool, deny, mode)
        self.assertFalse(set(csa.BROWSER_ALLOW) & set(csa.BROWSER_DENY))
        self.assertEqual(set(csa.BROWSER_ALLOW) | set(csa.BROWSER_DENY), PINNED_BROWSER_TOOLS)
        self.assertRegex(csa.PLAYWRIGHT_MCP, r"^@playwright/mcp@\d+\.\d+\.\d+$", "exact, not a tag")
        server = csa.mcp_config(att_dir)["mcpServers"]["playwright"]
        self.assertEqual(server["command"], "npx")
        self.assertEqual(server["args"][:2], ["-y", "@playwright/mcp@0.0.83"])
        for f in ("--headless", "--isolated", "--no-webmcp", "--output-dir"):
            self.assertIn(f, server["args"])
        self.assertNotIn("--allow-unrestricted-file-access", server["args"])
        self.assertEqual(server["args"][server["args"].index("--output-dir") + 1],
                         str(att_dir / "browser"))


class ParseStreamTest(unittest.TestCase):
    def parse(self, name):
        return csa.parse_stream(FIXTURES / name)

    def test_fixtures_have_no_secrets(self):
        for f in FIXTURES.glob("*.jsonl"):
            text = f.read_text(encoding="utf-8", errors="replace").lower()
            self.assertNotIn("sk-", text, f.name)
            self.assertNotIn("password", text, f.name)

    def test_real_v1_success_with_denial(self):
        p = self.parse("v1_success_with_denial.jsonl")
        self.assertEqual(p["result"]["subtype"], "success")
        self.assertFalse(p["result"]["is_error"])
        self.assertEqual(p["init"]["permissionMode"], "acceptEdits")
        self.assertEqual(len(p["denials"]), 1)
        self.assertEqual(p["denials"][0]["tool"], "Bash")
        self.assertEqual(csa.classify(0, p["result"], []), "complete")

    def test_real_v1_api_error_is_not_success(self):
        p = self.parse("v1_api_error.jsonl")
        self.assertEqual(p["result"]["subtype"], "success")  # subtype lies; is_error tells
        self.assertTrue(p["result"]["is_error"])
        self.assertEqual(csa.classify(0, p["result"], []), "failed")
        self.assertEqual(csa.classify(1, p["result"], []), "failed")

    def test_real_v1_unrecognized_model(self):
        p = self.parse("v1_unrecognized_model.jsonl")
        self.assertEqual(p["result"]["api_error_status"], 404)
        self.assertEqual(p["init"]["model"], "sol")
        self.assertEqual(csa.classify(1, p["result"], []), "failed")

    def test_real_v1_no_result(self):
        p = self.parse("v1_no_result.jsonl")
        self.assertIsNone(p["result"])
        self.assertIsNotNone(p["init"])
        self.assertEqual(csa.classify(0, None, []), "failed")
        self.assertEqual(csa.classify(None, None, [], launcher_lost=True), "interrupted")
        self.assertEqual(csa.classify(-9, None, []), "interrupted")

    def test_real_v1_aborted_streaming(self):
        p = self.parse("v1_aborted_streaming.jsonl")
        self.assertEqual(p["result"]["subtype"], "error_during_execution")
        self.assertEqual(p["result"]["terminal_reason"], "aborted_streaming")
        self.assertEqual(csa.classify(0, p["result"], []), "failed")

    def test_real_v2_structured_output(self):
        p = self.parse("v2_review_structured.jsonl")
        self.assertEqual(p["init"]["permissionMode"], "dontAsk")
        self.assertEqual(p["init"]["mcp_servers"], [])
        so = p["result"]["structured_output"]
        self.assertEqual(set(so), {"verdict", "findings", "verification", "risks"})
        self.assertEqual(json.loads(p["result"]["result"]), so)
        self.assertEqual(csa.classify(0, p["result"], []), "complete")

    def test_real_v2_write_denial(self):
        p = self.parse("v2_write_denied.jsonl")
        self.assertEqual(p["denials"][0]["tool"], "Write")
        self.assertIn("outside.txt", p["denials"][0]["input"])

    def test_real_v2_sigint(self):
        p = self.parse("v2_cancelled_sigint.jsonl")
        self.assertEqual(p["result"]["terminal_reason"], "aborted_streaming")
        self.assertEqual(csa.classify(0, p["result"], [], reason="cancelled"), "cancelled")

    def write(self, events, tail=""):
        fd, name = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(e) + "\n" for e in events) + tail)
        self.addCleanup(os.unlink, name)
        return csa.parse_stream(Path(name))

    def test_synthetic_streams(self):
        init = {"type": "system", "subtype": "init", "model": "m", "permissionMode": "dontAsk"}
        aborted = self.write([init, {"type": "assistant", "message": {}}], tail='{"type":"assi')
        self.assertIsNone(aborted["result"])
        self.assertEqual(csa.classify(None, None, [], launcher_lost=True), "interrupted")
        api = self.write([init, {"type": "result", "subtype": "success", "is_error": True,
                                 "terminal_reason": "api_error", "result": "API Error: 529"}])
        self.assertEqual(csa.classify(1, api["result"], []), "failed")
        model = self.write([init, {"type": "result", "subtype": "success", "is_error": True,
                                   "api_error_status": 404, "result": "issue with the selected model"}])
        self.assertEqual(csa.classify(1, model["result"], []), "failed")
        turns = self.write([init, {"type": "result", "subtype": "error_max_turns", "is_error": True}])
        self.assertEqual(csa.classify(1, turns["result"], []), "failed")
        two = self.write([init, {"type": "result", "subtype": "success", "is_error": False, "result": "a"},
                          {"type": "result", "subtype": "success", "is_error": False, "result": "b"}])
        self.assertEqual(two["result"]["result"], "b")

    def test_nonfinite_caps_refused(self):
        for key in ("budget_usd", "timeout_min"):
            for bad in (float("nan"), float("inf"), 0.0, -1.0):
                caps = {**csa.DEFAULT_CAPS, key: bad}
                with self.assertRaisesRegex(csa.CsaError, "finite", msg=(key, bad)):
                    csa.check_caps(caps)
        self.assertEqual(csa.check_caps(dict(csa.DEFAULT_CAPS)), csa.DEFAULT_CAPS)

    def test_classify_precedence(self):
        ok = {"subtype": "success", "is_error": False}
        self.assertEqual(csa.classify(0, ok, ["x"]), "scope-violation")
        self.assertEqual(csa.classify(0, ok, ["x"], reason="timeout"), "scope-violation")
        self.assertEqual(csa.classify(0, ok, [], reason="timeout"), "timeout")
        self.assertEqual(csa.classify(0, ok, None), "failed")
        self.assertEqual(csa.classify(None, ok, [], launcher_lost=True), "complete")
        self.assertEqual(csa.classify(0, ok, []), "complete")


class PathTest(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("WSL_DISTRO_NAME")
        os.environ["WSL_DISTRO_NAME"] = "Ubuntu-22.04"

    def tearDown(self):
        if self.old is None:
            os.environ.pop("WSL_DISTRO_NAME", None)
        else:
            os.environ["WSL_DISTRO_NAME"] = self.old

    def test_translations(self):
        cases = {
            r"\\wsl.localhost\Ubuntu-22.04\home\me\projects\x": "/home/me/projects/x",
            "//wsl.localhost/Ubuntu-22.04/home/me/x/": "/home/me/x",
            r"\\?\UNC\wsl.localhost\Ubuntu-22.04\home\me\x": "/home/me/x",
            r"\\?\unc\wsl.localhost\Ubuntu-22.04\home\me\x": "/home/me/x",
            r"\\wsl$\Ubuntu-22.04\home\me": "/home/me",
            r"\\wsl.localhost\Ubuntu-22.04": "/",
            # what arrives via powershell.exe -Command: each "\\" collapsed to "\"
            r"\wsl.localhost\Ubuntu-22.04\home\me\x": "/home/me/x",
            r"\?\UNC\wsl.localhost\Ubuntu-22.04\home\me\x": "/home/me/x",
            r"\wsl$\Ubuntu-22.04\home\me": "/home/me",
            "/wsl.localhost/Ubuntu-22.04/x": "/wsl.localhost/Ubuntu-22.04/x",  # a real Linux path
            r"C:\Users\me\repo": "/mnt/c/Users/me/repo",
            "/home/me/x": "/home/me/x",
            "relative/dir": "relative/dir",
        }
        for raw, want in cases.items():
            self.assertEqual(csa.to_linux_path(raw), want, raw)

    def test_other_distro_refused(self):
        with self.assertRaises(csa.CsaError):
            csa.to_linux_path(r"\\wsl.localhost\Debian\home\me")

    def test_windows_fs_cwd_refused(self):
        for raw in (r"C:\Users", "/mnt/c/Users"):
            with self.assertRaises(csa.CsaError):
                csa.resolve_cwd(raw, allow_windows_fs=False)

    def test_unc_cwd_resolves(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for lead in ("\\\\", "\\"):  # as typed, and as powershell.exe -Command delivers it
            unc = lead + "wsl.localhost\\Ubuntu-22.04" + str(tmp).replace("/", "\\")
            self.assertEqual(csa.resolve_cwd(unc, False), tmp.resolve())

    def test_text_file_encodings(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        text = "Hello \u00e9\u2713\n"
        for name, data in (("plain", text.encode("utf-8")),
                           ("bom8", codecs.BOM_UTF8 + text.encode("utf-8")),
                           ("le16", text.encode("utf-16")),  # Python writes a BOM
                           ("be16", codecs.BOM_UTF16_BE + text.encode("utf-16-be"))):
            (tmp / name).write_bytes(data)
            self.assertEqual(csa.read_text(str(tmp / name)), text, name)
        (tmp / "ansi").write_bytes("caf\u00e9\n".encode("cp1252"))
        with self.assertRaisesRegex(csa.CsaError, "save it as UTF-8"):
            csa.read_text(str(tmp / "ansi"))

    def test_scope_validation(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.assertEqual(csa.normalize_scope("src/", tmp), "src")
        self.assertEqual(csa.normalize_scope("src\\auth", tmp), "src/auth")
        for bad in ("/etc", "../x", ".", "a b", "x,y", "a/*", ".git/hooks", ".agent-runs", "-x"):
            with self.assertRaises(csa.CsaError, msg=bad):
                csa.normalize_scope(bad, tmp)
        os.symlink("/tmp", tmp / "link")
        with self.assertRaises(csa.CsaError):
            csa.normalize_scope("link", tmp)


class GitStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = make_repo(self.tmp / "repo")
        self.env = {**os.environ, **GIT_ENV}

    def test_info_exclude_written_once_and_gitignore_untouched(self):
        before = (self.repo / ".gitignore").read_bytes()
        exclude = csa.ensure_excluded(self.repo)
        csa.ensure_excluded(self.repo)
        self.assertEqual(exclude, (self.repo / ".git" / "info" / "exclude").resolve())
        lines = exclude.read_text().splitlines()
        self.assertEqual(lines.count(".agent-runs/"), 1)
        self.assertEqual((self.repo / ".gitignore").read_bytes(), before)
        (self.repo / ".agent-runs" / "claude").mkdir(parents=True)
        (self.repo / ".agent-runs" / "claude" / "x.json").write_text("{}")
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo,
                                capture_output=True, text=True).stdout
        self.assertEqual(status, "")

    def test_scope_check(self):
        (self.repo / "dirty.txt").write_text("before\n")  # pre-existing untracked change
        base = csa.snapshot(self.repo)
        (self.repo / "src" / "app.py").write_text("print('changed')\n")
        (self.repo / "src" / "new.py").write_text("x = 1\n")
        (self.repo / "README.md").write_text("# changed\n")
        (self.repo / "dirty.txt").write_text("after\n")  # modified again: still a change
        (self.repo / ".agent-runs").mkdir()
        (self.repo / ".agent-runs" / "noise").write_text("x")
        changed = csa.changed_paths(self.repo, base, csa.snapshot(self.repo))
        self.assertEqual(changed, ["README.md", "dirty.txt", "src/app.py", "src/new.py"])
        out = [p for p in changed if not csa.in_scope(p, ["src"])]
        self.assertEqual(out, ["README.md", "dirty.txt"])
        self.assertFalse(csa.in_scope("srcfoo/x", ["src"]))

    def test_commit_by_worker_is_detected(self):
        base = csa.snapshot(self.repo)
        (self.repo / "README.md").write_text("# committed\n")
        subprocess.run(["git", "commit", "-qam", "sneaky"], cwd=self.repo, check=True, env=self.env)
        self.assertEqual(csa.changed_paths(self.repo, base, csa.snapshot(self.repo)), ["README.md"])

    def test_no_change(self):
        base = csa.snapshot(self.repo)
        self.assertEqual(csa.changed_paths(self.repo, base, csa.snapshot(self.repo)), [])

    def test_index_only_change_is_detected(self):
        f = self.repo / "README.md"

        def stage(text):
            f.write_text(text)
            subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, env=self.env)
            f.write_text("worktree B\n")

        stage("staged A\n")
        base = csa.snapshot(self.repo)
        stage("staged C\n")  # only the index changes: status stays MM, the file stays B
        after = csa.snapshot(self.repo)
        self.assertEqual([s["entries"]["README.md"][:2] for s in (base, after)], ["MM", "MM"])
        self.assertEqual(csa.changed_paths(self.repo, base, after), ["README.md"])
        self.assertEqual(csa.changed_paths(self.repo, after, csa.snapshot(self.repo)), [])

    def test_unreadable_task_blocks_while_its_lock_is_held(self):
        runs = self.repo / ".agent-runs" / "claude"
        (runs / "broken").mkdir(parents=True)
        (runs / "broken" / "run.json").write_text('{"task": "bro')  # truncated
        self.assertIsNone(csa.tree_conflict(runs, "new", "write"), "no launcher holds it")
        self.assertFalse(csa.is_active(csa.task_row(runs / "broken")))
        fd = csa.try_lock(runs / "broken" / "active.lock")  # a live launcher, unreadable state
        self.addCleanup(os.close, fd)
        for mode in ("write", "review"):  # mode and tree unknown: fail closed
            self.assertIn("'broken'", csa.tree_conflict(runs, "new", mode) or "", mode)
        row = csa.task_row(runs / "broken")
        self.assertEqual((row["status"], row["lock_held"]), ("unknown", True))
        self.assertTrue(csa.is_active(row), "status --wait must keep polling it")

    def test_held_lock_blocks_whatever_the_state_file_holds(self):
        runs = self.repo / ".agent-runs" / "claude"
        for name, content in (("null-state", "null"), ("empty-state", "{}"), ("no-state", None)):
            d = runs / name
            d.mkdir(parents=True)
            if content is not None:
                (d / "run.json").write_text(content)
            self.assertIsNone(csa.tree_conflict(runs, "new", "write"), name)
            fd = csa.try_lock(d / "active.lock")
            try:
                self.assertIn(repr(name), csa.tree_conflict(runs, "new", "write") or "", name)
            finally:
                os.close(fd)

    def test_conflicted_index_stage_edit_is_detected(self):
        f = self.repo / "README.md"
        run = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, env=self.env,
                                        capture_output=True, text=True)
        run("checkout", "-qb", "side")
        f.write_text("side\n")
        run("commit", "-qam", "side")
        run("checkout", "-q", "-")
        f.write_text("main\n")
        run("commit", "-qam", "main")
        subprocess.run(["git", "merge", "-q", "side"], cwd=self.repo, env=self.env, capture_output=True)
        base = csa.snapshot(self.repo)
        self.assertTrue(base["entries"]["README.md"].startswith("UU"))
        oid = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.repo, env=self.env,
                             input="swapped\n", capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(["git", "update-index", "--index-info"], cwd=self.repo, env=self.env, check=True,
                       input=f"100644 {oid} 3\tREADME.md\n", text=True)
        after = csa.snapshot(self.repo)
        self.assertTrue(after["entries"]["README.md"].startswith("UU"), "still conflicted")
        self.assertEqual(csa.changed_paths(self.repo, base, after), ["README.md"])


class ReconcileTest(unittest.TestCase):
    """A run whose launcher and worker are both gone must not stay 'running'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = make_repo(self.tmp / "repo")
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.dead_pid = dead.pid
        self.task_dir = self.repo / ".agent-runs" / "claude" / "stale"
        (self.task_dir / "a1").mkdir(parents=True)
        (self.task_dir / "a1" / "changes.json").write_text(
            json.dumps({"baseline": csa.snapshot(self.repo)}))
        run = run_dict(task="stale", workdir=str(self.repo), cwd=str(self.repo), status="running")
        run["attempts"] = [{"n": 1, "kind": "start", "started_at": csa.now_iso(), "pid": self.dead_pid,
                            "pgid": self.dead_pid, "start_ticks": 1, "status": "running"}]
        csa.save_run(self.task_dir, run)

    def test_stale_pid_without_result_is_interrupted(self):
        run = csa.reconcile(self.task_dir)
        self.assertEqual(run["status"], "interrupted")
        self.assertTrue(run["attempts"][0]["launcher_lost"])
        self.assertEqual(csa.load_run(self.task_dir)["status"], "interrupted")

    def test_stale_pid_with_result_uses_result(self):
        shutil.copy(FIXTURES / "v2_review_structured.jsonl", self.task_dir / "a1" / "stream.jsonl")
        run = csa.reconcile(self.task_dir)
        self.assertEqual(run["status"], "complete")
        self.assertTrue((self.task_dir / "a1" / "result.json").exists())

    def test_held_lock_means_alive(self):
        fd = csa.try_lock(self.task_dir / "active.lock")
        self.addCleanup(os.close, fd)
        self.assertEqual(csa.reconcile(self.task_dir)["status"], "running")

    def test_unrecordable_evidence_still_ends_the_run(self):
        changes = self.task_dir / "a1" / "changes.json"
        changes.unlink()
        changes.mkdir()  # unreadable and unwritable as JSON
        run = csa.reconcile(self.task_dir)
        self.assertEqual(run["status"], "failed")
        self.assertIn("finalize failed", run["attempts"][0]["error"])
        self.assertEqual(csa.load_run(self.task_dir)["status"], "failed")

    def test_scope_check_error_fails_the_run(self):
        shutil.copy(FIXTURES / "v2_review_structured.jsonl", self.task_dir / "a1" / "stream.jsonl")
        run = csa.load_run(self.task_dir)
        run["workdir"] = str(self.tmp / "gone")
        csa.save_run(self.task_dir, run)
        run = csa.reconcile(self.task_dir)
        self.assertEqual(run["status"], "failed", "a success result without a scope verdict")
        self.assertIn("scope check failed", run["attempts"][0]["error"])


class InProcessTest(unittest.TestCase):
    """Races and failure paths driven from inside the process."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = make_repo(self.tmp / "repo")
        self.task_dir = self.repo / ".agent-runs" / "claude" / "t1"
        (self.task_dir / "a1").mkdir(parents=True)

    def test_cancel_never_signals_a_newer_attempt(self):
        newer = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(newer.wait)
        self.addCleanup(newer.kill)
        run = run_dict(task="t1", workdir=str(self.repo), cwd=str(self.repo), status="running")
        run["attempts"] = [{"n": 1, "kind": "start", "started_at": csa.now_iso(), "pid": None,
                            "pgid": None, "status": "running"}]
        csa.save_run(self.task_dir, run)
        fd = csa.try_lock(self.task_dir / "active.lock")  # a live launcher owns the task
        self.addCleanup(os.close, fd)

        def resume_right_after_cancel():  # attempt 1 ends on the marker; a resume starts a2
            while not (self.task_dir / "a1" / "cancel").exists():
                time.sleep(0.01)
            run["attempts"][0]["status"] = "cancelled"
            run["attempts"].append({"n": 2, "kind": "resume", "started_at": csa.now_iso(),
                                    "pid": newer.pid, "pgid": newer.pid, "status": "running",
                                    "start_ticks": csa._proc_stat(newer.pid)[1]})
            (self.task_dir / "a2").mkdir()
            csa.save_run(self.task_dir, run)

        racer = threading.Thread(target=resume_right_after_cancel)
        racer.start()
        args = argparse.Namespace(cwd=str(self.repo), allow_windows_fs=False, task="t1")
        out = io.StringIO()
        with mock.patch.object(csa, "KILL_GRACE_S", 0.0), mock.patch.object(csa, "HARD_KILL_S", 0.0), \
                mock.patch.object(csa, "KILL_WAIT_S", 0.0), contextlib.redirect_stdout(out):
            csa.cmd_cancel(args)
        racer.join()
        self.assertIsNone(newer.poll(), "cancel killed the resumed attempt")
        res = json.loads(out.getvalue())
        self.assertEqual((res["attempt"], res["status"]), (2, "running"))
        self.assertIn("not signalled", res["note"])
        self.assertFalse((self.task_dir / "a2" / "cancel").exists())

    def test_lost_spawn_report_keeps_supervising(self):
        bindir, fake = self.tmp / "bin", self.tmp / "fake"
        bindir.mkdir()
        fake.mkdir()
        shutil.copy(HERE / "fake_claude.py", bindir / "claude")
        (bindir / "claude").chmod(0o755)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):  # run_attempt installs its own
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        run = run_dict(task="t1", workdir=str(self.repo), cwd=str(self.repo), json_schema=None,
                       status="running", attempts=[])
        with mock.patch.dict(os.environ, {"FAKE_DIR": str(fake), "FAKE_SLEEP": "0.5"}):
            csa.new_attempt(self.task_dir, run, "start", "Review.\n", str(bindir / "claude"))
            rfd, wfd = os.pipe()
            os.close(rfd)  # the initiating parent died before the report: BrokenPipeError
            csa.run_attempt(self.task_dir, run, report_fd=wfd)
        self.assertEqual(csa.load_run(self.task_dir)["status"], "complete")

    def test_unwritable_stderr_during_lost_report_keeps_supervising(self):
        bindir, fake = self.tmp / "bin", self.tmp / "fake"
        bindir.mkdir()
        fake.mkdir()
        shutil.copy(HERE / "fake_claude.py", bindir / "claude")
        (bindir / "claude").chmod(0o755)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

        class FullDisk(io.StringIO):
            def write(self, s):
                raise OSError(28, "No space left on device")

        run = run_dict(task="t1", workdir=str(self.repo), cwd=str(self.repo), json_schema=None,
                       status="running", attempts=[])
        with mock.patch.dict(os.environ, {"FAKE_DIR": str(fake), "FAKE_SLEEP": "0.5"}):
            csa.new_attempt(self.task_dir, run, "start", "Review.\n", str(bindir / "claude"))
            rfd, wfd = os.pipe()
            os.close(rfd)
            with mock.patch.object(sys, "stderr", FullDisk()):
                csa.run_attempt(self.task_dir, run, report_fd=wfd)
        self.assertEqual(csa.load_run(self.task_dir)["status"], "complete")


class FakeClaudeTest(unittest.TestCase):
    """End-to-end through the CLI with a stub claude on PATH."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self.cleanup)
        self.repo = make_repo(self.tmp / "repo")
        self.home = self.tmp / "home"
        self.home.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        shutil.copy(HERE / "fake_claude.py", bindir / "claude")
        (bindir / "claude").chmod(0o755)
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        self.env = {**GIT_ENV, "PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(self.home),
                    "FAKE_DIR": str(self.fake), "CSA_KILL_GRACE_S": "1", "CSA_HARD_KILL_S": "1",
                    "LANG": "C.UTF-8"}
        self.prompt = self.tmp / "prompt.md"
        self.prompt.write_text("Review the repo. Unicode: \u00e9\u2713\n", encoding="utf-8")

    def cleanup(self):
        for d in (self.repo / ".agent-runs" / "claude").glob("*/run.json"):
            try:
                att = json.loads(d.read_text())["attempts"][-1]
            except (ValueError, KeyError, IndexError):
                continue
            for pid in (att.get("pid"), att.get("launcher_pid")):
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
        for call in self.calls():  # children a failed test left behind
            if call.get("child_pid"):
                try:
                    os.kill(call["child_pid"], signal.SIGKILL)
                except OSError:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def csa(self, *args, ok=True, **fake):
        env = {**self.env, **{k: str(v) for k, v in fake.items()}}
        p = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                           env=env, timeout=120)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        try:
            return json.loads(p.stdout), p.returncode
        except ValueError:
            return p.stdout, p.returncode

    def start(self, task="t1", *extra, **fake):
        return self.csa("start", "--cwd", str(self.repo), "--task", task, "--prompt",
                        str(self.prompt), *extra, **fake)

    def calls(self):
        path = self.fake / "calls.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def signals(self):
        path = self.fake / "signals"
        return path.read_text().split() if path.exists() else []

    def run_json(self, task="t1"):
        return json.loads((self.repo / ".agent-runs" / "claude" / task / "run.json").read_text())

    def wait_for(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        self.fail("condition not met in time")

    def test_start_foreground_complete(self):
        out, _ = self.start()
        self.assertEqual(out["status"], "complete")
        call = self.calls()[0]
        self.assertEqual(call["stdin"], self.prompt.read_text(encoding="utf-8"))
        self.assertFalse(any("Review the repo" in a for a in call["argv"]))
        self.assertEqual(set(call["env"].values()), {"1"}, call["env"])
        self.assertEqual(call["pgid"], call["pid"])
        self.assertEqual(call["argv"][call["argv"].index("--permission-mode") + 1], "dontAsk")
        a1 = self.repo / ".agent-runs" / "claude" / "t1" / "a1"
        for name in ("prompt.md", "stream.jsonl", "stderr.log", "result.md", "result.json",
                     "changes.json"):
            self.assertTrue((a1 / name).exists(), name)
        self.assertEqual((a1 / "result.md").read_text(), "Outcome: done")
        run = self.run_json()
        self.assertEqual(run["schema"], 1)
        att = run["attempts"][0]
        self.assertEqual((att["model_resolved"], att["permission_mode"]), ("fake-model-1", "dontAsk"))
        self.assertEqual(att["cost_usd"], 0.0123)
        self.assertEqual(run["claude_version"], "9.9.9 (Claude Code)")
        self.assertIn(".agent-runs/", (self.repo / ".git" / "info" / "exclude").read_text())
        self.assertEqual((self.repo / ".gitignore").read_text(), "*.log\n")
        res, _ = self.csa("result", "--cwd", str(self.repo), "--task", "t1", "--json")
        self.assertEqual(res["result"], "Outcome: done")

    def test_detach_and_status_wait(self):
        t0 = time.time()
        out, _ = self.start("t1", "--detach", FAKE_SLEEP=3)
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["pid"], out["pgid"])
        st, _ = self.csa("status", "--cwd", str(self.repo), "--json")
        self.assertEqual(st["tasks"][0]["status"], "running")
        self.assertTrue(st["tasks"][0]["worker_alive"])
        st, _ = self.csa("status", "--cwd", str(self.repo), "--wait", "30", "--json")
        self.assertEqual(st["tasks"][0]["status"], "complete")
        self.assertEqual(st["transitions"], [{"task": "t1", "from": "running", "to": "complete"}])
        self.assertLess(st["waited_s"], 15)
        st, _ = self.csa("status", "--cwd", str(self.repo), "--wait", "30", "--json")
        self.assertLess(st["waited_s"], 1, "nothing active: --wait returns at once")

    def test_cancel_reaches_process_group(self):
        self.start("t1", "--detach", FAKE_SLEEP=60, FAKE_CHILD=1)
        self.wait_for(lambda: self.calls())
        child = self.calls()[0]["child_pid"]
        out, _ = self.csa("cancel", "--cwd", str(self.repo), "--task", "t1")
        self.assertEqual(out["status"], "cancelled")
        self.assertIn("SIGINT", self.signals())
        self.wait_for(lambda: csa._proc_stat(child) is None or csa._proc_stat(child)[0] == "Z", 5)

    def test_timeout_escalates(self):
        out, _ = self.start("t1", "--timeout-min", "0.03", FAKE_SLEEP=60, FAKE_IGNORE_INT=1)
        self.assertEqual(out["status"], "timeout")
        self.assertEqual(self.signals()[:2], ["SIGINT", "SIGTERM"])

    def assert_dead(self, pid):
        st = csa._proc_stat(pid)
        self.assertTrue(st is None or st[0] == "Z", f"pid {pid} still runs after the final state")

    def test_child_ignoring_sigterm_is_killed_before_final_state(self):
        out, _ = self.start("t1", FAKE_CHILD="stubborn")  # foreground: returns after the lock
        self.assertEqual(out["status"], "complete")
        self.assertNotIn("error", out)
        self.assert_dead(self.calls()[0]["child_pid"])

    def test_timeout_kills_child_ignoring_sigint_and_sigterm(self):
        # the worker exits on SIGINT; escalation must go on for the child it leaves
        out, _ = self.start("t1", "--timeout-min", "0.02", FAKE_SLEEP=60, FAKE_CHILD="stubborn")
        self.assertEqual(out["status"], "timeout")
        self.assertEqual(self.signals(), ["SIGINT"])
        self.assert_dead(self.calls()[0]["child_pid"])

    def test_resume_keeps_first_attempt(self):
        self.start()
        a1 = self.repo / ".agent-runs" / "claude" / "t1" / "a1"
        digest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in a1.iterdir()}
        prompt2 = self.tmp / "p2.md"
        prompt2.write_text("Continue.\n")
        out, _ = self.csa("resume", "--cwd", str(self.repo), "--task", "t1", "--prompt", str(prompt2))
        self.assertEqual((out["status"], out["attempt"], out["kind"]), ("complete", 2, "resume"))
        self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in a1.iterdir()},
                         digest)
        first, second = self.calls()
        sid = self.run_json()["session_id"]
        self.assertEqual(first["argv"][first["argv"].index("--session-id") + 1], sid)
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], sid)
        self.assertNotIn("--session-id", second["argv"])
        self.assertEqual(second["stdin"], "Continue.\n")

    def test_duplicate_start_and_running_resume_refused(self):
        self.start("t1", "--detach", FAKE_SLEEP=4)
        out, rc = self.start("t1", ok=False)
        self.assertEqual(rc, 1)
        self.assertIn("active", out["error"])
        out, rc = self.csa("resume", "--cwd", str(self.repo), "--task", "t1", "--prompt",
                           str(self.prompt), ok=False)
        self.assertEqual(rc, 1)
        self.csa("status", "--cwd", str(self.repo), "--wait", "30")
        out, rc = self.start("t1", ok=False)
        self.assertIn("already exists", out["error"])
        self.assertEqual(len(self.calls()), 1)

    def test_launcher_sigterm_writes_final_state(self):
        self.start("t1", "--detach", FAKE_SLEEP=60)
        self.wait_for(lambda: self.run_json()["attempts"][0].get("launcher_pid"))
        os.kill(self.run_json()["attempts"][0]["launcher_pid"], signal.SIGTERM)
        self.wait_for(lambda: self.run_json()["status"] != "running")
        self.assertEqual(self.run_json()["status"], "interrupted")
        self.assertIn("SIGINT", self.signals())

    def test_killed_launcher_reconciled(self):
        self.start("t1", "--detach", FAKE_SLEEP=60, FAKE_CHILD=1)
        self.wait_for(lambda: self.calls())
        att = self.run_json()["attempts"][0]
        child = self.calls()[0]["child_pid"]
        os.kill(att["launcher_pid"], signal.SIGKILL)
        time.sleep(0.3)
        st, _ = self.csa("status", "--cwd", str(self.repo), "--json")
        self.assertEqual(st["tasks"][0]["status"], "running", "orphaned worker is still alive")
        os.kill(att["pid"], signal.SIGKILL)
        self.wait_for(lambda: not os.path.exists(f"/proc/{att['pid']}"), 5)
        st = csa._proc_stat(child)
        self.assertTrue(st and st[0] != "Z", "the worker's background child outlives it")
        st, _ = self.csa("status", "--cwd", str(self.repo), "--json")
        self.assertEqual(st["tasks"][0]["status"], "interrupted")
        self.assertTrue(st["tasks"][0]["launcher_lost"])
        self.assertTrue(self.run_json()["attempts"][0]["launcher_lost"])
        self.wait_for(lambda: csa._proc_stat(child) is None or csa._proc_stat(child)[0] == "Z", 5)

    def test_orphaned_worker_past_timeout_is_stopped_by_status(self):
        self.start("t1", "--detach", "--timeout-min", "0.02", FAKE_SLEEP=60, FAKE_CHILD="stubborn")
        self.wait_for(lambda: self.calls())
        os.kill(self.run_json()["attempts"][0]["launcher_pid"], signal.SIGKILL)
        time.sleep(1.5)
        st, _ = self.csa("status", "--cwd", str(self.repo), "--json")
        self.assertEqual(st["tasks"][0]["status"], "timeout")
        self.assertIn("SIGINT", self.signals())
        self.assert_dead(self.calls()[0]["child_pid"])

    def test_browser_and_schema_none(self):
        out, _ = self.start("t1", "--browser", "--schema", "none")
        argv = self.calls()[0]["argv"]
        att_dir = Path(out["attempt_dir"])
        mcp = att_dir / "mcp.json"
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(mcp))
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(att_dir / "browser"))
        self.assertTrue((att_dir / "browser").is_dir())
        self.assertIn("mcp__playwright__browser_navigate", argv)
        self.assertFalse([a for a in argv if a.startswith("mcp__") and "*" in a])
        self.assertNotIn("--json-schema", argv)
        args = json.loads(mcp.read_text())["mcpServers"]["playwright"]["args"]
        self.assertEqual(args[1], csa.PLAYWRIGHT_MCP)
        self.assertEqual(args[args.index("--output-dir") + 1], str(att_dir / "browser"))

    def test_resume_adds_permissions_per_attempt(self):
        self.start()
        out, rc = self.csa("resume", "--cwd", str(self.repo), "--task", "t1", "--prompt",
                           str(self.prompt), "--timeout-min", "nan", ok=False)
        self.assertEqual(rc, 1)
        self.assertIn("finite", out["error"])
        out, _ = self.csa("resume", "--cwd", str(self.repo), "--task", "t1", "--prompt",
                          str(self.prompt), "--allow-bash", "pytest  -q", "--browser")
        self.assertEqual((out["status"], out["attempt"]), ("complete", 2))
        self.csa("resume", "--cwd", str(self.repo), "--task", "t1", "--prompt", str(self.prompt),
                 "--allow-bash", "pytest -q", "--allow-bash", "make test")
        first, second, third = (c["argv"] for c in self.calls())
        self.assertNotIn("Bash(pytest -q:*)", first)
        self.assertNotIn("--mcp-config", first)
        self.assertIn("Bash(pytest -q:*)", second)
        self.assertIn("--mcp-config", second)
        self.assertIn("Bash(make test:*)", third)
        self.assertEqual(third.count("Bash(pytest -q:*)"), 1)
        self.assertIn("--mcp-config", third, "browser stays on for later attempts")
        self.assertEqual([(a["allow_bash"], a["browser"]) for a in self.run_json()["attempts"]],
                         [([], False), (["pytest -q"], True), (["pytest -q", "make test"], True)])

    def test_nonfinite_caps_refused_before_launch(self):
        for flag in ("--timeout-min", "--budget-usd"):
            for val in ("nan", "inf"):
                out, rc = self.start("t1", flag, val, ok=False)
                self.assertEqual(rc, 1, (flag, val))
                self.assertIn("finite", out["error"])
        self.assertEqual(self.calls(), [])

    def test_scope_inside_submodule_refused(self):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                              text=True, check=True).stdout.strip()
        subprocess.run(["git", "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/lib"],
                       cwd=self.repo, check=True)
        (self.repo / "vendor" / "lib" / "src").mkdir(parents=True)
        out, rc = self.start("w1", "--mode", "write", "--scope", "vendor/lib/src", ok=False)
        self.assertEqual(rc, 1)
        self.assertIn("submodule 'vendor/lib'", out["error"])
        out, rc = self.csa("start", "--cwd", str(self.repo / "vendor"), "--task", "w1", "--mode",
                           "write", "--scope", "lib/src", "--prompt", str(self.prompt), ok=False)
        self.assertIn("submodule 'vendor/lib'", out["error"], "scopes are relative to --cwd")
        self.assertEqual(self.calls(), [])

    def test_write_scope_violation_flagged_not_reverted(self):
        out, _ = self.start("t1", "--mode", "write", "--scope", "src",
                            FAKE_WRITE="src/ok.py,outside.txt")
        self.assertEqual(out["status"], "scope-violation")
        self.assertEqual(out["out_of_scope"], ["outside.txt"])
        changes = json.loads((self.repo / ".agent-runs/claude/t1/a1/changes.json").read_text())
        self.assertEqual(changes["changed"], ["outside.txt", "src/ok.py"])
        self.assertTrue((self.repo / "outside.txt").exists())

    def test_write_in_scope_completes(self):
        out, _ = self.start("t1", "--mode", "write", "--scope", "src", FAKE_WRITE="src/ok.py")
        self.assertEqual((out["status"], out["out_of_scope"]), ("complete", []))

    def test_review_mode_any_change_is_violation(self):
        out, _ = self.start("t1", FAKE_WRITE="src/app.py")
        self.assertEqual(out["status"], "scope-violation")

    def test_write_requires_scope(self):
        out, rc = self.start("t1", "--mode", "write", ok=False)
        self.assertEqual(rc, 1)
        self.assertIn("--scope", out["error"])

    def test_parallel_writers_need_worktree(self):
        self.start("w1", "--mode", "write", "--scope", "src", "--detach", FAKE_SLEEP=5)
        out, rc = self.start("w2", "--mode", "write", "--scope", "docs", ok=False)
        self.assertIn("--worktree", out["error"])
        out, _ = self.start("w2", "--mode", "write", "--scope", "src", "--worktree",
                            FAKE_WRITE="src/wt.py")
        wt = self.repo / ".agent-runs" / "wt" / "w2"
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["workdir"], str(wt))
        self.assertIn(str(wt), [c["cwd"] for c in self.calls()])
        self.assertTrue((wt / "src" / "wt.py").exists())
        self.assertFalse((self.repo / "src" / "wt.py").exists())
        branches = subprocess.run(["git", "branch", "--list", "csa/w2"], cwd=self.repo,
                                  capture_output=True, text=True).stdout
        self.assertIn("csa/w2", branches)

    def test_failures(self):
        out, _ = self.start("f1", FAKE_SUBTYPE="error_max_turns", FAKE_IS_ERROR=1, FAKE_RC=1)
        self.assertEqual(out["status"], "failed")
        out, _ = self.start("f2", FAKE_NO_RESULT=1, FAKE_RC=1)
        self.assertEqual(out["status"], "failed")
        out, _ = self.start("f3", FAKE_NO_RESULT=1)
        self.assertEqual(out["status"], "failed", "exit 0 without a result event is not success")

    def test_invalid_task_and_list_legacy(self):
        out, rc = self.start("Bad_Task", ok=False)
        self.assertEqual(rc, 1)
        runs = self.repo / ".agent-runs" / "claude"
        runs.mkdir(parents=True)
        (runs / "ledger.json").write_text('{"runs": []}')
        self.start("t1")
        out, _ = self.csa("list", "--cwd", str(self.repo), "--json")
        self.assertEqual([t["task"] for t in out["tasks"]], ["t1"])
        self.assertIn("ledger.json", out["legacy"])
        out, _ = self.csa("list", "--cwd", str(self.repo), "--active", "--json")
        self.assertEqual(out["tasks"], [])
        (runs / "old").mkdir()
        (runs / "old" / "stream.jsonl").write_text("{}\n")  # v1 per-task layout
        out, rc = self.start("old", ok=False)
        self.assertIn("v1 run files", out["error"])
        self.assertEqual((runs / "old" / "stream.jsonl").read_text(), "{}\n")

    def test_main_tree_runs_exclusive_with_writers(self):
        self.start("w1", "--mode", "write", "--scope", "src", "--detach", FAKE_SLEEP=4)
        out, rc = self.start("r1", ok=False)
        self.assertEqual(rc, 1)
        self.assertIn("'w1'", out["error"])
        self.csa("status", "--cwd", str(self.repo), "--wait", "30")
        self.start("r2", "--detach", FAKE_SLEEP=10)
        out, _ = self.start("r3")
        self.assertEqual(out["status"], "complete", "reviews may overlap each other")
        out, rc = self.start("w2", "--mode", "write", "--scope", "src", ok=False)
        self.assertIn("'r2'", out["error"])
        self.assertIn("--worktree", out["error"])
        out, rc = self.csa("resume", "--cwd", str(self.repo), "--task", "w1", "--prompt",
                           str(self.prompt), ok=False)
        self.assertIn("'r2'", out["error"])

    def test_unreadable_task_state_is_isolated(self):
        runs = self.repo / ".agent-runs" / "claude"
        (runs / "broken").mkdir(parents=True)
        (runs / "broken" / "run.json").write_text('{"task": "bro')  # truncated
        out, _ = self.start("t1", FAKE_WRITE="bad\udcff.txt")  # a non-UTF-8 file name
        self.assertEqual(out["status"], "scope-violation")
        self.assertEqual(out["out_of_scope"], ["bad\udcff.txt"])
        for verb in ("status", "list"):
            res, _ = self.csa(verb, "--cwd", str(self.repo), "--json")
            rows = {r["task"]: r for r in res["tasks"]}
            self.assertEqual(rows["t1"]["status"], "scope-violation")
            self.assertEqual(rows["broken"]["status"], "unknown")
            self.assertIn("JSONDecodeError", rows["broken"]["error"])
        text, _ = self.csa("status", "--cwd", str(self.repo))
        self.assertIn("unknown", text)

    def test_prompt_bom_stripped_and_effort_checked(self):
        bom = self.tmp / "bom.md"
        bom.write_bytes(codecs.BOM_UTF8 + b"Hello\n")
        self.csa("start", "--cwd", str(self.repo), "--task", "t1", "--prompt", str(bom))
        self.assertEqual(self.calls()[0]["stdin"], "Hello\n")
        _, rc = self.start("t2", "--effort", "bogus", ok=False)
        self.assertEqual(rc, 2)

    def test_failed_worktree_start_leaves_nothing_behind(self):
        fresh = self.repo / "fresh"
        fresh.mkdir()  # untracked, so absent from a worktree checked out from HEAD
        out, rc = self.csa("start", "--cwd", str(fresh), "--task", "w1", "--mode", "write",
                           "--scope", "x", "--worktree", "--prompt", str(self.prompt), ok=False)
        self.assertEqual(rc, 1, out)
        self.assertFalse((self.repo / ".agent-runs" / "wt" / "w1").exists())
        branches = subprocess.run(["git", "branch", "--list", "csa/w1"], cwd=self.repo,
                                  capture_output=True, text=True).stdout
        self.assertEqual(branches, "")
        out, _ = self.start("w1", "--mode", "write", "--scope", "src", "--worktree")
        self.assertEqual(out["status"], "complete", "the task id is still usable")

    def test_doctor_fails_on_warnings(self):
        out, _ = self.csa("doctor")
        self.assertTrue(out["ok"], out)
        out, rc = self.csa("doctor", ok=False, FAKE_WARN=1)
        self.assertEqual(rc, 1)
        bad = [c for c in out["checks"] if not c["ok"]]
        self.assertTrue(bad and all("Warning:" in c["detail"] for c in bad), bad)


if __name__ == "__main__":
    unittest.main()
