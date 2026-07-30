#!/usr/bin/env python3
"""
compare_gt.py — Render a side-by-side visual audit of MODEL crowns vs BBMP CENSUS
points so you can decide, by eye but systematically, whether the census is usable
as detectree2 training ground truth.

The ortho is cut into a regular grid of tiles. Each tile becomes ONE png with two
panels over the same patch of imagery:

    LEFT  = what the model detected   (crown polygons, coloured by census support)
    RIGHT = what the census claims    (consensus points, coloured by canopy/match)

Same imagery, same extent, same scale — so a disagreement is a difference you can
see rather than one you have to infer from a metric.

Classification reuses consensus_gt.py's signals (ExG greenness from the ortho +
DBSCAN consensus collapsing duplicate survey points), so the categories here mean
exactly what its printed report means:

  crowns                          census consensus points
  ------                          -----------------------
  validated   >=1 consensus pt    confirmed        matched & on canopy
  merged      >=2 consensus pts   matched_offcanopy matched but ExG low
  green_nopt  0 pts but green     missed_by_model  on canopy, no crown  <- FN
  likely_fp   0 pts, not green    bad_census       off canopy, no crown <- junk

Outputs (under --out):
  index.html      open this — thumbnails, totals, legend
  overview.png    whole extent, both panels
  tiles/*.png     the grid, one file per tile
  summary.csv     per-tile counts, plus a totals row
  stats.json      machine-readable totals/rates/coverage/area-cap evidence
                  (feeds make_gt_report.py and make_summary_md.py)

Usage:
  python compare_gt.py \
      --ortho jp_nagar_drone_ortho_native_5cm_sample.tif \
      --crowns data/storage/projects/<id>/work/run_1/polygons/<stem>.geojson \
      --census bbmp_tree_census.gpkg \
      --out ./gt_compare --tile-m 50
"""
import argparse
import csv
import html
import json
import os
import sys

import geopandas as gpd
import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from shapely.geometry import Point, Polygon
from sklearn.cluster import DBSCAN

# Reuse the census loader / ExG sampling from the analysis script rather than
# re-deriving them — same file, same defaults, same meaning.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from consensus_gt import crown_greenness, exg_sampler, load_census_in_bbox  # noqa: E402

# ── categories -> (label, RGB) ────────────────────────────────────────────────
CROWN_CATS = {
    "validated":  ("validated by census",      (60, 200, 90)),
    "merged":     ("merged (>=2 census trees)", (255, 150, 30)),
    "green_nopt": ("green, no census point",    (245, 225, 60)),
    "likely_fp":  ("not green, no point (FP?)", (235, 60, 60)),
}
POINT_CATS = {
    "confirmed":         ("confirmed by a crown",   (60, 200, 90)),
    "matched_offcanopy": ("matched but not green",   (170, 175, 60)),
    "missed_by_model":   ("on canopy, no crown",     (70, 190, 255)),
    "bad_census":        ("off canopy, no crown",    (235, 60, 60)),
}


def _font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def classify(ortho, crowns, clusters, utm, exg_thresh, match_tol):
    """Attach an ExG-and-proximity category to every crown and consensus point.

    Mirrors consensus_gt.main()'s matching step: buffer each crown by the GPS
    tolerance and spatial-join the consensus centroids into it.
    """
    sample = exg_sampler(ortho)
    crowns["exg"] = [crown_greenness(p, sample, utm) for p in crowns.geometry]
    crowns["veg"] = crowns.exg > exg_thresh

    buf = crowns.copy()
    buf["geometry"] = crowns.buffer(match_tol)
    sj = gpd.sjoin(clusters.assign(cid=range(len(clusters))),
                   buf.assign(kid=range(len(buf))),
                   predicate="within", how="left")
    per_crown = sj.dropna(subset=["kid"]).groupby("kid").size()
    crowns["n_clusters"] = [int(per_crown.get(i, 0)) for i in range(len(crowns))]
    matched = set(sj.dropna(subset=["kid"])["cid"])
    clusters["matched"] = [i in matched for i in range(len(clusters))]

    def crown_cat(r):
        if r.n_clusters >= 2:
            return "merged"
        if r.n_clusters == 1:
            return "validated"
        return "green_nopt" if r.veg else "likely_fp"

    def point_cat(r):
        if r.matched:
            return "confirmed" if r.on_canopy else "matched_offcanopy"
        return "missed_by_model" if r.on_canopy else "bad_census"

    crowns["cat"] = crowns.apply(crown_cat, axis=1)
    clusters["cat"] = clusters.apply(point_cat, axis=1)
    return crowns, clusters


