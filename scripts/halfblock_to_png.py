"""Render half-block terminal output to a PNG so it can be inspected off-terminal.

Used to verify image_preview's unicode encoder produces a recognizable preview
without needing a live terminal.
"""

import sys

from PIL import Image, ImageDraw

sys.path.insert(0, "/home/polyphonyrequiem/hermes-canvas-poc")

from agent import image_preview as ip  # noqa: E402

CW, CH = 8, 16  # simulated cell size in px


def cells_to_png(grid, out_path, bg=(16, 16, 20)):
    """Draw (top,bottom) RGBA cell rows as two stacked half-pixels per cell."""
    rows = len(grid)
    cols = max((len(r) for r in grid), default=0)
    img = Image.new("RGB", (cols * CW, rows * CH), bg)
    d = ImageDraw.Draw(img)

    for y, row in enumerate(grid):
        for x, (top, bottom) in enumerate(row):
            for half, px in ((0, top), (1, bottom)):
                r, g, b, a = px
                if a < 8:
                    continue
                # composite onto bg by alpha
                f = a / 255.0
                c = (
                    int(r * f + bg[0] * (1 - f)),
                    int(g * f + bg[1] * (1 - f)),
                    int(b * f + bg[2] * (1 - f)),
                )
                y0 = y * CH + half * (CH // 2)
                d.rectangle([x * CW, y0, x * CW + CW - 1, y0 + CH // 2 - 1], fill=c)

    img.save(out_path)
    return img.size


if __name__ == "__main__":
    src = sys.argv[1]
    out = sys.argv[2]
    cols = int(sys.argv[3]) if len(sys.argv) > 3 else 56
    rows = int(sys.argv[4]) if len(sys.argv) > 4 else 20
    grid = ip.thumbnail_cells(src, max_cols=cols, max_rows=rows)
    size = cells_to_png(grid, out)
    print(f"{src} -> {out}  grid={len(grid)}x{len(grid[0])} cells  png={size}")
