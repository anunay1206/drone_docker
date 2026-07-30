#!/usr/bin/env python3
"""
make_tile_compare_fig.py — Side-by-side proof for the tile-size claim.

The tile-size slide asserts that coverage peaks around tile 50 and degrades after.
This renders the same patch of ground from several runs so the claim is visible
rather than only tabulated.

Usage:
  python make_tile_compare_fig.py --project 9bb8878b --out docs/progress_reports/assets/tile_compare.png \
      --runs "run_1=tile 40" "run_2=tile 50" "run_6=tile 100" --window 80
"""
import argparse, glob, os, sys
import geopandas as gpd, numpy as np, rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.windows import Window, from_bounds

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare_gt import _font, read_rgb  # noqa: E402

OUTLINE = (255, 120, 40)


def panel(src, geoms, left, top, size_m, px):
    win = from_bounds(left, top - size_m, left + size_m, top, src.transform)
    win = Window(int(round(win.col_off)), int(round(win.row_off)),
                 int(round(win.width)), int(round(win.height)))
    wt = src.window_transform(win)
    l0, t0 = wt.c, wt.f
    res = abs(src.transform.a)
    img = Image.fromarray(read_rgb(src, win, out_shape=(px, px)))
    k = px / (win.width or 1)
    d = ImageDraw.Draw(img, "RGBA")
    for geom in geoms:
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            pts = [(((x - l0) / res) * k, ((t0 - y) / res) * k)
                   for x, y in poly.exterior.coords]
            d.line(pts + [pts[0]], fill=OUTLINE + (255,), width=4)
            d.polygon(pts, fill=OUTLINE + (45,))
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--runs", nargs="+", required=True, help="run_1='tile 40' ...")
    ap.add_argument("--out", required=True)
    ap.add_argument("--window", type=float, default=80)
    ap.add_argument("--px", type=int, default=720)
    a = ap.parse_args()

    P = glob.glob(f"data/storage/projects/{a.project}*/")[0]
    src = rasterio.open(glob.glob(P + "input/ortho/*.tif")[0])
    b = src.bounds

    # pick the densest-canopy window so the difference is actually visible
    from rasterio.enums import Resampling
    S = 1200
    arr = src.read([1, 2, 3], out_shape=(3, S, S),
                   resampling=Resampling.average).astype(int)
    canopy = (2 * arr[1] - arr[0] - arr[2]) > 15
    w = int(a.window / (b.right - b.left) * S)
    best = max(((canopy[r:r + w, c:c + w].mean(), r, c)
                for r in range(0, S - w, 40) for c in range(0, S - w, 40)))
    _, r, c = best
    left = b.left + c / S * (b.right - b.left)
    top = b.top - r / S * (b.top - b.bottom)

    panels, labels = [], []
    for spec in a.runs:
        run, lbl = spec.split("=", 1)
        g = gpd.read_file(glob.glob(f"{P}work/{run}/polygons/*.geojson")[0])
        sel = g.cx[left:left + a.window, top - a.window:top]
        panels.append(panel(src, list(sel.geometry), left, top, a.window, a.px))
        labels.append((lbl, len(sel)))

    gap, pad, head = 16, 18, 74
    W = pad * 2 + len(panels) * a.px + (len(panels) - 1) * gap
    H = head + a.px + 42 + pad
    canvas = Image.new("RGB", (W, H), (22, 24, 21))
    d = ImageDraw.Draw(canvas)
    d.text((pad, 16), f"The same {a.window:g} m of dense canopy, seen by "
                      f"{len(panels)} runs",
           fill=(240, 240, 235), font=_font(30))
    d.text((pad, 50), "orange = one tree outline drawn by the model",
           fill=(150, 152, 145), font=_font(19))
    for i, (img, (lbl, n)) in enumerate(zip(panels, labels)):
        x = pad + i * (a.px + gap)
        canvas.paste(img, (x, head))
        d.text((x, head + a.px + 8), f"{lbl}  —  {n} outlines here",
               fill=(255, 190, 120), font=_font(24))
    canvas.save(a.out)
    print(f"wrote {a.out}  {canvas.size}  window at ({left:.0f},{top:.0f})")


if __name__ == "__main__":
    main()
