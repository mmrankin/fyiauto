#!/usr/bin/env python3
"""Generate the 1200x630 Open Graph share image (static/og-default.png).

Brand palette from static/style.css: brand #1565d8, brand-ink #0b46a0,
accent #0fae8b. Run with the venv python: `python gen_og_image.py`.
"""
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BRAND = (21, 101, 216)       # #1565d8
BRAND_INK = (11, 70, 160)    # #0b46a0
DEEP = (8, 38, 92)           # darker top for depth
ACCENT = (15, 174, 139)      # #0fae8b
WHITE = (255, 255, 255)
SUBTLE = (214, 228, 248)
CHIP_BG = (240, 245, 252)

ARIAL_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "static", "og-default.png")


def font(path, size):
    return ImageFont.truetype(path, size)


def main():
    img = Image.new("RGB", (W, H), BRAND_INK)
    d = ImageDraw.Draw(img)

    # diagonal-ish gradient: deep navy (top-left) -> brand blue (bottom-right)
    for y in range(H):
        t = y / (H - 1)
        c = tuple(int(a + (b - a) * t) for a, b in zip(DEEP, BRAND))
        d.line([(0, y), (W, y)], fill=c)

    # accent geometry: a teal corner wedge (bottom-right) for visual interest
    d.polygon([(W, H), (W, H - 230), (W - 360, H)], fill=ACCENT)
    d.polygon([(W, H), (W, H - 120), (W - 190, H)], fill=(13, 150, 120))

    wf = font(ARIAL_BOLD, 158)
    tf = font(ARIAL, 44)
    cf = font(ARIAL_BOLD, 28)
    sf = font(ARIAL_BOLD, 26)

    # eyebrow label
    d.text((90, 70), "FYIAUTO.COM", font=sf, fill=ACCENT)

    # wordmark: "fyi" white + "Auto" teal, centered
    fyi, auto = "fyi", "Auto"
    w1 = d.textlength(fyi, font=wf)
    w2 = d.textlength(auto, font=wf)
    total = w1 + w2
    x = (W - total) / 2
    y = 200
    d.text((x, y), fyi, font=wf, fill=WHITE)
    d.text((x + w1, y), auto, font=wf, fill=ACCENT)

    # accent underline bar
    bar_w = 130
    bx = (W - bar_w) / 2
    by = y + 178
    d.rounded_rectangle([bx, by, bx + bar_w, by + 9], radius=4, fill=ACCENT)

    # tagline
    tag = "Search millions of used & new cars for sale"
    tw = d.textlength(tag, font=tf)
    d.text(((W - tw) / 2, by + 34), tag, font=tf, fill=SUBTLE)

    # feature chips
    chips = ["3M+ Listings", "AI-Powered Search", "Dealers Nationwide"]
    pad, gap, ch = 26, 18, 56
    widths = [d.textlength(c, font=cf) + pad * 2 for c in chips]
    row_w = sum(widths) + gap * (len(chips) - 1)
    cx = (W - row_w) / 2
    cy = 512
    for c, cw in zip(chips, widths):
        d.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=ch // 2, fill=CHIP_BG)
        tw = d.textlength(c, font=cf)
        d.text((cx + (cw - tw) / 2, cy + (ch - 34) / 2), c, font=cf, fill=BRAND_INK)
        cx += cw + gap

    img.save(OUT, "PNG", optimize=True)
    print("wrote %s (%dx%d, %d bytes)" % (OUT, W, H, os.path.getsize(OUT)))


if __name__ == "__main__":
    main()
