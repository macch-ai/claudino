#!/usr/bin/env python3
"""Draw claudino's animated step emoji: one 128x128 pixel-art GIF per kind of step.

The live step message starts with one of these (see `steps.py`). Upload each GIF to the workspace as
a custom emoji named after its file (`claudino-read`, `claudino-search`, ...). Without them claudino
falls back to standard emoji.

    uv run --with pillow scripts/make-step-emoji.py docs/emoji
"""

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

GRID, SCALE = 32, 4  # drawn on a 32x32 grid, scaled up 4x with hard pixel edges
FRAMES, FRAME_MS = 16, 100  # 1.6 s loops at 10 fps, under Slack's emoji limits
ORANGE, LIGHT, DARK, GREY, WHITE = (230, 121, 78), (246, 178, 140), (40, 40, 44), (120, 120, 128), (250, 250, 250)
KEY = (255, 0, 255)  # transparent color


def canvas():
    im = Image.new("RGB", (GRID, GRID), KEY)
    return im, ImageDraw.Draw(im)


def read(t):
    """A page whose lines get read one by one, an orange highlight sweeping down."""
    im, d = canvas()
    d.rectangle([6, 3, 25, 28], fill=WHITE, outline=DARK)
    done = int(t * 7)
    for i in range(6):
        y, width = 7 + i * 3, (14, 11, 13, 9, 12, 8)[i]
        d.line([9, y, 9 + width, y], fill=ORANGE if i == done % 6 else GREY)
    return im


def search(t):
    """A magnifying glass circling."""
    im, d = canvas()
    a = t * 2 * math.pi
    cx, cy = 13 + 4 * math.cos(a), 13 + 4 * math.sin(a)
    d.ellipse([cx - 7, cy - 7, cx + 7, cy + 7], outline=DARK, width=2, fill=LIGHT)
    d.ellipse([cx - 4, cy - 5, cx - 1, cy - 2], fill=WHITE)
    d.line([cx + 5, cy + 5, cx + 11, cy + 11], fill=ORANGE, width=3)
    return im


def run(t):
    """A terminal typing a command, cursor blinking."""
    im, d = canvas()
    d.rectangle([3, 6, 28, 25], fill=DARK, outline=GREY)  # the outline keeps it visible on dark themes
    d.rectangle([3, 6, 28, 8], fill=GREY)
    d.line([6, 12, 8, 14], fill=ORANGE)
    d.line([8, 14, 6, 16], fill=ORANGE)
    typed = int(t * 12) % 12
    for i in range(min(typed, 8)):
        d.point([11 + i * 2, 15], fill=WHITE)
    if int(t * 8) % 2 == 0:
        d.rectangle([11 + min(typed, 8) * 2, 13, 12 + min(typed, 8) * 2, 16], fill=WHITE)
    return im


def edit(t):
    """A pencil writing a wavy line."""
    im, d = canvas()
    end = 5 + int(t * 20)
    points = [(x, 22 + 2 * math.sin(x / 2.0)) for x in range(5, end + 1)]
    if len(points) > 1:
        d.line(points, fill=ORANGE, width=2)
    x, y = points[-1]
    d.line([x, y, x + 9, y - 13], fill=DARK, width=4)
    d.line([x, y, x + 2, y - 3], fill=GREY, width=2)
    d.line([x + 8, y - 12, x + 10, y - 15], fill=LIGHT, width=4)
    return im


def web(t):
    """A globe spinning: meridians slide across."""
    im, d = canvas()
    d.ellipse([4, 4, 27, 27], fill=LIGHT, outline=DARK, width=2)
    d.line([5, 15.5, 26, 15.5], fill=ORANGE)
    for k in range(3):
        phase = (t + k / 3) % 1
        x = 15.5 + 10.5 * math.cos(phase * math.pi)
        width = abs(x - 15.5)
        d.ellipse([15.5 - width, 5, 15.5 + width, 26], outline=ORANGE)
    return im


def delegate(t):
    """Two small claudinos passing a task back and forth."""
    im, d = canvas()
    for x in (3, 21):
        d.rectangle([x, 14, x + 7, 21], fill=ORANGE)
        d.rectangle([x + 1, 16, x + 6, 17], fill=DARK)
        d.line([x + 1, 22, x + 1, 24], fill=ORANGE)
        d.line([x + 6, 22, x + 6, 24], fill=ORANGE)
    s = (math.sin(t * 2 * math.pi) + 1) / 2
    bx, by = 9 + s * 12, 10 - 6 * math.sin(s * math.pi)
    d.rectangle([bx, by, bx + 3, by + 3], fill=WHITE, outline=DARK)
    return im


def tool(t):
    """A gear turning."""
    im, d = canvas()
    a = t * 2 * math.pi / 8
    for k in range(8):
        ang = a + k * math.pi / 4
        x, y = 15.5 + 11 * math.cos(ang), 15.5 + 11 * math.sin(ang)
        d.rectangle([x - 2, y - 2, x + 2, y + 2], fill=ORANGE)
    d.ellipse([6, 6, 25, 25], fill=ORANGE)
    d.ellipse([11, 11, 20, 20], fill=KEY)
    return im


KINDS = {"read": read, "search": search, "run": run, "edit": edit, "web": web, "delegate": delegate,
         "tool": tool}


def palette() -> Image.Image:
    """One fixed palette for every frame, the transparent key first, so index 0 is transparent."""
    pal = Image.new("P", (1, 1))
    colors = [KEY, ORANGE, LIGHT, DARK, GREY, WHITE]
    pal.putpalette([c for color in colors for c in color] + [0] * 3 * (256 - len(colors)))
    return pal


def save(name, draw, out: Path):
    pal = palette()
    frames = [draw(i / FRAMES).resize((GRID * SCALE, GRID * SCALE), Image.NEAREST)
              .quantize(palette=pal, dither=Image.Dither.NONE) for i in range(FRAMES)]
    path = out / f"claudino-{name}.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=FRAME_MS, loop=0,
                   transparency=0, disposal=2, optimize=False)
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/emoji")
    out.mkdir(parents=True, exist_ok=True)
    for name, draw in KINDS.items():
        print(save(name, draw, out))
