"""Generate the ANP2 logo set.

Concept: three nodes (agents) on a horizontal triangle with signed-event
edges between them, plus the wordmark "ANP2" with a tiny superscript 2.
Deliberately minimal (JP-redacted) readable at 256x256 avatar size and at 32x32 favicon
size. No gradients (compresses cleanly to PNG).

Outputs:
  - logo_512.png  (JP-redacted) full color, dark background (Reddit/HF avatar)
  - logo_512_light.png (JP-redacted) same on light background
  - logo_favicon_64.png (JP-redacted) square favicon
  - logo.svg (JP-redacted) vector source
"""
from __future__ import annotations
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
OUT.mkdir(exist_ok=True)

# Brand colors
BG_DARK = (12, 17, 28)        # near-black
FG = (220, 230, 240)          # off-white
ACCENT = (110, 220, 200)      # soft teal (signed events / cryptographic)

def find_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Supplemental/Futura.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Black.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def make(size: int, bg, fg, accent, *, include_text: bool = True, favicon: bool = False) -> Image.Image:
    img = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(img, "RGBA")

    cx, cy = size / 2, size / 2 * (0.85 if include_text else 1.0)
    r_outer = size * 0.30
    node_r = size * 0.075 if not favicon else size * 0.13

    # Three node positions: equilateral triangle
    angles = [-math.pi / 2, math.pi / 2 - 2 * math.pi / 3, math.pi / 2 + 2 * math.pi / 3]
    nodes = [(cx + r_outer * math.cos(a), cy + r_outer * math.sin(a)) for a in angles]

    # Edges first (signed-event connections)
    edge_w = max(2, int(size * 0.012))
    for i in range(3):
        for j in range(i + 1, 3):
            d.line([nodes[i], nodes[j]], fill=accent + (255,), width=edge_w)

    # Nodes on top
    for (x, y) in nodes:
        d.ellipse([x - node_r, y - node_r, x + node_r, y + node_r],
                  fill=fg, outline=accent, width=max(2, int(size * 0.01)))

    # Centerpoint glyph (relay)
    relay_r = size * 0.045 if not favicon else size * 0.07
    d.ellipse([cx - relay_r, cy - relay_r, cx + relay_r, cy + relay_r],
              fill=accent, outline=None)

    if include_text:
        # Wordmark "ANP2" (JP-redacted) uppercase, bold, tracked
        text_size = int(size * 0.18)
        f = find_font(text_size)
        text = "ANP2"
        bbox = d.textbbox((0, 0), text, font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = (size - tw) / 2 - bbox[0]
        ty = size * 0.72 - bbox[1]
        d.text((tx, ty), text, fill=fg, font=f)

    return img


def main() -> None:
    main_logo = make(512, BG_DARK, FG, ACCENT)
    main_logo.save(OUT / "logo_512.png", optimize=True)

    light = make(512, (250, 250, 250), (12, 17, 28), (40, 140, 130))
    light.save(OUT / "logo_512_light.png", optimize=True)

    favicon = make(64, BG_DARK, FG, ACCENT, include_text=False, favicon=True)
    favicon.save(OUT / "logo_favicon_64.png", optimize=True)

    # Also 256 for Reddit (their crop is square)
    reddit = make(256, BG_DARK, FG, ACCENT)
    reddit.save(OUT / "logo_reddit_256.png", optimize=True)

    print("Wrote:", *[p.name for p in OUT.glob("logo_*.png")])


if __name__ == "__main__":
    main()
