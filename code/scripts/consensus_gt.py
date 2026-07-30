#!/usr/bin/env python3
"""
consensus_gt.py — Decide how much of the BBMP tree-census can be used as ground
truth for detectree2 crown detection, WITHOUT eyeballing Google Maps.

Two objective signals replace the eye:
  1. GREENNESS from the ortho itself (ExG = 2G - R - B) at each census point and
     inside each crown. A point/crown on roof/ground/shadow scores low; canopy
     scores high. This tells you which census points are physically on a tree.
  2. PROXIMITY between census points and detected crowns (within a GPS-error
     tolerance), which tells you which detections a survey actually confirms.

Consensus for "many points near one crown":
  - DBSCAN(eps = GPS error) collapses duplicate/scattered survey points into ONE
    consensus tree (centroid). n_pts in the cluster = agreement weight.
  - A crown covering >=2 consensus clusters = a MERGED multi-tree crown
    (detector undercount), count = n clusters.
  - A crown covering exactly 1 cluster (even if that cluster had 18 raw points)
    = one tree, duplicates removed.

Outputs: a printed report, crowns_labeled.geojson, census_consensus.geojson
(load both in QGIS over the ortho to inspect).

Usage:
  python consensus_gt.py --ortho downsampled.tif --crowns tree_crowns.geojson \
      --census bbmp_tree_census_july_2026.geojson --out ./gt_out \
      --utm EPSG:32643 --gps-err 4 --exg-thresh 15 --match-tol 4
"""
import argparse, json, os
import numpy as np, rasterio, geopandas as gpd
from shapely.geometry import Point, box
from rasterio.warp import transform_bounds
from sklearn.cluster import DBSCAN


