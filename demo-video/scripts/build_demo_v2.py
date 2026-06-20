#!/usr/bin/env python3
"""
codex-claude-subagents demo video — terminal animation build.
Run: /tmp/videnv2/bin/python build_demo_v2.py
Output: demo-video/renders/codex-claude-subagents-demo-v2.mp4
"""
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FPS = 24
W, H = 1920, 1080
FRAMES_DIR = Path("/tmp/demo_frames_v2")
SCRIPT_DIR = Path(__file__).resolve().parent
RENDERS = SCRIPT_DIR.parent / "renders"

# ── Colors ────────────────────────────────────────────────────────────────────
BG      = (11,  16,  32)
PANEL   = (18,  26,  47)
PANEL2  = (24,  35,  61)
FG      = (231, 237, 247)
MUTED   = (159, 176, 200)
ACCENT  = (244, 184,  96)
SUCCESS = (103, 211, 145)
ERR     = (240,  88,  76)

# ── Fonts ─────────────────────────────────────────────────────────────────────
MONO   = "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf"
MONO_B = "/usr/share/fonts/truetype/ubuntu/UbuntuMono-B.ttf"
SANS   = "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf"
SANS_B = "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"

def F(path, size):
    return ImageFont.truetype(path, size)

def alpha_color(color, a):
    return tuple(int(c * min(1.0, max(0.0, a))) for c in color)

def base():
    return Image.new("RGB", (W, H), BG)

def text_w(draw, text, fnt):
    bb = draw.textbbox((0, 0), text, font=fnt)
    return bb[2] - bb[0]

def centered(draw, text, y, fnt, color):
    x = (W - text_w(draw, text, fnt)) // 2
    draw.text((x, y), text, font=fnt, fill=color)

