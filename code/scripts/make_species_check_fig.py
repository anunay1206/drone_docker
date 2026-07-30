#!/usr/bin/env python3
"""
make_species_check_fig.py — Show the merged groups the species field says are wrong.

check_cluster_species.py reports that most 4 m groups contain more than one
species. That is a claim about a spreadsheet; this renders the actual imagery
under each such group so it can be judged by eye: is that one canopy with a
mislabelled dot, or plainly several different trees?

Usage:
  python make_species_check_fig.py --ortho species_test_500m.tif \
      --census bbmp_tree_census.gpkg --out docs/progress_reports/assets/species_check.png
"""
import argparse, os, sys
import geopandas as gpd, numpy as np, rasterio
from PIL import Image, ImageDraw
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from sklearn.cluster import DBSCAN

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare_gt import _font, read_rgb            # noqa: E402
from consensus_gt import load_census_in_bbox      # noqa: E402

PALETTE = [(70,190,255),(255,150,30),(120,230,120),(255,90,90),
           (220,140,255),(255,230,90),(90,255,220),(255,120,190)]
UNINF = {"", "others", "other", "na", "n/a", "unknown", "none"}


def short(name):
    w = str(name).replace("(", " ").split()
    return " ".join(w[:2]) if w else "?"


def crop(src, cx, cy, size_m, px):
    win = from_bounds(cx - size_m/2, cy - size_m/2, cx + size_m/2, cy + size_m/2,
                      src.transform)
    win = Window(int(round(win.col_off)), int(round(win.row_off)),
                 int(round(win.width)), int(round(win.height)))
    wt = src.window_transform(win)
    img = Image.fromarray(read_rgb(src, win, out_shape=(px, px)))
    return img, wt.c, wt.f, abs(src.transform.a) * (win.width / px)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ortho", required=True)
    ap.add_argument("--census", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--utm", default="EPSG:32643")
    ap.add_argument("--gps-err", type=float, default=4.0)
    ap.add_argument("--window", type=float, default=26)
    ap.add_argument("--px", type=int, default=620)
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--min-span", type=float, default=1.0,
                    help="ignore groups tighter than this (0-span groups are dots "
                         "stacked on one coordinate — a data-entry artefact, not a "
                         "merging question)")
    ap.add_argument("--max-species", type=int, default=3,
                    help="cap species per panel so the legend stays readable")
    a = ap.parse_args()

    src = rasterio.open(a.ortho); b = src.bounds
    bbox = transform_bounds(str(src.crs), "EPSG:4326", b.left, b.bottom, b.right, b.top)
    g = load_census_in_bbox(a.census, bbox).to_crs(a.utm)
    xy = np.array([[p.x, p.y] for p in g.geometry])
    lab = DBSCAN(eps=a.gps_err, min_samples=1).fit(xy).labels_
    names = [str(n).strip() for n in g["TreeName"]]

    # groups whose usable species labels disagree, smallest span first: those are
    # the cases the 4 m rule looks MOST defensible on, so the hardest test of it
    cands = []
    for k in np.unique(lab):
        idx = np.where(lab == k)[0]
        if len(idx) < 2:
            continue
        us = [names[i] for i in idx if names[i].lower() not in UNINF]
        if len(us) < 2 or len(set(us)) < 2:
            continue
        pts = xy[idx]
        span = float(np.sqrt(((pts[:, None, :] - pts[None, :, :])**2).sum(-1)).max())
        cands.append((span, len(set(us)), k, idx))
    cands = [c for c in cands if c[0] >= a.min_span and c[1] <= a.max_species]
    cands.sort(key=lambda t: t[0])
    # sample evenly across the span range so the panels show the trend, not one corner
    if len(cands) > a.n:
        step = (len(cands) - 1) / (a.n - 1)
        picks = [cands[round(i * step)] for i in range(a.n)]
    else:
        picks = cands
    print(f"{len(cands)} disagreeing groups with span >= {a.min_span} m and "
          f"<= {a.max_species} species; showing {len(picks)} across "
          f"{picks[0][0]:.1f}-{picks[-1][0]:.1f} m")

    gutil = gpd.GeoSeries([g.geometry.iloc[0]], crs=a.utm)   # for crs conversion
    panels = []
    for span, nsp, k, idx in picks:
        c = xy[idx].mean(axis=0)
        cx, cy = gpd.GeoSeries(gpd.points_from_xy([c[0]], [c[1]]), crs=a.utm)\
            .to_crs(src.crs).iloc[0].coords[0]
        img, l0, t0, res = crop(src, cx, cy, a.window, a.px)
        d = ImageDraw.Draw(img, "RGBA")
        uniq = sorted({names[i] for i in idx})
        cmap = {s: PALETTE[j % len(PALETTE)] for j, s in enumerate(uniq)}
        pr = gpd.GeoSeries(gpd.points_from_xy(xy[idx][:, 0], xy[idx][:, 1]),
                           crs=a.utm).to_crs(src.crs)
        for i, pt in zip(idx, pr):
            px_, py_ = (pt.x - l0) / res, (t0 - pt.y) / res
            col = cmap[names[i]]
            d.ellipse([px_-11, py_-11, px_+11, py_+11], fill=col+(210,),
                      outline=(15,15,15,255), width=3)
        # legend inside the panel
        y = 8
        for s in uniq:
            d.rectangle([8, y, 22, y+14], fill=cmap[s], outline=(230,230,230))
            d.text((28, y-2), short(s), fill=(255,255,255),
                   font=_font(17), stroke_width=2, stroke_fill=(0,0,0))
            y += 22
        panels.append((img, f"{len(idx)} dots · {nsp} species · {span:.1f} m apart"))

    cols = a.cols; rows = (len(panels)+cols-1)//cols
    gap, pad, head, cap = 14, 18, 76, 30
    W = pad*2 + cols*a.px + (cols-1)*gap
    H = head + rows*(a.px+cap) + (rows-1)*gap + pad
    canvas = Image.new("RGB", (W, H), (22,24,21))
    dd = ImageDraw.Draw(canvas)
    dd.text((pad,16), "Groups the 4 m rule merged — but the census names different "
                      "species", fill=(240,240,235), font=_font(30))
    dd.text((pad,50), f"each panel is {a.window:g} m across · one colour per "
                      f"species · sampled across the full range of group widths",
            fill=(150,152,145), font=_font(19))
    for i,(img,lbl) in enumerate(panels):
        x = pad + (i%cols)*(a.px+gap)
        y = head + (i//cols)*(a.px+cap+gap)
        canvas.paste(img,(x,y))
        dd.text((x, y+a.px+6), lbl, fill=(255,190,120), font=_font(21))
    canvas.save(a.out)
    print(f"wrote {a.out}  {canvas.size}")


if __name__ == "__main__":
    main()
