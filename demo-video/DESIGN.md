## Style Prompt

Technical, credible, and restrained. The video should feel like a serious developer tool demo, not a fake SaaS promo. Use real command output, ledger fields, and summary excerpts as the visual proof.

## Colors

- Background: `#0b1020`
- Panel: `#121a2f`
- Panel secondary: `#18233d`
- Foreground: `#e7edf7`
- Muted text: `#9fb0c8`
- Accent: `#e07a54` (warm clay)
- Success: `#67d391`

## Typography

- Headline: `Space Grotesk`, bold (700)
- Body/labels/captions: `Inter`, regular–medium (400–440), optical size matched to point size
- Code/terminal: `JetBrains Mono`, regular (400) with medium (500) for emphasis

All three are variable fonts (SIL OFL), fetched on demand into `assets/fonts/`
(gitignored) by `ensure_fonts()` in `build_demo_v2.py` — nothing to install by hand.

## Motion And Layout

- Ease-out cubic on every fade, paired with a small upward "settle" (element
  starts ~20px low, eases into place) — reads as considered, not mechanical.
- Title card carries a small tracked-caps eyebrow label above the headline,
  the way a product-launch title card would.
- Prioritize legible terminal proof over decorative animation.
- Use large type; no tiny CLI screenshots.
- Use one consistent dark canvas throughout.

## What NOT To Do

- No fake dashboard UI.
- No generic AI-agent stock visuals.
- No neon purple/cyan gradients.
- No claims that are not backed by README, runner help, or `.agent-runs`.