def build_consensus(census_pts, utm, gps_err, exg_thresh, sample):
    """DBSCAN-collapse duplicate survey points into consensus trees (as in
    consensus_gt.main): eps = GPS error, so scattered repeat surveys of one tree
    become a single centroid weighted by n_pts."""
    census_pts["exg"] = sample(census_pts.geometry.values, utm)
    xy = np.array([[p.x, p.y] for p in census_pts.geometry])
    census_pts["cluster"] = DBSCAN(eps=gps_err, min_samples=1).fit(xy).labels_
    grp = census_pts.groupby("cluster")
    cent = grp.geometry.apply(
        lambda s: Point(np.mean([p.x for p in s]), np.mean([p.y for p in s])))
    clusters = gpd.GeoDataFrame(
        {"n_pts": grp.size().values, "exg": grp["exg"].mean().values},
        geometry=cent.values, crs=utm)
    clusters["on_canopy"] = clusters.exg > exg_thresh
    return clusters


def coverage_stats(census_r, crowns_r, b, nbins=10):
    """Where the census actually surveyed, versus where the ortho looks.

    A municipal census has its own footprint (ward/road boundaries); the drone
    sees everything. If that footprint doesn't fill the ortho, every crown outside
    it is scored 'no census point' for a reason that has nothing to do with the
    detector — so quantify the gap instead of leaving it to the eye.
    """
    W = b.right - b.left
    bins = []
    for i in range(nbins):
        x0 = b.left + i * W / nbins
        n = len(census_r.cx[x0:x0 + W / nbins, b.bottom:b.top])
        bins.append({"from_pct": round(100 * i / nbins),
                     "to_pct": round(100 * (i + 1) / nbins), "points": int(n)})
    cb = list(census_r.total_bounds) if len(census_r) else [0, 0, 0, 0]
    inside = crowns_r.cx[cb[0]:cb[2], cb[1]:cb[3]] if len(census_r) else crowns_r.iloc[:0]
    return {
        "width_bins": bins,
        "census_bbox": [float(v) for v in cb],
        "unsurveyed_west_pct": round(100 * (cb[0] - b.left) / W, 1),
        "crowns_inside_census_bbox": int(len(inside)),
        "crowns_outside_census_bbox": int(len(crowns_r) - len(inside)),
    }


def true_area_factor(src, utm="EPSG:32643"):
    """Multiplier converting src-CRS square units to TRUE square metres.

    These orthos are EPSG:3857 (Web Mercator), whose "metres" are inflated by
    1/cos(latitude) — ~5.8% in area at Bangalore. Reporting raw 3857 areas as m²
    overstates every figure, so measure the extent in a real metric CRS and take
    the ratio. Ratios of two same-CRS areas are unaffected; absolutes are not.
    """
    b = src.bounds
    poly = Polygon([(b.left, b.bottom), (b.right, b.bottom),
                    (b.right, b.top), (b.left, b.top)])
    src_area = poly.area
    true_area = gpd.GeoSeries([poly], crs=src.crs).to_crs(utm).area.iloc[0]
    return float(true_area / src_area) if src_area else 1.0


