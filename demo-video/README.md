# codex-claude-subagents Demo Video Package

This folder contains the source for the demo video.

## Outputs

- `renders/codex-claude-subagents-demo.mp4` - final rendered video
- `renders/codex-claude-subagents-demo.zip` - shareable package
- `frames/` - rendered title/proof slides
- `narration/voiceover.wav` - generated narration when Windows SAPI is available

## Build

Run from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File demo-video/scripts/build_demo.ps1
```

The build uses Windows PowerShell, .NET drawing APIs, optional Windows SAPI narration, and FFmpeg.
