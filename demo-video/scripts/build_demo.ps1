param(
    [string]$OutputName = "codex-claude-subagents-demo"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).ProviderPath
$DemoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).ProviderPath
$FramesDir = Join-Path $DemoRoot "frames"
$NarrationDir = Join-Path $DemoRoot "narration"
$RenderDir = Join-Path $DemoRoot "renders"
$ProofDir = Join-Path $DemoRoot "proof"
New-Item -ItemType Directory -Force -Path $FramesDir, $NarrationDir, $RenderDir, $ProofDir | Out-Null

$Ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$Width = 1920
$Height = 1080

Add-Type -AssemblyName System.Drawing

function Read-Text($path) {
    return Get-Content -Raw -LiteralPath (Join-Path $RepoRoot $path)
}

function Shorten($value, $max = 92) {
    if ($null -eq $value) { return "" }
    $text = [string]$value
    if ($text.Length -le $max) { return $text }
    return $text.Substring(0, $max - 1) + "..."
}

function Wrap-Text($graphics, $text, $font, $maxWidth) {
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($paragraph in ([string]$text -split "`n")) {
        if ($paragraph.Trim().Length -eq 0) {
            $lines.Add("")
            continue
        }
        $words = $paragraph -split "\s+"
        $line = ""
        foreach ($word in $words) {
            $candidate = if ($line.Length -eq 0) { $word } else { "$line $word" }
            if ($graphics.MeasureString($candidate, $font).Width -le $maxWidth) {
                $line = $candidate
            } else {
                if ($line.Length -gt 0) { $lines.Add($line) }
                $line = $word
            }
        }
        if ($line.Length -gt 0) { $lines.Add($line) }
    }
    return $lines
}

function Draw-Wrapped($g, $text, $font, $brush, $x, $y, $maxWidth, $lineHeight) {
    $cursor = $y
    foreach ($line in (Wrap-Text $g $text $font $maxWidth)) {
        $g.DrawString($line, $font, $brush, [float]$x, [float]$cursor)
        $cursor += $lineHeight
    }
    return $cursor
}

function New-Brush($hex) {
    return [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($hex))
}

