# Brand & visual assets

UNITARES inherits the CIRWEL house register rather than carrying a brand of its
own: cream paper, oxblood ink, hairline rules, and Bodoni Moda for display. The
mark is wordless so that it survives copy changes; every tagline lives in
markdown text, not in an image. Four hero banners between April and September
2026 each died when the sentence baked into them moved.

## The mark

`unitares-mark.svg` is the CIRWEL flat-top hexagon holding a didone capital U
cut from Bodoni Moda (OFL). The hex is the publisher's device already used on
cirwel.org; the U is the product initial, made the same way the site's
illuminated C is made. The lockup files add the wordmark "Unitares" in true
small caps beside it.

Text in every SVG is outlined into paths, because GitHub serves README images
through a proxy that loads no webfonts, and each has a light and a dark file
for a `<picture>` element. `unitares-favicon.svg` is a separate, heavier cut
(optical size 11, weight 800) with the same colours: Bodoni's hairline stem
vanishes below roughly 24 px.

## Palette

| Role | Light | Dark | Used for |
|---|---|---|---|
| Paper | `#F5F1E8` | `#0d1117` (GitHub) | card ground; README ground is GitHub's |
| Ink | `#1A1612` | `#F5F1E8` | wordmark, hex outline, body |
| Oxblood | `#7A1F1F` | `#F5F1E8` | the U; one accent, never decoration |
| Stone | `#5C544A` | | badge values, secondary text |
| Sepia | `#C9C0AE` | | hairline rules, dark-mode hex outline |

README badges use `labelColor=1A1612` with `5C544A` values and `7A1F1F` for the
DOI. No EISV colours appear on any brand surface; the four coordinates are a
readout, not the product.

## Social preview

`social-preview.png` (1280×640) is the card GitHub shows when the repo is linked
on X, LinkedIn, Slack, or Discord. **It is a repository setting, not part of the
README**: Settings → General → Social preview → Edit → Upload an image. It
carries the mark, the README's current tagline, the repo URL, the DOI, and the
start date. It carries no counts, because a count on a card is wrong the day
after it is rendered.

Regenerate after a copy or palette change, from `scripts/dev/brand/`:

```bash
python3 glyphs.py && python3 compose.py && python3 favicon_bold.py   # SVGs
python3 build_artboards.py                                           # canvas artboards
python3 render_social_preview.py                                     # PNG via headless Chrome
```

`glyphs.py` downloads Bodoni Moda from the google/fonts repository on first use
into the gitignored `fonts/` directory, licence alongside. The renderer needs
Google Chrome and Pillow; it is a maintainer tool, not part of any install path.

## Asset inventory

| File | What | Where it's used |
|---|---|---|
| `unitares-mark.svg`, `unitares-mark-dark.svg` | The hex-and-U mark, 200×200 | avatars, docs |
| `unitares-lockup.svg`, `unitares-lockup-dark.svg` | Mark plus wordmark, 493×96 | README header (`<picture>`) |
| `unitares-favicon.svg` | Heavier cut on a cream tile, 64×64 | `docs/public-site/favicon.svg` |
| `social-preview.png` | 1280×640 share card | repo social-preview setting |
| `dashboard-overview.png` | Overview — fleet, metrics, trust tiers, Pulse | Production snapshot |
| `dashboard-agents.png` | Agents — per-instance verdict/coherence/risk | Production snapshot |
| `dashboard-eisv.png` | EISV — live fleet trajectory charts | Production snapshot |
| `dashboard-discoveries.png` | Discoveries — shared knowledge graph | Production snapshot |
| `dashboard-activity.png` | Activity — filterable event log | Production snapshot |

The dashboard captures date from June 2026 and show the dashboard of that time;
they are evidence of what ran, not brand surfaces.