def terminal_chrome(draw, x, y, w, h, title="bash"):
    r = 12
    draw.rounded_rectangle([x, y, x+w, y+h], radius=r, fill=PANEL)
    # title bar
    draw.rounded_rectangle([x, y, x+w, y+42], radius=r, fill=PANEL2)
    draw.rectangle([x, y+30, x+w, y+42], fill=PANEL2)  # flatten bottom of bar
    # window dots
    for i, c in enumerate([(235,80,80), (244,184,96), (103,211,145)]):
        draw.ellipse([x+16+i*24, y+14, x+30+i*24, y+28], fill=c)
    # title label
    tf = F(SANS, 13)
    tw = text_w(draw, title, tf)
    draw.text((x + (w - tw)//2, y+12), title, font=tf, fill=MUTED)

def fade(t, start, dur=0.4):
    return min(1.0, max(0.0, (t - start) / dur))


# ── Scene 1: Title ───────────────────────────────────────────────────────────
def scene_title(n):
    h1 = F(SANS_B, 74)
    h2 = F(SANS,   32)
    h3 = F(MONO,   22)
    frames = []
    for i in range(n):
        t = i / FPS
        img = base()
        d = ImageDraw.Draw(img)

        a1 = fade(t, 0.0, 0.7)
        a2 = fade(t, 1.1, 0.6)
        a3 = fade(t, 2.3, 0.5)

        # accent bar
        if a1 > 0:
            bar_w = int(320 * a1)
            bx = (W - bar_w) // 2
            d.rectangle([bx, H//2 - 88, bx + bar_w, H//2 - 83], fill=alpha_color(ACCENT, a1))

        centered(d, "codex-claude-subagents", H//2 - 70, h1, alpha_color(FG, a1))
        centered(d, "Codex leads. Claude works. The logs prove it.", H//2 + 24, h2, alpha_color(MUTED, a2))
        centered(d, "cp -R skills/claude-subagents ~/.codex/skills/", H//2 + 84, h3, alpha_color(ACCENT, a3))
        frames.append(img)
    return frames


# ── Scene 2: Terminal — command + live log ───────────────────────────────────
def scene_terminal(n):
    M20 = F(MONO, 20)
    M18 = F(MONO, 18)
    S14 = F(SANS, 14)

    TX, TY = 100, 72
    TW, TH = W - 200, H - 144
    LPAD = 30
    LINE_H = 27
    CONTENT_TOP = TY + 52

    CMD1 = "$ python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \\"
    CMD2 = "    --task smoke-test  --prompt examples/prompts/read-only-audit.md"

    # Output lines: (text, color)
    OUT = [
        ("", FG),
        ("[11:54:41]  worker started", MUTED),
        ("            task        = smoke-test", MUTED),
        ("            session_id  = 3a89fee7-89e5-4349-81f4-d33eeb91cfe7", MUTED),
        ("            write_scope = []  (read-only)", MUTED),
        ("", FG),
        ("[11:54:43]  Bash   ls .", FG),
        ("[11:54:44]  Read   skills/claude-subagents/scripts/run_claude_subagent.py", FG),
        ("[11:54:45]  Read   skills/claude-subagents/agents/openai.yaml", FG),
        ("[11:54:46]  Read   examples/prompts/read-only-audit.md", FG),
        ("[11:54:46]  Bash   grep -rn 'api_key|secret|password|sk-|bearer' .", FG),
        ("            → 0 hits", SUCCESS),
        ("[11:54:47]  Bash   grep -rn 'shell=True' . --include='*.py'", FG),
        ("            → 0 hits", SUCCESS),
        ("[11:54:49]  Bash   find . -perm /o+w", FG),
        ("            → 0 files", SUCCESS),
        ("[11:54:51]  Read   .agent-runs/claude/ledger.json", FG),
        ("", FG),
        ("[11:55:02]  Write  .agent-runs/claude/smoke-test.summary.md", ACCENT),
        ("", FG),
        ("[11:58:01]  worker complete   exit_code=0", SUCCESS),
        ("            summary  → .agent-runs/claude/smoke-test.summary.md", SUCCESS),
        ("            ledger   → .agent-runs/claude/ledger.json", SUCCESS),
    ]

    # Timing
    T_CMD1_END   = 2.8
    T_CMD2_START = T_CMD1_END
    T_CMD2_END   = 4.8
    T_OUT_START  = 5.4
    T_TOTAL      = n / FPS

    lines_per_sec = len(OUT) / max(1, T_TOTAL - T_OUT_START - 1.5)

    frames = []
    for i in range(n):
        t = i / FPS
        img = base()
        d = ImageDraw.Draw(img)
        terminal_chrome(d, TX, TY, TW, TH, "run_claude_subagent.py — smoke-test")

        y = CONTENT_TOP + 8

        # ── cmd line 1 (typewriter) ──────────────────────────────────────────
        if t < T_CMD1_END:
            shown = max(2, int(len(CMD1) * t / T_CMD1_END))
            partial = CMD1[:shown]
            d.text((TX+LPAD, y), "$ ", font=M20, fill=ACCENT)
            off = text_w(d, "$ ", M20)
            d.text((TX+LPAD+off, y), partial[2:], font=M20, fill=FG)
            if int(t * 2) % 2 == 0:
                cx = TX+LPAD + text_w(d, partial, M20)
                d.rectangle([cx+2, y+2, cx+13, y+LINE_H-3], fill=FG)
        else:
            d.text((TX+LPAD, y), "$ ", font=M20, fill=ACCENT)
            off = text_w(d, "$ ", M20)
            d.text((TX+LPAD+off, y), CMD1[2:], font=M20, fill=FG)
            y += LINE_H

            # ── cmd line 2 (typewriter) ──────────────────────────────────────
            if t < T_CMD2_END:
                p2 = max(0, t - T_CMD2_START)
                shown2 = int(len(CMD2) * p2 / (T_CMD2_END - T_CMD2_START))
                partial2 = CMD2[:shown2]
                d.text((TX+LPAD, y), partial2, font=M20, fill=MUTED)
                if int(t * 2) % 2 == 0:
                    cx2 = TX+LPAD + text_w(d, partial2, M20)
                    d.rectangle([cx2+2, y+2, cx2+13, y+LINE_H-3], fill=FG)
            else:
                d.text((TX+LPAD, y), CMD2, font=M20, fill=MUTED)
                y += LINE_H

                # ── output scroll ─────────────────────────────────────────────
                if t > T_OUT_START:
                    lines_vis = min(int((t - T_OUT_START) * lines_per_sec), len(OUT))
                    max_vis   = (TH - 60) // LINE_H - 4
                    start     = max(0, lines_vis - max_vis)

                    for li in range(start, lines_vis):
                        txt, col = OUT[li]
                        if txt:
                            d.text((TX+LPAD, y), txt, font=M18, fill=col)
                        y += LINE_H

        frames.append(img)
    return frames


# ── Scene 3: Ledger ──────────────────────────────────────────────────────────
def scene_ledger(n):
    M20 = F(MONO, 20)
    M18 = F(MONO, 18)
    SB  = F(SANS_B, 24)
    S18 = F(SANS, 18)

    TX, TY = 100, 80
    TW, TH = W - 200, H - 160
    LPAD = 36
    LINE_H = 32

    # (indent_chars, text, key_color, val_color)
    LINES = [
        (0,  "{",                               FG,    FG),
        (1,  '"runs": [',                       MUTED, FG),
        (2,  "{",                               FG,    FG),
        (3,  '"task_id":      "smoke-test",',   MUTED, ACCENT),
        (3,  '"status":       "complete",',     MUTED, SUCCESS),
        (3,  '"returncode":   0,',              MUTED, SUCCESS),
        (3,  '"session_id":   "3a89fee7-89e5-4349-81f4-d33eeb91cfe7",', MUTED, FG),
        (3,  '"write_scope":  [],',             MUTED, FG),
        (3,  '"started_at":   "2026-06-19T15:54:41Z",', MUTED, MUTED),
        (3,  '"finished_at":  "2026-06-19T15:58:01Z",', MUTED, MUTED),
        (3,  '"summary_path": ".agent-runs/claude/smoke-test.summary.md",', MUTED, ACCENT),
        (3,  '"log_path":     ".agent-runs/claude/smoke-test.jsonl"',  MUTED, MUTED),
        (2,  "}",                               FG,    FG),
        (1,  "]",                               FG,    FG),
        (0,  "}",                               FG,    FG),
    ]

    frames = []
    for i in range(n):
        t = i / FPS
        img = base()
        d = ImageDraw.Draw(img)
        terminal_chrome(d, TX, TY, TW, TH, ".agent-runs/claude/ledger.json")

        # caption
        a_cap = fade(t, 0.0, 0.5)
        d.text((TX+LPAD, TY+52), "Every run is indexed. Session IDs survive restarts.", font=S18, fill=alpha_color(MUTED, a_cap))

        y = TY + 88
        lines_vis = min(int(t * 2.8) + 1, len(LINES))

        for li in range(lines_vis):
            indent, raw, kc, vc = LINES[li]
            a = fade(t, li * 0.36, 0.3)
            ix = TX + LPAD + indent * 22

            # Split on first ": " to colorize key vs value
            if '": ' in raw:
                k, v = raw.split('": ', 1)
                k += '": '
                d.text((ix, y), k, font=M20, fill=alpha_color(kc, a))
                kw = text_w(d, k, M20)
                d.text((ix+kw, y), v, font=M20, fill=alpha_color(vc, a))
            else:
                d.text((ix, y), raw, font=M20, fill=alpha_color(FG, a))
            y += LINE_H

        frames.append(img)
    return frames


# ── Scene 4: Summary / audit findings ────────────────────────────────────────
def scene_summary(n):
    M18 = F(MONO, 18)
    M16 = F(MONO, 16)
    SB  = F(SANS_B, 22)
    S16 = F(SANS, 16)

    TX, TY = 100, 70
    TW, TH = W - 200, H - 140
    LPAD = 32
    LINE_H = 29

    OUTCOME = (
        "No hardcoded secrets or dangerous shell patterns found; "
        "two medium-severity issues warrant attention before wider distribution."
    )

    FINDINGS = [
        ("MEDIUM", "run_claude_subagent.py:67",
         "task_id path-traversal not blocked — ../ escapes .agent-runs/claude/"),
        ("MEDIUM", "run_claude_subagent.py:54",
         "--permission-mode no choices= guard; bypassPermissions not rejected"),
        ("LOW",    "run_claude_subagent.py:133",
         "Prompt injection trust boundary — expected by design, needs docs"),
    ]

    CHECKS = [
        "[ok]  No hardcoded secrets   (grepped api_key, sk-, bearer, token -- 0 hits)",
        "[ok]  No shell=True anywhere",
        "[ok]  No world-writable files",
        "[ok]  Logs are gitignored   (.agent-runs/ in .gitignore)",
        "[ok]  Stdlib-only -- no CVE surface from dependencies",
    ]

    frames = []
    for i in range(n):
        t = i / FPS
        img = base()
        d = ImageDraw.Draw(img)
        terminal_chrome(d, TX, TY, TW, TH, ".agent-runs/claude/smoke-test.summary.md")

        y = TY + 52

        # Outcome
        a1 = fade(t, 0.2, 0.5)
        d.text((TX+LPAD, y), "## Outcome", font=SB, fill=alpha_color(ACCENT, a1))
        y += 34
        for line in textwrap.wrap(OUTCOME, width=105):
            d.text((TX+LPAD, y), line, font=M18, fill=alpha_color(FG, a1))
            y += LINE_H - 2
        y += 12

        # Findings header
        a2 = fade(t, 1.0, 0.4)
        d.text((TX+LPAD, y), "## Findings", font=SB, fill=alpha_color(ACCENT, a2))
        y += 34

        # Table header
        d.text((TX+LPAD, y), f"{'Severity':<9}  {'Location':<37}  Issue", font=M18, fill=alpha_color(MUTED, a2))
        y += 6
        bar_a = fade(t, 1.1, 0.3)
        if bar_a > 0:
            d.rectangle([TX+LPAD, y, TX+TW-LPAD, y+1], fill=alpha_color(MUTED, bar_a))
        y += 14

        for fi, (sev, loc, issue) in enumerate(FINDINGS):
            row_a = fade(t, 1.5 + fi * 0.55, 0.35)
            sev_col = ERR if "MEDIUM" in sev else MUTED
            d.text((TX+LPAD, y), f"{sev:<9}", font=M18, fill=alpha_color(sev_col, row_a))
            d.text((TX+LPAD + text_w(d, "MEDIUM    ", M18), y), f"  {loc:<37}", font=M18, fill=alpha_color(MUTED, row_a))
            # issue may wrap
            issue_x = TX+LPAD + text_w(d, f"MEDIUM      {loc:<37}  ", M18)
            wrapped = textwrap.wrap(issue, width=40)
            for wl in wrapped:
                d.text((issue_x, y), wl, font=M18, fill=alpha_color(FG, row_a))
                y += LINE_H
        y += 12

        # Checks
        a3 = fade(t, 3.2, 0.4)
        d.text((TX+LPAD, y), "## Verification", font=SB, fill=alpha_color(ACCENT, a3))
        y += 34
        for ci, chk in enumerate(CHECKS):
            ca = fade(t, 3.6 + ci * 0.3, 0.25)
            if ca > 0:
                # chk already includes "[ok]  " prefix
                mark = chk[:6]
                rest = chk[6:]
                d.text((TX+LPAD, y), mark, font=M18, fill=alpha_color(SUCCESS, ca))
                d.text((TX+LPAD + text_w(d, mark, M18), y), rest, font=M18, fill=alpha_color(FG, ca))
                y += LINE_H

        frames.append(img)
    return frames


# ── Scene 5: End card ─────────────────────────────────────────────────────────
def scene_endcard(n):
    SB60 = F(SANS_B, 64)
    S28  = F(SANS,   28)
    M22  = F(MONO,   22)
    M16  = F(MONO,   16)

    install = "cp -R skills/claude-subagents ~/.codex/skills/"

    frames = []
    for i in range(n):
        t = i / FPS
        img = base()
        d = ImageDraw.Draw(img)

        a1 = fade(t, 0.0, 0.6)
        a2 = fade(t, 0.9, 0.5)
        a3 = fade(t, 1.8, 0.5)
        a4 = fade(t, 2.4, 0.4)

        # accent bar
        if a1 > 0:
            bw = int(280 * a1)
            bx = (W - bw) // 2
            d.rectangle([bx, H//2 - 92, bx + bw, H//2 - 87], fill=alpha_color(ACCENT, a1))

        centered(d, "codex-claude-subagents", H//2 - 74, SB60, alpha_color(FG, a1))
        centered(d, "github.com/jnopareboateng/codex-claude-subagents", H//2 + 12, S28, alpha_color(MUTED, a2))

        if a3 > 0:
            iw = text_w(d, install, M22) + 48
            ix = (W - iw) // 2
            iy = H//2 + 66
            d.rounded_rectangle([ix, iy, ix+iw, iy+52], radius=8, fill=alpha_color(PANEL2, a3))
            d.text((ix+24, iy+13), install, font=M22, fill=alpha_color(ACCENT, a3))

        if a4 > 0:
            hint = "Restart Codex after installing — skills are discovered at session start."
            centered(d, hint, H//2 + 138, M16, alpha_color(MUTED, a4))

        frames.append(img)
    return frames


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    for f in FRAMES_DIR.glob("frame_*.png"):
        f.unlink()

    SCENES = [
        (int(4.5 * FPS), scene_title,    "Title"),
        (int(20 * FPS),  scene_terminal, "Terminal demo"),
        (int(11 * FPS),  scene_ledger,   "Ledger"),
        (int(13 * FPS),  scene_summary,  "Summary/findings"),
        (int(7  * FPS),  scene_endcard,  "End card"),
    ]

    idx = 0
    for n, fn, label in SCENES:
        print(f"  [{label}] {n} frames...", end=" ", flush=True)
        for img in fn(n):
            img.save(FRAMES_DIR / f"frame_{idx:05d}.png")
            idx += 1
        print(f"done  (total {idx})")

    RENDERS.mkdir(exist_ok=True)
    out = RENDERS / "codex-claude-subagents-demo-v2.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(FRAMES_DIR / "frame_%05d.png"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-preset", "slow",
        "-crf", "18",
        str(out),
    ]
    print(f"\nffmpeg → {out}")
    subprocess.run(cmd, check=True, capture_output=True)
    size = out.stat().st_size // 1024
    print(f"✓  {size} KB   {idx/FPS:.1f}s  {idx} frames")


if __name__ == "__main__":
    main()