def load_census_in_bbox(path, bbox4326):
    """Return census points inside bbox (EPSG:4326), load-test junk dropped.

    If `path` is a spatially-indexed format (.gpkg / .fgb / .parquet), read ONLY
    the bbox via the driver's spatial index — O(bbox), not O(all points). Fall
    back to streaming the raw .geojson (O(n) text parse) only when no index
    exists. Build the index once with `index_census.py` to avoid the O(n) scan
    on every run.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in (".gpkg", ".fgb", ".geojsonl"):
        g = gpd.read_file(path, bbox=tuple(bbox4326)).to_crs("EPSG:4326")
    elif ext == ".parquet":
        g = gpd.read_parquet(path)
        w, s, e, n = bbox4326
        g = g.cx[w:e, s:n]
    else:  # raw .geojson — no spatial index, must stream every feature
        import ijson
        w, s, e, n = bbox4326
        lon, lat, name = [], [], []
        with open(path, "rb") as f:
            for feat in ijson.items(f, "features.item"):
                x, y = float(feat["geometry"]["coordinates"][0]), float(
                    feat["geometry"]["coordinates"][1])
                if w <= x <= e and s <= y <= n:
                    p = feat["properties"]
                    if str(p.get("KGISTreeID", "")).startswith("loadtesting"):
                        continue
                    lon.append(x); lat.append(y); name.append(p.get("TreeName"))
        return gpd.GeoDataFrame({"TreeName": name},
                                geometry=[Point(a, b) for a, b in zip(lon, lat)],
                                crs="EPSG:4326")
    if "KGISTreeID" in g.columns:
        g = g[~g["KGISTreeID"].astype(str).str.startswith("loadtesting")]
    return g[["TreeName", "geometry"]] if "TreeName" in g.columns else g


def exg_sampler(src, win=5):
    """Return f(list_of_UTM_points) -> ExG array, sampling the ortho (any CRS).

    Reads the MEDIAN ExG over a win x win pixel window centred on each point,
    not a single pixel. At 3-5 cm GSD one pixel is ~4 cm of ground: it can land
    in a gap between leaves, on a sunlit highlight or on a bare branch and
    mis-call a tree that is plainly there. A 5 px window is ~20 cm, still well
    inside a crown, and the median rejects those outliers instead of averaging
    them in. Crowns were already sampled over a 3x3 grid; this brings points to
    comparable footing.
    """
    from rasterio.windows import Window
    half = win // 2

    def f(geoms_utm, utm):
        g = gpd.GeoSeries(list(geoms_utm), crs=utm).to_crs(src.crs)
        out = []
        for pt in g:
            row, col = src.index(pt.x, pt.y)
            a = src.read(indexes=[1, 2, 3],
                         window=Window(col - half, row - half, win, win),
                         boundless=True, fill_value=0).astype(int)
            # ExG per pixel first, then median — median of the statistic we act on
            out.append(float(np.median(2 * a[1] - a[0] - a[2])))
        return np.array(out)
    return f


def crown_greenness(poly, sample, utm):
    """Mean ExG over a 3x3 grid of points inside the crown polygon."""
    minx, miny, maxx, maxy = poly.bounds
    pts = []
    for fx in (0.25, 0.5, 0.75):
        for fy in (0.25, 0.5, 0.75):
            p = Point(minx + (maxx - minx) * fx, miny + (maxy - miny) * fy)
            if poly.contains(p):
                pts.append(p)
    if not pts:
        pts = [poly.centroid]
    return float(np.mean(sample(pts, utm)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ortho", required=True)
    ap.add_argument("--crowns", required=True)
    ap.add_argument("--census", required=True)
    ap.add_argument("--out", default="./gt_out")
    ap.add_argument("--utm", default="EPSG:32643", help="metric CRS for the area")
    ap.add_argument("--gps-err", type=float, default=4.0, help="DBSCAN eps, metres")
    ap.add_argument("--exg-thresh", type=float, default=15.0, help="ExG canopy cutoff")
    ap.add_argument("--match-tol", type=float, default=4.0, help="crown<->point tol, m")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    UTM = a.utm

    src = rasterio.open(a.ortho)
    b = src.bounds
    bbox = transform_bounds(str(src.crs), "EPSG:4326", b.left, b.bottom, b.right, b.top)

    gp = load_census_in_bbox(a.census, bbox).to_crs(UTM)
    crowns = gpd.read_file(a.crowns).to_crs(UTM)
    print(f"census pts in ortho: {len(gp)}   crowns: {len(crowns)}")

    sample = exg_sampler(src)
    gp["exg"] = sample(gp.geometry.values, UTM)
    crowns["exg"] = [crown_greenness(p, sample, UTM) for p in crowns.geometry]

    # --- consensus: DBSCAN collapse duplicate survey points ---
    XY = np.array([[p.x, p.y] for p in gp.geometry])
    gp["cluster"] = DBSCAN(eps=a.gps_err, min_samples=1).fit(XY).labels_
    grp = gp.groupby("cluster")
    cent = grp.geometry.apply(lambda s: Point(np.mean([p.x for p in s]),
                                              np.mean([p.y for p in s])))
    clusters = gpd.GeoDataFrame(
        {"n_pts": grp.size().values, "exg": grp["exg"].mean().values},
        geometry=cent.values, crs=UTM)
    clusters["on_canopy"] = clusters.exg > a.exg_thresh
    print(f"consensus clusters: {len(clusters)} "
          f"(merged >=2 pts: {(clusters.n_pts>=2).sum()}, "
          f"max pts/cluster: {int(clusters.n_pts.max())})")

    # --- match crowns <-> clusters within tolerance ---
    buf = crowns.copy(); buf["geometry"] = crowns.buffer(a.match_tol)
    sj = gpd.sjoin(clusters.assign(cid=range(len(clusters))),
                   buf.assign(kid=range(len(buf))),
                   predicate="within", how="left")
    per_crown = sj.dropna(subset=["kid"]).groupby("kid").size()
    crowns["n_clusters"] = [int(per_crown.get(i, 0)) for i in range(len(crowns))]
    crowns["veg"] = crowns.exg > a.exg_thresh
    matched = set(sj.dropna(subset=["kid"])["cid"])
    clusters["matched"] = [i in matched for i in range(len(clusters))]

    TP = int((crowns.n_clusters >= 1).sum())
    FP = int(((crowns.n_clusters == 0) & (~crowns.veg)).sum())
    green_nopoint = int(((crowns.n_clusters == 0) & (crowns.veg)).sum())
    merged = int((crowns.n_clusters >= 2).sum())
    FN = int(((~clusters.matched) & clusters.on_canopy).sum())
    bad_census = int(((~clusters.matched) & (~clusters.on_canopy)).sum())
    usable = int(clusters.on_canopy.sum())

    print("\n=== CROWNS ===")
    print(f" validated by census (TP)          : {TP}")
    print(f" 0 census & low green (likely FP)   : {FP}")
    print(f" 0 census but green (census missed) : {green_nopoint}")
    print(f" cover >=2 clusters (merged trees)  : {merged}")
    print("=== CENSUS ===")
    print(f" consensus trees                    : {len(clusters)}")
    print(f" usable as GT (on canopy)           : {usable} "
          f"({100*usable/len(clusters):.0f}%)")
    print(f" unmatched & on canopy (FN, missed) : {FN}")
    print(f" unmatched & off canopy (bad point) : {bad_census}")
    print(f" precision TP/(TP+FP)               : {TP/max(1,TP+FP):.2f}")
    rec = clusters[clusters.on_canopy].matched.mean() if usable else 0
    print(f" recall matched/on-canopy           : {rec:.2f}")

    crowns[["Confidence_score", "area", "exg", "n_clusters", "veg", "geometry"]]\
        .to_file(f"{a.out}/crowns_labeled.geojson", driver="GeoJSON")
    clusters.assign(usable=clusters.on_canopy.values)\
        .to_file(f"{a.out}/census_consensus.geojson", driver="GeoJSON")
    print(f"\nsaved {a.out}/crowns_labeled.geojson + census_consensus.geojson")


if __name__ == "__main__":
    main()