def canopy_coverage(src, crowns_r, exg_thresh, size=2500, area_factor=1.0):
    """How much of the ortho's canopy the detector actually enclosed, BY AREA.

    Counting crowns hides the failure mode where a detector finds many small
    isolated trees and none of the large contiguous canopy: 205 crowns sounds
    reasonable until you see they cover 6% of the vegetation. Rasterises an ExG
    canopy mask and the crown polygons onto a common reduced grid and intersects
    them, giving a count-independent recall plus a purity check (how much of the
    crown area is actually on vegetation).
    """
    from rasterio.enums import Resampling
    from rasterio.features import rasterize

    arr = src.read([1, 2, 3], out_shape=(3, size, size),
                   resampling=Resampling.average).astype(int)
    canopy = (2 * arr[1] - arr[0] - arr[2]) > exg_thresh
    tr = src.transform * src.transform.scale(src.width / size, src.height / size)
    px = abs(tr.a * tr.e) * area_factor   # -> true m^2 per cell
    mask = rasterize([(g, 1) for g in crowns_r.geometry], out_shape=(size, size),
                     transform=tr, fill=0, dtype="uint8").astype(bool)
    inter = int((mask & canopy).sum())
    can, crn = int(canopy.sum()), int(mask.sum())
    return {
        "canopy_m2": round(can * px),
        "canopy_pct_of_extent": round(100 * can / (size * size), 1),
        "crown_area_m2": round(crn * px),
        "canopy_covered_pct": round(100 * inter / max(1, can), 1),
        "crown_on_canopy_pct": round(100 * inter / max(1, crn), 1),
        "canopy_missed_m2": round((can - inter) * px),
    }


# predict.py filters crowns to `4 < area < 200`, measured in the RASTER CRS.
# Mirrored here so the reports can quantify what that forecloses. Keep in sync.
PREDICT_AREA_MIN, PREDICT_AREA_MAX = 4, 200


def area_cap_stats(src, crowns_r, exg_thresh, area_factor=1.0, size=2500):
    """Evidence for whether predict.py's hard crown-area cap is binding.

    Two independent signals:
      * the crown-size distribution pressed against the ceiling (and the floor);
      * how much ExG canopy sits in connected blobs larger than the cap, i.e.
        canopy that cannot be enclosed by any single permitted crown.
    Areas are compared in the SAME CRS predict.py filters in, since that is the
    space the threshold actually acts on.
    """
    from rasterio.enums import Resampling
    from scipy import ndimage

    ar = crowns_r.geometry.area   # raster CRS == the CRS predict.py measured in
    arr = src.read([1, 2, 3], out_shape=(3, size, size),
                   resampling=Resampling.average).astype(int)
    canopy = (2 * arr[1] - arr[0] - arr[2]) > exg_thresh
    tr = src.transform * src.transform.scale(src.width / size, src.height / size)
    px_src = abs(tr.a * tr.e)
    lab, n = ndimage.label(canopy)
    blob = ndimage.sum(canopy, lab, range(1, n + 1)) * px_src
    tot = float(blob.sum())
    over = float(blob[blob > PREDICT_AREA_MAX].sum())
    return {
        "cap_min_src": PREDICT_AREA_MIN, "cap_max_src": PREDICT_AREA_MAX,
        "cap_max_true_m2": round(PREDICT_AREA_MAX * area_factor, 1),
        "crown_area_max_src": round(float(ar.max()), 1) if len(ar) else 0.0,
        "crowns_at_floor": int((ar <= PREDICT_AREA_MIN * 1.05).sum()),
        "crowns_near_cap": int((ar >= PREDICT_AREA_MAX * 0.95).sum()),
        "crowns_over_cap": int((ar >= PREDICT_AREA_MAX).sum()),
        "canopy_blobs": int(n),
        "canopy_in_blobs_over_cap_pct": round(100 * over / tot, 1) if tot else 0.0,
    }


def read_rgb(src, window, out_shape=None):
    """Window of the ortho as an HxWx3 uint8 array, padded if it runs off-edge.

    ``out_shape`` decimates during the read. Required for whole-raster reads: a
    gigapixel ortho at full resolution is several GB in RAM, so the overview must
    never materialise it.
    """
    kw = {"indexes": [1, 2, 3], "window": window, "boundless": True, "fill_value": 0}
    if out_shape is not None:
        kw["out_shape"] = (3, out_shape[0], out_shape[1])
    arr = src.read(**kw)
    return np.transpose(arr, (1, 2, 0)).astype("uint8")


def _to_px(x, y, left, top, res):
    return ((x - left) / res, (top - y) / res)


