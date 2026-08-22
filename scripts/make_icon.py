"""Generate the Bellwether app icon.

The same mark the sidebar uses: a dark red rounded square with a white serif B.
Rendered at every size Windows asks for, so the shortcut, the taskbar and the
alt-tab switcher all stay sharp instead of scaling one bitmap.

    python -m scripts.make_icon
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "bellwether.ico"
RED = (166, 50, 50, 255)
SIZES = [16, 24, 32, 48, 64, 128, 256]

FONTS = [r"C:\Windows\Fonts\georgiab.ttf", r"C:\Windows\Fonts\georgia.ttf",
         r"C:\Windows\Fonts\timesbd.ttf"]


def font_for(px: int) -> ImageFont.FreeTypeFont:
    for path in FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, px)
    return ImageFont.load_default()


def one(size: int) -> Image.Image:
    # Draw at 4x and downsample: rounded corners and a serif letter both alias
    # badly at 16px otherwise.
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=RED)
    f = font_for(int(s * 0.62))
    box = d.textbbox((0, 0), "B", font=f)
    d.text(((s - box[2] - box[0]) / 2, (s - box[3] - box[1]) / 2 - s * 0.02),
           "B", font=f, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Save from the LARGEST render and let the ICO writer derive the rest. It
    # ignores append_images and resizes the base image, so handing it the 16px
    # version silently produces a one entry, permanently blurry icon.
    base = one(max(SIZES))
    base.save(OUT, format="ICO", sizes=[(s, s) for s in SIZES])
    got = sorted(Image.open(OUT).ico.sizes())
    if len(got) != len(SIZES):
        raise SystemExit(f"expected {len(SIZES)} icon sizes, file holds {got}")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes {got})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
