#!/usr/bin/env python3
"""
check_cluster_species.py — Validate the duplicate-merging step against species.

consensus_gt/compare_gt collapse census dots within `--gps-err` metres into one
"tree" on the assumption that they are repeat visits. The census also records a
species per dot, which is an INDEPENDENT signal that assumption never used: if a
group's dots all name the same species it is plausibly one tree re-surveyed; if
they name different species they are certainly different trees wrongly merged.

Writes a per-group CSV and prints the summary.

Usage:
  python check_cluster_species.py --ortho <ortho.tif> --census bbmp_tree_census.gpkg \
      --out cluster_species_jp.csv [--gps-err 4]
"""
import argparse, collections, csv, os, sys

import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from sklearn.cluster import DBSCAN

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from consensus_gt import load_census_in_bbox  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ortho", required=True)
    ap.add_argument("--census", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--utm", default="EPSG:32643")
    ap.add_argument("--gps-err", type=float, default=4.0)
    a = ap.parse_args()

    src = rasterio.open(a.ortho)
    b = src.bounds
    bbox = transform_bounds(str(src.crs), "EPSG:4326", b.left, b.bottom, b.right, b.top)
    g = load_census_in_bbox(a.census, bbox).to_crs(a.utm)
    if not len(g):
        sys.exit("no census points in this ortho")

    xy = np.array([[p.x, p.y] for p in g.geometry])
    lab = DBSCAN(eps=a.gps_err, min_samples=1).fit(xy).labels_
    names = ["" if n is None else str(n).strip() for n in g["TreeName"]]

    # "Others" is the census's catch-all, not a species. Two dots both labelled
    # "Others" agree on nothing, so such groups carry NO evidence either way.
    UNINFORMATIVE = {"", "others", "other", "na", "n/a", "unknown", "none"}

    groups = collections.defaultdict(list)
    for i, k in enumerate(lab):
        groups[k].append(i)

    rows, pure, mixed, unnamed = [], 0, 0, 0
    for k, idx in sorted(groups.items()):
        pts = xy[idx]
        span = 0.0
        if len(idx) > 1:
            span = float(np.sqrt(((pts[:, None, :] - pts[None, :, :]) ** 2)
                                 .sum(-1)).max())
        usable = [names[i] for i in idx if names[i].lower() not in UNINFORMATIVE]
        sp = sorted(set(usable))
        if len(idx) > 1:
            # need at least TWO usable labels before "they agree" means anything
            if len(usable) < 2:
                unnamed += 1
                verdict = "not testable"
            elif len(sp) == 1:
                pure += 1
                verdict = "same species"
            else:
                mixed += 1
                verdict = "DIFFERENT species"
        else:
            verdict = "single dot"
        rows.append({"group": k, "n_dots": len(idx), "span_m": round(span, 2),
                     "n_species": len(sp), "verdict": verdict,
                     "species": " | ".join(sp)})

    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    multi = pure + mixed + unnamed
    print(f"{len(g)} dots -> {len(groups)} groups "
          f"({len(groups) - multi} single-dot, {multi} multi-dot)\n")
    print("Of the multi-dot groups (the ones the merging actually changed):")
    for lbl, n in (("all one real species -> plausibly one tree", pure),
                   ("different species    -> definitely NOT one tree", mixed),
                   ("fewer than 2 usable labels -> NOT TESTABLE", unnamed)):
        print(f"  {lbl:<45} {n:>4}  ({100*n/max(1,multi):.0f}%)")

    if pure + mixed < 0.2 * max(1, multi):
        print(f"\n  ** {100*unnamed/max(1,multi):.0f}% of merged groups carry no usable "
              f"species label, so this test CANNOT validate the merging here. **")

    # does disagreement track how spread out the group is? (informative groups only)
    print("\n  span of group      same species   different species")
    for lo, hi in ((0, 2), (2, 4), (4, 8), (8, 100)):
        s = [r for r in rows if r["n_dots"] > 1 and lo <= r["span_m"] < hi]
        p = sum(1 for r in s if r["verdict"] == "same species")
        m = sum(1 for r in s if r["verdict"] == "DIFFERENT species")
        print(f"  {lo:>3}-{hi if hi<100 else '+':<4} m         {p:>8}        {m:>8}")
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