def draw_panel(base, geoms_cats, left, top, res, kind, scale):
    """Draw one panel: the imagery plus either crown outlines or census dots."""
    img = base.copy()
    d = ImageDraw.Draw(img, "RGBA")
    for geom, cat, extra in geoms_cats:
        colour = (CROWN_CATS if kind == "crowns" else POINT_CATS)[cat][1]
        if kind == "crowns":
            polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
            for poly in polys:
                pts = [_to_px(x, y, left, top, res) for x, y in poly.exterior.coords]
                pts = [(a * scale, b * scale) for a, b in pts]
                d.line(pts + [pts[0]], fill=colour + (255,), width=max(2, int(2 * scale)))
                d.polygon(pts, fill=colour + (55,))
        else:
            px, py = _to_px(geom.x, geom.y, left, top, res)
            px, py = px * scale, py * scale
            # Radius is in OUTPUT pixels, deliberately NOT multiplied by `scale`:
            # a marker is a screen annotation, not a ground feature, so it must
            # stay legible on the heavily downscaled overview too. It still grows
            # with survey agreement (n_pts collapsed into this tree).
            r = 7 + min(11, 2.2 * float(np.sqrt(max(1, extra))))
            d.ellipse([px - r, py - r, px + r, py + r],
                      fill=colour + (170,), outline=(15, 15, 15, 235), width=2)
            if extra >= 5:
                d.text((px + r + 2, py - r), str(int(extra)),
                       fill=(255, 255, 255, 255), font=_font(13),
                       stroke_width=2, stroke_fill=(0, 0, 0, 220))
    return img


