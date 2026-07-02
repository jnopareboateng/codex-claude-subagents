# codex-claude-subagents Demo Video Package

This folder contains the source for the demo video.

## Outputs

- `renders/codex-claude-subagents-demo-v2.mp4` - final rendered video (silent; mux narration separately if needed)
- `narration/voiceover.wav` - generated narration when Windows SAPI is available

## Build

Cross-platform build (Python + Pillow + FFmpeg), run from `demo-video/`:

```bash
python3 -m venv /tmp/videnv2 && /tmp/videnv2/bin/pip install pillow
/tmp/videnv2/bin/python scripts/build_demo_v2.py
```

Fonts (Space Grotesk, Inter, JetBrains Mono — see `DESIGN.md`) are fetched
automatically into `assets/fonts/` on first run. Requires `ffmpeg` on `PATH`.

### Legacy Windows build

`scripts/build_demo.ps1` is an older, Windows-only build (PowerShell, .NET
drawing APIs, optional SAPI narration). Kept for reference; `build_demo_v2.py`
is the maintained path.