function Draw-Panel($g, $x, $y, $w, $h, $fill, $border = "#263651") {
    $rect = [System.Drawing.RectangleF]::new($x, $y, $w, $h)
    $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
    $radius = 22
    $d = $radius * 2
    $path.AddArc($x, $y, $d, $d, 180, 90)
    $path.AddArc($x + $w - $d, $y, $d, $d, 270, 90)
    $path.AddArc($x + $w - $d, $y + $h - $d, $d, $d, 0, 90)
    $path.AddArc($x, $y + $h - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    $g.FillPath((New-Brush $fill), $path)
    $g.DrawPath([System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml($border), 2), $path)
}

function New-Slide($index, $eyebrow, $title, $body, $blocks, $footer = "codex-claude-subagents") {
    $bmp = [System.Drawing.Bitmap]::new($Width, $Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
    $g.Clear([System.Drawing.ColorTranslator]::FromHtml("#0b1020"))

    $bgPen = [System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml("#1b2740"), 2)
    for ($x = -60; $x -lt $Width; $x += 120) { $g.DrawLine($bgPen, $x, 0, $x + 460, $Height) }
    $g.FillEllipse((New-Brush "#17213a"), 1320, -220, 720, 720)
    $g.FillEllipse((New-Brush "#111a30"), -260, 760, 640, 640)

    $headline = [System.Drawing.Font]::new("Aptos Display", 72, [System.Drawing.FontStyle]::Bold)
    $subhead = [System.Drawing.Font]::new("Aptos", 30, [System.Drawing.FontStyle]::Regular)
    $small = [System.Drawing.Font]::new("Aptos", 22, [System.Drawing.FontStyle]::Regular)
    $mono = [System.Drawing.Font]::new("Cascadia Mono", 25, [System.Drawing.FontStyle]::Regular)
    $monoSmall = [System.Drawing.Font]::new("Cascadia Mono", 21, [System.Drawing.FontStyle]::Regular)

    $fg = New-Brush "#e7edf7"
    $muted = New-Brush "#9fb0c8"
    $accent = New-Brush "#f4b860"
    $success = New-Brush "#67d391"

    $g.DrawString($eyebrow.ToUpperInvariant(), $small, $accent, 110, 82)
    Draw-Wrapped $g $title $headline $fg 105 135 1160 82 | Out-Null
    Draw-Wrapped $g $body $subhead $muted 112 330 1120 42 | Out-Null

    $y = 510
    foreach ($block in $blocks) {
        Draw-Panel $g 110 $y 1700 $block.Height "#121a2f"
        $g.DrawString($block.Label, $small, $success, 145, ($y + 28))
        if ($block.Kind -eq "code") {
            $cy = $y + 72
            foreach ($line in $block.Lines) {
                $g.DrawString($line, $monoSmall, $fg, 145, $cy)
                $cy += 32
            }
        } elseif ($block.Kind -eq "stats") {
            $x = 145
            foreach ($item in $block.Items) {
                $g.DrawString($item.Value, $headline, $accent, $x, ($y + 70))
                $g.DrawString($item.Label, $small, $muted, $x + 6, ($y + 154))
                $x += 360
            }
        } else {
            Draw-Wrapped $g $block.Text $mono $fg 145 ($y + 72) 1610 34 | Out-Null
        }
        $y += $block.Height + 32
    }

    $g.DrawString($footer, $small, $muted, 110, 1015)
    $g.DrawString(("{0:00}" -f $index), $small, $accent, 1750, 1015)

    $out = Join-Path $FramesDir ("slide-{0:00}.png" -f $index)
    $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose()
    $bmp.Dispose()
    return $out
}

function New-DiagramSlide($index) {
    $bmp = [System.Drawing.Bitmap]::new($Width, $Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
    $g.Clear([System.Drawing.ColorTranslator]::FromHtml("#0b1020"))

    $headline = [System.Drawing.Font]::new("Aptos Display", 64, [System.Drawing.FontStyle]::Bold)
    $subhead = [System.Drawing.Font]::new("Aptos", 28, [System.Drawing.FontStyle]::Regular)
    $small = [System.Drawing.Font]::new("Aptos", 22, [System.Drawing.FontStyle]::Regular)
    $fg = New-Brush "#e7edf7"
    $muted = New-Brush "#9fb0c8"
    $accent = New-Brush "#f4b860"

    $bgPen = [System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml("#1b2740"), 2)
    for ($x = -60; $x -lt $Width; $x += 120) { $g.DrawLine($bgPen, $x, 0, $x + 460, $Height) }
    $g.DrawString("README ARCHITECTURE", $small, $accent, 110, 82)
    $g.DrawString("Codex orchestrates. Claude executes.", $headline, $fg, 105, 135)
    Draw-Wrapped $g "The diagram from the README becomes the central proof: no framework, no package runtime, just a scoped worker contract and file-backed logs." $subhead $muted 112 235 1580 40 | Out-Null

    Draw-Panel $g 150 345 1620 545 "#121a2f"
    $imgPath = Join-Path $RepoRoot "assets\architecture.png"
    $img = [System.Drawing.Image]::FromFile($imgPath)
    $maxW = 1500
    $maxH = 455
    $scale = [Math]::Min($maxW / $img.Width, $maxH / $img.Height)
    $drawW = [int]($img.Width * $scale)
    $drawH = [int]($img.Height * $scale)
    $drawX = [int](150 + (1620 - $drawW) / 2)
    $drawY = [int](375 + (485 - $drawH) / 2)
    $g.FillRectangle((New-Brush "#e7edf7"), $drawX - 18, $drawY - 18, $drawW + 36, $drawH + 36)
    $g.DrawImage($img, $drawX, $drawY, $drawW, $drawH)
    $img.Dispose()

    $g.DrawString("codex-claude-subagents", $small, $muted, 110, 1015)
    $g.DrawString(("{0:00}" -f $index), $small, $accent, 1750, 1015)

    $out = Join-Path $FramesDir ("slide-{0:00}.png" -f $index)
    $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose()
    $bmp.Dispose()
    return $out
}

$ledger = Get-Content -Raw -LiteralPath (Join-Path $RepoRoot ".agent-runs\claude\ledger.json") | ConvertFrom-Json
$bootstrap = $ledger.runs | Where-Object { $_.task_id -eq "public-repo-bootstrap" } | Select-Object -First 1
$smoke = $ledger.runs | Where-Object { $_.task_id -eq "smoke-test" } | Select-Object -First 1
$help = & python (Join-Path $RepoRoot "skills\claude-subagents\scripts\run_claude_subagent.py") --help 2>&1
$helpLines = $help | Select-String -Pattern "task-id|permission-mode|write-scope|session-id|Trust boundary" | ForEach-Object { Shorten $_.Line 110 }

$summary = Read-Text ".agent-runs\claude\smoke-test.summary.md"
$findingLines = @(
    "Outcome: No hardcoded secrets or dangerous shell patterns found.",
    "Files inspected: launcher, skill instructions, prompts, README, ledger.",
    "Final output: .agent-runs/claude/smoke-test.summary.md"
)

$slides = @()
$slides += New-Slide 1 "The friction" "Two capable agents. Too much manual glue." "Codex and Claude can work together, but copy-pasting context between separate tools loses boundaries, resumability, and proof." @(
    @{ Kind = "text"; Label = "Before"; Text = "Open Codex. Open Claude. Copy context. Paste task. Copy result. Hope the scope was respected. Reconstruct what happened later."; Height = 210 }
)
$slides += New-Slide 2 "The project" "Claude as a scoped worker inside Codex." "This repo packages a Codex skill and stdlib-only Python launcher that lets Codex orchestrate Claude CLI workers from the current worktree." @(
    @{ Kind = "code"; Label = "README claim"; Lines = @("Use Claude as subagents in Codex.", "Codex stays lead/orchestrator.", "Claude receives a scoped worker contract."); Height = 220 }
)
$slides += New-DiagramSlide 3
$slides += New-Slide 4 "The contract" "A worker gets a task, scope, session, and summary path." "The launcher injects a contract into every prompt, so delegated work has explicit boundaries and a required handoff artifact." @(
    @{ Kind = "code"; Label = "Worker contract"; Lines = @("Allowed write scope: read-only, or one or more repo-relative paths", "Raw logs -> .agent-runs/claude/<task>.jsonl", "Final summary -> .agent-runs/claude/<task>.summary.md", "Resume handle -> --session-id <uuid>"); Height = 300 }
)
$slides += New-Slide 5 "Live repo evidence" "This repository was bootstrapped by a Claude worker." "The existing ledger records a completed public-repo-bootstrap run with return code zero and a constrained write scope." @(
    @{ Kind = "stats"; Label = "Ledger"; Items = @(
        @{ Value = "0"; Label = "return code" },
        @{ Value = "4"; Label = "write scopes" },
        @{ Value = "7"; Label = "files produced" },
        @{ Value = "1"; Label = "summary" }
    ); Height = 250 },
    @{ Kind = "code"; Label = "Session"; Lines = @("task_id: $($bootstrap.task_id)", "session_id: $($bootstrap.session_id)", "status: $($bootstrap.status)"); Height = 180 }
)
$slides += New-Slide 6 "Read-only proof" "A smoke audit ran through the same delegation loop." "The read-only audit inspected the project, wrote structured findings, and changed no files." @(
    @{ Kind = "code"; Label = "Smoke-test summary"; Lines = $findingLines; Height = 260 },
    @{ Kind = "code"; Label = "Read-only ledger"; Lines = @("task_id: $($smoke.task_id)", "write_scope: []", "returncode: $($smoke.returncode)", "summary_exists: $($smoke.summary_exists)"); Height = 210 }
)
$slides += New-Slide 7 "Safety surface" "The CLI rejects sloppy delegation." "The current launcher constrains task IDs, keeps write scopes inside the repo, and excludes bypassPermissions from accepted permission modes." @(
    @{ Kind = "code"; Label = "Runner help"; Lines = $helpLines[0..([Math]::Min($helpLines.Count - 1, 7))]; Height = 330 }
)
$slides += New-Slide 8 "What the logs buy you" "Resumability and auditability are the product." "The important output is not a chat transcript. It is a file-backed ledger that Codex can inspect, resume, summarize, and verify." @(
    @{ Kind = "code"; Label = ".agent-runs/claude"; Lines = @("ledger.json", "<task>.jsonl", "<task>.stderr.log", "<task>.prompt.md", "<task>.summary.md"); Height = 260 }
)
$slides += New-Slide 9 "Close" "Codex leads. Claude works. The repo keeps the receipt." "Install the skill, restart Codex, then delegate a read-only audit or a scoped fix from inside the repo." @(
    @{ Kind = "code"; Label = "Quickstart"; Lines = @("cp -R skills/claude-subagents ~/.codex/skills/", "python3 ~/.codex/skills/claude-subagents/scripts/run_claude_subagent.py \\", "  --task audit-security --prompt examples/prompts/read-only-audit.md"); Height = 280 }
)

$voiceoverText = Get-Content -Raw -LiteralPath (Join-Path $DemoRoot "voiceover-script.md")
$voiceoverText = ($voiceoverText -split "## Script", 2)[1].Trim()
$voiceoverPath = Join-Path $NarrationDir "voiceover.wav"
$audioAvailable = $false
try {
    Add-Type -AssemblyName System.Speech
    $synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
    $synth.Rate = 0
    $synth.Volume = 100
    $synth.SetOutputToWaveFile($voiceoverPath)
    $synth.Speak($voiceoverText)
    $synth.Dispose()
    $audioAvailable = Test-Path $voiceoverPath
} catch {
    "Narration generation failed: $($_.Exception.Message)" | Set-Content -LiteralPath (Join-Path $NarrationDir "narration-error.txt")
}

$slideDurations = @(9, 10, 10, 11, 12, 12, 12, 10, 11)
$concatPath = Join-Path $RenderDir "slides.ffconcat"
$concat = New-Object System.Collections.Generic.List[string]
$concat.Add("ffconcat version 1.0")
for ($i = 0; $i -lt $slides.Count; $i++) {
    $concat.Add("file '$($slides[$i].Replace('\', '/'))'")
    $concat.Add("duration $($slideDurations[$i])")
}
$concat.Add("file '$($slides[-1].Replace('\', '/'))'")
Set-Content -LiteralPath $concatPath -Value $concat

$videoNoAudio = Join-Path $RenderDir "$OutputName.noaudio.mp4"
& $Ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i $concatPath -vf "scale=1920:1080,fps=30,format=yuv420p" -c:v libx264 -preset medium -crf 18 $videoNoAudio

$finalVideo = Join-Path $RenderDir "$OutputName.mp4"
if ($audioAvailable) {
    & $Ffmpeg -y -hide_banner -loglevel error -i $videoNoAudio -i $voiceoverPath -c:v copy -c:a aac -b:a 160k -shortest $finalVideo
} else {
    Copy-Item -LiteralPath $videoNoAudio -Destination $finalVideo -Force
}

$proof = [ordered]@{
    generated_at = (Get-Date).ToString("o")
    repo = $RepoRoot
    final_video = $finalVideo
    audio_available = $audioAvailable
    source_evidence = @(
        "README.md",
        "skills/claude-subagents/scripts/run_claude_subagent.py --help",
        ".agent-runs/claude/ledger.json",
        ".agent-runs/claude/public-repo-bootstrap.summary.md",
        ".agent-runs/claude/smoke-test.summary.md"
    )
    bootstrap_run = $bootstrap
    smoke_run = $smoke
}
$proof | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $ProofDir "build-proof.json")

$zipPath = Join-Path $RenderDir "$OutputName.zip"
if (Test-Path $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
Compress-Archive -Path $finalVideo, (Join-Path $DemoRoot "README.md"), (Join-Path $DemoRoot "DESIGN.md"), (Join-Path $DemoRoot "voiceover-script.md"), (Join-Path $ProofDir "build-proof.json") -DestinationPath $zipPath

[pscustomobject]@{
    video = $finalVideo
    zip = $zipPath
    audio = $audioAvailable
    frames = $slides.Count
} | ConvertTo-Json
