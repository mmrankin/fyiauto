#!/usr/bin/env python3
"""Generate the site icons (static/favicon.png + apple-touch-icon.png) in the
fyiAuto brand style: a white car silhouette with teal wheels on a blue-gradient
rounded square. Run with the venv python: `python gen_icon.py`.
"""
import os
from PIL import Image, ImageDraw, ImageFont  # noqa: F401

BRAND = (21, 101, 216)
DEEP = (8, 38, 92)
ACCENT = (15, 174, 139)
WHITE = (255, 255, 255)
GLASS = (37, 116, 222)     # window tint (a touch lighter than the body's blue)
HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4   # supersample for crisp downscaling


def draw_car(img, S):
    """Head-on (front) view car: symmetric white fascia, tinted windshield,
    teal headlights, dark grille, two tires peeking at the lower corners."""
    d = ImageDraw.Draw(img)
    def U(v):
        return v * S
    def poly(pts, fill):
        d.polygon([(U(x), U(y)) for x, y in pts], fill=fill)

    # tires at the lower corners (drawn first, so the body sits over their tops)
    for cx in (0.275, 0.725):
        d.ellipse([U(cx - 0.085), U(0.62), U(cx + 0.085), U(0.79)], fill=DEEP)

    # roof + windshield (trapezoid, wider at the base)
    d.rounded_rectangle([U(0.37), U(0.255), U(0.63), U(0.315)], radius=U(0.03), fill=WHITE)
    poly([(0.385, 0.30), (0.615, 0.30), (0.665, 0.455), (0.335, 0.455)], WHITE)   # A-pillars/frame
    poly([(0.405, 0.315), (0.595, 0.315), (0.638, 0.44), (0.362, 0.44)], GLASS)   # glass

    # main front fascia (wide, rounded)
    d.rounded_rectangle([U(0.155), U(0.43), U(0.845), U(0.71)], radius=U(0.075), fill=WHITE)

    # headlights: two angled teal sweeps near the top corners of the fascia
    poly([(0.205, 0.485), (0.37, 0.475), (0.38, 0.55), (0.205, 0.55)], ACCENT)
    poly([(0.795, 0.485), (0.63, 0.475), (0.62, 0.55), (0.795, 0.55)], ACCENT)

    # grille (dark, centered) + a slim teal trim line below it
    d.rounded_rectangle([U(0.40), U(0.575), U(0.60), U(0.635)], radius=U(0.022), fill=DEEP)
    d.rounded_rectangle([U(0.30), U(0.665), U(0.70), U(0.69)], radius=U(0.012), fill=ACCENT)


def make_icon(size, rounded=True):
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    grad = Image.new("RGB", (S, S), BRAND)
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)],
                fill=tuple(int(a + (b - a) * t) for a, b in zip(DEEP, BRAND)))

    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    if rounded:
        md.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    else:
        md.rectangle([0, 0, S - 1, S - 1], fill=255)
    img.paste(grad, (0, 0), mask)

    draw_car(img, S)
    return img.resize((size, size), Image.LANCZOS)


def main():
    make_icon(64, rounded=True).save(
        os.path.join(HERE, "static", "favicon.png"), "PNG", optimize=True)
    at = make_icon(180, rounded=False)
    bg = Image.new("RGBA", (180, 180), BRAND + (255,))
    bg.alpha_composite(at)
    bg.convert("RGB").save(
        os.path.join(HERE, "static", "apple-touch-icon.png"), "PNG", optimize=True)
    for n in ("favicon.png", "apple-touch-icon.png"):
        p = os.path.join(HERE, "static", n)
        print("wrote %s (%d bytes)" % (p, os.path.getsize(p)))


if __name__ == "__main__":
    main()
