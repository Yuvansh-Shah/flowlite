"""Generate the flowlite app icon (icons/flowlite.ico + .png).
Run once: python make_icon.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent / "icons"
OUT_DIR.mkdir(exist_ok=True)


def render(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Rounded-square dark background with a subtle teal.
    pad = int(size * 0.06)
    radius = int(size * 0.24)
    d.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=radius,
        fill=(18, 20, 24, 255),
    )
    # Sound-wave bars (the "flow" mark), teal/green gradient-ish.
    bar_w = max(2, int(size * 0.07))
    gap = max(2, int(size * 0.055))
    n = 5
    total = n * bar_w + (n - 1) * gap
    x0 = (size - total) / 2
    cy = size / 2
    # heights as fractions of half-height
    fracs = [0.30, 0.62, 0.92, 0.55, 0.38]
    colors = [
        (54, 211, 153),
        (45, 191, 168),
        (34, 197, 211),
        (45, 191, 168),
        (54, 211, 153),
    ]
    for i in range(n):
        x = x0 + i * (bar_w + gap)
        h = fracs[i] * (size * 0.40)
        d.rounded_rectangle(
            [x, cy - h, x + bar_w, cy + h],
            radius=bar_w / 2,
            fill=colors[i] + (255,),
        )
    return img


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [render(s) for s in sizes]
    ico_path = OUT_DIR / "flowlite.ico"
    imgs[-1].save(ico_path, format="ICO", sizes=[(s, s) for s in sizes])
    imgs[-1].save(OUT_DIR / "flowlite.png", format="PNG")
    print(f"wrote {ico_path}")
    print(f"wrote {OUT_DIR / 'flowlite.png'}")


if __name__ == "__main__":
    main()