def compose(pa, pb, title, sub_a, sub_b):
    """Two panels side by side under a header. Each panel carries its OWN legend
    directly beneath it — crown categories under the model panel, point
    categories under the census panel, so the two colour vocabularies never get
    confused for one another."""
    f_t, f_s, f_l = _font(26), _font(19), _font(16)
    gap, pad, head = 14, 16, 74
    lg_h = 12 + max(len(CROWN_CATS), len(POINT_CATS)) * 24
    W = pad * 2 + pa.width + gap + pb.width
    H = head + pa.height + 30 + lg_h + pad
    canvas = Image.new("RGB", (W, H), (22, 24, 21))
    d = ImageDraw.Draw(canvas)
    d.text((pad, 14), title, fill=(240, 240, 235), font=f_t)
    d.text((pad, 46), sub_a, fill=(150, 220, 160), font=f_s)
    d.text((pad + pa.width + gap, 46), sub_b, fill=(150, 200, 240), font=f_s)
    canvas.paste(pa, (pad, head))
    canvas.paste(pb, (pad + pa.width + gap, head))
    y0 = head + pa.height + 8
    for x, heading, colour, cats in (
        (pad, "MODEL — detectree2 crowns", (150, 220, 160), CROWN_CATS),
        (pad + pa.width + gap, "CENSUS — BBMP consensus trees", (150, 200, 240),
         POINT_CATS),
    ):
        d.text((x, y0), heading, fill=colour, font=f_l)
        for i, (label, swatch) in enumerate(cats.values()):
            cy = y0 + 26 + i * 24
            d.rectangle([x, cy + 4, x + 14, cy + 16], fill=swatch,
                        outline=(230, 230, 230))
            d.text((x + 22, cy), label, fill=(215, 215, 210), font=f_l)
    return canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ortho", required=True, help="native-resolution GeoTIFF")
    ap.add_argument("--crowns", required=True, help="run's polygons/<stem>.geojson")
    ap.add_argument("--census", required=True, help=".gpkg (fast) or raw .geojson")
    ap.add_argument("--out", default="./gt_compare")
    ap.add_argument("--tile-m", type=float, default=50.0,
                    help="tile edge in ortho CRS units (default 50)")
    ap.add_argument("--skip-tiles", action="store_true",
                    help="recompute stats/index without re-rendering tile pngs")
    ap.add_argument("--max-px", type=int, default=1100,
                    help="max panel edge in px; tiles are downscaled to fit")
    ap.add_argument("--utm", default="EPSG:32643", help="metric CRS for distances")
    ap.add_argument("--gps-err", type=float, default=4.0)
    ap.add_argument("--exg-thresh", type=float, default=15.0)
    ap.add_argument("--match-tol", type=float, default=4.0)
    a = ap.parse_args()

    os.makedirs(f"{a.out}/tiles", exist_ok=True)
    src = rasterio.open(a.ortho)
    b = src.bounds
    res = abs(src.transform.a)
    print(f"ortho  : {src.width}x{src.height} @ {res:.4f} {src.crs} units/px, {src.crs}")

    bbox = transform_bounds(str(src.crs), "EPSG:4326", b.left, b.bottom, b.right, b.top)
    census = load_census_in_bbox(a.census, bbox).to_crs(a.utm)
    crowns = gpd.read_file(a.crowns).to_crs(a.utm)
    print(f"census : {len(census)} raw points in extent")
    print(f"crowns : {len(crowns)} detections")
    if len(crowns) == 0 or len(census) == 0:
        sys.exit("nothing to compare — check the ortho/crowns/census overlap")

    sample = exg_sampler(src)
    clusters = build_consensus(census, a.utm, a.gps_err, a.exg_thresh, sample)
    crowns, clusters = classify(src, crowns, clusters, a.utm, a.exg_thresh, a.match_tol)
    print(f"consensus trees: {len(clusters)} "
          f"(from {len(census)} raw; max {int(clusters.n_pts.max())} pts collapsed)")

    # to raster CRS for pixel maths
    crowns_r = crowns.to_crs(src.crs)
    clusters_r = clusters.to_crs(src.crs)

    totals_c = {k: int((crowns.cat == k).sum()) for k in CROWN_CATS}
    totals_p = {k: int((clusters.cat == k).sum()) for k in POINT_CATS}
    print("\n=== CROWNS ({}) ===".format(len(crowns)))
    for k, (lab, _) in CROWN_CATS.items():
        print(f"  {lab:<28} {totals_c[k]:>5}")
    print("=== CENSUS CONSENSUS TREES ({}) ===".format(len(clusters)))
    for k, (lab, _) in POINT_CATS.items():
        print(f"  {lab:<28} {totals_p[k]:>5}")
    tp = totals_c["validated"] + totals_c["merged"]
    prec = tp / max(1, len(crowns))
    on_can = totals_p["confirmed"] + totals_p["missed_by_model"]
    rec = totals_p["confirmed"] / max(1, on_can)
    usable = on_can / max(1, len(clusters))
    print(f"\n  crowns with census support : {prec:.0%}")
    print(f"  on-canopy census found     : {rec:.0%}")
    print(f"  census points on canopy    : {usable:.0%}  <- ceiling on GT usability")

    # ── tiles ────────────────────────────────────────────────────────────────
    ncols = int(np.ceil((b.right - b.left) / a.tile_m))
    nrows = int(np.ceil((b.top - b.bottom) / a.tile_m))
    print(f"\ntiling : {nrows} x {ncols} = {nrows*ncols} tiles of {a.tile_m:g} units")
    rows = []
    for r in range(nrows):
        for c in range(ncols):
            left = b.left + c * a.tile_m
            top = b.top - r * a.tile_m
            right, bottom = left + a.tile_m, top - a.tile_m
            win = from_bounds(left, bottom, right, top, src.transform)
            win = Window(int(round(win.col_off)), int(round(win.row_off)),
                         int(round(win.width)), int(round(win.height)))
            if win.width < 2 or win.height < 2:
                continue
            # Draw against the window's OWN origin, not the requested bounds —
            # rounding to whole pixels above shifts it by up to half a pixel, and
            # this is an alignment audit, so that offset must not creep in.
            wt = src.window_transform(win)
            left, top = wt.c, wt.f
            right, bottom = left + win.width * res, top - win.height * res
            if a.skip_tiles:
                name = f"r{r:02d}_c{c:02d}"
                ci = crowns_r.cx[left:right, bottom:top]
                pi = clusters_r.cx[left:right, bottom:top]
                counts = {f"crown_{k}": int((ci.cat == k).sum()) for k in CROWN_CATS}
                counts.update({f"pt_{k}": int((pi.cat == k).sum()) for k in POINT_CATS})
                rows.append({"tile": name, "crowns": len(ci), "census": len(pi),
                             **counts})
                continue
            rgb = read_rgb(src, win)
            base = Image.fromarray(rgb)
            scale = min(1.0, a.max_px / max(base.width, base.height))
            if scale < 1.0:
                base = base.resize((max(1, int(base.width * scale)),
                                    max(1, int(base.height * scale))),
                                   Image.LANCZOS)

            tile_geom = rasterio.coords.BoundingBox(left, bottom, right, top)
            ci = crowns_r.cx[tile_geom.left:tile_geom.right,
                             tile_geom.bottom:tile_geom.top]
            pi = clusters_r.cx[tile_geom.left:tile_geom.right,
                               tile_geom.bottom:tile_geom.top]
            name = f"r{r:02d}_c{c:02d}"
            counts = {f"crown_{k}": int((ci.cat == k).sum()) for k in CROWN_CATS}
            counts.update({f"pt_{k}": int((pi.cat == k).sum()) for k in POINT_CATS})
            rows.append({"tile": name, "crowns": len(ci), "census": len(pi), **counts})

            pa = draw_panel(base, [(g, cat, 0) for g, cat in
                                   zip(ci.geometry, ci.cat)],
                            left, top, res, "crowns", scale)
            pb = draw_panel(base, [(g, cat, n) for g, cat, n in
                                   zip(pi.geometry, pi.cat, pi.n_pts)],
                            left, top, res, "points", scale)
            canvas = compose(
                pa, pb,
                f"{name}   ·   {a.tile_m:g} units ({left:.0f}, {bottom:.0f})",
                f"{len(ci)} crowns", f"{len(pi)} consensus trees")
            canvas.save(f"{a.out}/tiles/{name}.png")
    print(f"{'counted' if a.skip_tiles else 'wrote'} {len(rows)} tiles"
          f"{'' if a.skip_tiles else f' -> {a.out}/tiles/'}")

    # ── overview ─────────────────────────────────────────────────────────────
    if not a.skip_tiles:
        ov_scale = min(1.0, 1600 / max(src.width, src.height))
        ow, oh = int(src.width * ov_scale), int(src.height * ov_scale)
        # decimate in the read, not after — see read_rgb
        ov = Image.fromarray(read_rgb(src, Window(0, 0, src.width, src.height),
                                      out_shape=(oh, ow)))
        pa = draw_panel(ov, [(g, cat, 0) for g, cat in
                             zip(crowns_r.geometry, crowns_r.cat)],
                        b.left, b.top, res, "crowns", ov_scale)
        pb = draw_panel(ov, [(g, cat, n) for g, cat, n in
                             zip(clusters_r.geometry, clusters_r.cat, clusters_r.n_pts)],
                        b.left, b.top, res, "points", ov_scale)
        compose(pa, pb, "FULL EXTENT", f"{len(crowns)} crowns",
                f"{len(clusters)} consensus trees").save(f"{a.out}/overview.png")

    # ── summary.csv ──────────────────────────────────────────────────────────
    cols = ["tile", "crowns", "census"] + \
           [f"crown_{k}" for k in CROWN_CATS] + [f"pt_{k}" for k in POINT_CATS]
    with open(f"{a.out}/summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
        tot = {"tile": "TOTAL", "crowns": len(crowns), "census": len(clusters)}
        tot.update({f"crown_{k}": totals_c[k] for k in CROWN_CATS})
        tot.update({f"pt_{k}": totals_p[k] for k in POINT_CATS})
        w.writerow(tot)

    # ── stats.json — everything a report generator needs, no re-derivation ────
    cov = coverage_stats(clusters_r, crowns_r, b)
    afac = true_area_factor(src, a.utm)
    true_utm = gpd.GeoSeries(
        [Polygon([(b.left, b.bottom), (b.right, b.bottom),
                  (b.right, b.top), (b.left, b.top)])],
        crs=src.crs).to_crs(a.utm).total_bounds
    stats = {
        "inputs": {
            "ortho": os.path.abspath(a.ortho),
            "crowns": os.path.abspath(a.crowns),
            "census": os.path.abspath(a.census),
            "ortho_px": [src.width, src.height],
            "ortho_crs": str(src.crs),
            "gsd_units": res,
            "extent_units": [round(b.right - b.left, 1), round(b.top - b.bottom, 1)],
            "extent_true_m": [round(true_utm[2] - true_utm[0], 1),
                              round(true_utm[3] - true_utm[1], 1)],
        },
        "params": {"tile_m": a.tile_m, "gps_err": a.gps_err,
                   "exg_thresh": a.exg_thresh, "match_tol": a.match_tol,
                   "utm": a.utm},
        "counts": {
            "census_raw_points": int(len(census)),
            "consensus_trees": int(len(clusters)),
            "max_pts_collapsed": int(clusters.n_pts.max()),
            "crowns": int(len(crowns)),
            "crown_cats": totals_c,
            "point_cats": totals_p,
        },
        "rates": {
            "crowns_census_supported": round(prec, 4),
            "on_canopy_census_found": round(rec, 4),
            "census_points_on_canopy": round(usable, 4),
        },
        "coverage": cov,
        "canopy": canopy_coverage(src, crowns_r, a.exg_thresh,
                                  area_factor=afac),
        "area_cap": area_cap_stats(src, crowns_r, a.exg_thresh, area_factor=afac),
        "crs_note": {
            "src_crs": str(src.crs),
            "true_area_factor": round(afac, 5),
            "true_extent_m": [round(true_utm[2] - true_utm[0], 1),
                              round(true_utm[3] - true_utm[1], 1)],
            "comment": "src is Web Mercator; extents/areas here are TRUE metres. "
                       "Ratios (canopy_covered_pct etc.) are CRS-independent.",
        },
        "tiles": {"rows": nrows, "cols": ncols, "written": len(rows)},
    }
    with open(f"{a.out}/stats.json", "w") as f:
        json.dump(stats, f, indent=2)

    write_index(a, rows, totals_c, totals_p, len(crowns), len(clusters),
                prec, rec, usable)
    cn = stats["canopy"]
    print(f"\ncanopy in extent           : {cn['canopy_m2']:,} m2 "
          f"({cn['canopy_pct_of_extent']}% of the ortho)")
    print(f"  enclosed by crowns       : {cn['canopy_covered_pct']}%  "
          f"<- area recall, ignores crown count")
    print(f"  crown area on vegetation : {cn['crown_on_canopy_pct']}%")
    ac = stats["area_cap"]
    print(f"\npredict.py crown-area cap {ac['cap_min_src']}-{ac['cap_max_src']} "
          f"(src CRS; {ac['cap_max_true_m2']} true m2)")
    print(f"  largest crown            : {ac['crown_area_max_src']}  "
          f"(at floor: {ac['crowns_at_floor']}, near cap: {ac['crowns_near_cap']}, "
          f"over cap: {ac['crowns_over_cap']})")
    print(f"  canopy in blobs > cap    : {ac['canopy_in_blobs_over_cap_pct']}%  "
          f"<- cannot be one crown")
    print(f"\ncensus surveyed nothing in the western {cov['unsurveyed_west_pct']:.0f}% "
          f"of the extent; {cov['crowns_outside_census_bbox']} crowns fall outside "
          f"the census bbox entirely")
    print(f"open {os.path.abspath(a.out)}/index.html")


def write_index(a, rows, totals_c, totals_p, n_crowns, n_clusters,
                prec, rec, usable):
    def sw(colour):
        return (f"<span style='display:inline-block;width:11px;height:11px;"
                f"background:rgb{colour};border:1px solid #999;"
                f"vertical-align:middle;margin-right:6px'></span>")

    cat_rows = "".join(
        f"<tr><td>{sw(CROWN_CATS[k][1])}{html.escape(CROWN_CATS[k][0])}</td>"
        f"<td class='n'>{totals_c[k]}</td>"
        f"<td class='n'>{totals_c[k]/max(1,n_crowns):.0%}</td></tr>"
        for k in CROWN_CATS)
    pt_rows = "".join(
        f"<tr><td>{sw(POINT_CATS[k][1])}{html.escape(POINT_CATS[k][0])}</td>"
        f"<td class='n'>{totals_p[k]}</td>"
        f"<td class='n'>{totals_p[k]/max(1,n_clusters):.0%}</td></tr>"
        for k in POINT_CATS)
    def legend_group(title, cats):
        items = "".join(f"<span class='lg'>{sw(c)}{html.escape(l)}</span>"
                        for l, c in cats.values())
        return f"<div style='margin-top:8px'><b>{title}</b><br>{items}</div>"
    legend_html = (legend_group("Left panel — model crowns", CROWN_CATS)
                   + legend_group("Right panel — census trees", POINT_CATS))
    cards = "".join(
        f"<a class='card' href='tiles/{r['tile']}.png' target='_blank'>"
        f"<img src='tiles/{r['tile']}.png' loading='lazy'>"
        f"<div class='cap'><b>{r['tile']}</b>"
        f"<span>{r['crowns']} crowns · {r['census']} census</span></div></a>"
        for r in rows)

    doc = f"""<!doctype html><meta charset="utf-8">
<title>Model vs BBMP census — {html.escape(os.path.basename(a.ortho))}</title>
<style>
 body{{margin:0;padding:26px;background:#161815;color:#e6e6e1;
   font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}}
 h1{{font-size:21px;margin:0 0 4px}} h2{{font-size:16px;margin:30px 0 10px}}
 .muted{{color:#9a9c95}} code{{background:#23261f;padding:1px 5px;border-radius:3px}}
 .row{{display:flex;gap:34px;flex-wrap:wrap;margin-top:14px}}
 table{{border-collapse:collapse;font-size:13px}}
 th,td{{padding:5px 12px;border-bottom:1px solid #2c2f28;text-align:left}}
 td.n{{text-align:right;font-variant-numeric:tabular-nums}}
 .hero{{display:flex;gap:26px;flex-wrap:wrap;margin:16px 0 4px}}
 .stat{{background:#1e211c;border:1px solid #2f332b;border-radius:6px;
   padding:12px 18px;min-width:150px}}
 .stat b{{display:block;font-size:26px;font-weight:700}}
 .lg{{display:inline-block;margin:0 16px 6px 0;font-size:12px;color:#c9cbc4}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}}
 .card{{background:#1e211c;border:1px solid #2f332b;border-radius:6px;
   overflow:hidden;text-decoration:none;color:inherit}}
 .card:hover{{border-color:#5d8a5f}} .card img{{width:100%;display:block}}
 .cap{{display:flex;justify-content:space-between;padding:7px 10px;font-size:12px}}
 .cap span{{color:#9a9c95}} img.ov{{width:100%;border-radius:6px;margin-top:8px}}
</style>
<h1>Model crowns vs BBMP census — visual audit</h1>
<div class="muted">
 ortho <code>{html.escape(os.path.basename(a.ortho))}</code> ·
 crowns <code>{html.escape(a.crowns)}</code> ·
 census <code>{html.escape(os.path.basename(a.census))}</code><br>
 tiles {a.tile_m:g} units · GPS tolerance {a.gps_err:g} m ·
 ExG canopy cutoff {a.exg_thresh:g} · match tolerance {a.match_tol:g} m
</div>

<div class="hero">
  <div class="stat"><b>{usable:.0%}</b>census points on canopy
    <div class="muted">ceiling on usable GT</div></div>
  <div class="stat"><b>{prec:.0%}</b>crowns census-supported</div>
  <div class="stat"><b>{rec:.0%}</b>on-canopy census found</div>
  <div class="stat"><b>{n_crowns}</b>model crowns</div>
  <div class="stat"><b>{n_clusters}</b>consensus trees
    <div class="muted">duplicates collapsed</div></div>
</div>

<div class="row">
 <div><h2>Crowns</h2><table>
   <tr><th>category</th><th>n</th><th>share</th></tr>{cat_rows}</table></div>
 <div><h2>Census consensus trees</h2><table>
   <tr><th>category</th><th>n</th><th>share</th></tr>{pt_rows}</table></div>
</div>

<h2>How to read a tile</h2>
<div class="muted">Left panel = model detections. Right panel = census consensus
 trees over the same imagery; dot size grows with how many raw survey points
 collapsed into that tree, and counts &ge;5 are labelled.</div>
<div style="margin-top:10px">{legend_html}</div>

<h2>Full extent</h2>
<a href="overview.png" target="_blank"><img class="ov" src="overview.png"></a>

<h2>Tiles <span class="muted" style="font-size:13px">({len(rows)} — click to open full size)</span></h2>
<div class="grid">{cards}</div>
"""
    with open(f"{a.out}/index.html", "w") as f:
        f.write(doc)


if __name__ == "__main__":
    main()
