"""Is the BBMP tree census usable as ground truth? Species-consensus test.

The census stores one point per *recorded* tree, but points sit far closer
together than tree crowns do (median nearest-neighbour spacing 2.25 m, and
~72k points share identical coordinates). So a cluster of nearby points may be

  * several records of ONE tree, or
  * several genuinely distinct trees planted a metre or two apart.

We cannot tell those apart from the points alone. What we CAN test is whether
the records in a crown-sized neighbourhood at least *agree on the species*. If
they do, the neighbourhood is safe to treat as one labelled tree. If they do
not, no amount of detection work will make that label trustworthy.

Method
------
1. **Classify.** ``TreeName`` carries botanical authorities ("Pongamia Pinnata
   (L.) Pierre"). Strip them down to genus + species so spelling variants of the
   same tree vote together. "Others" carries no species information and is
   treated as unlabelled, not as a species.

2. **Group by distance.** Reproject to UTM 43N so distances are true metres,
   then single-linkage cluster at ``--eps`` metres (default 3.0 = a small crown
   radius). Implemented as connected components over a KD-tree radius graph,
   which is equivalent to DBSCAN with ``min_samples=1`` but much faster at 700k
   points.

3. **Vote.** Within each group, count the labelled points. If one species holds
   >= ``--agree`` (default 0.80) of them, the group is CONSENSUS. Otherwise it
   is AMBIGUOUS. Groups with no labelled points at all are UNLABELLED.

4. **Emit lat/lon** for every group so the result can be loaded into QGIS and
   checked against imagery.

Caveat that must be read before quoting any number
--------------------------------------------------
Single-linkage chains: a row of avenue trees spaced 2 m apart links into one
long group even though it is many trees. ``--eps`` therefore trades false
merging against false splitting, and the script prints the group-size
distribution so chaining is visible rather than hidden. Use ``--sweep`` to see
how the verdict moves with eps before settling on one.

Usage
-----
    python code/scripts/census_species_vote.py bbmp_tree_census.gpkg --sweep
    python code/scripts/census_species_vote.py bbmp_tree_census.gpkg \
        --eps 3.0 --agree 0.8 --out-prefix census_vote
"""

from __future__ import annotations

import argparse
import re
import sys


import numpy as np
import pandas as pd

# Bengaluru sits in UTM zone 43N. Distances in this CRS are true metres, unlike
# the source EPSG:4326 (degrees) or Web Mercator (inflated by 1/cos(lat)).
UTM_CRS = 32643

# Values that name no species. Compared after normalisation.
UNLABELLED = {"others", "other", "unknown", "na", "nil", ""}


def normalize_species(raw: str) -> str:
    """Reduce a census TreeName to a comparable 'genus species' key.

    "Pongamia Pinnata (L.) Pierre"                      -> "pongamia pinnata"
    "Acacia Nilotica (L.) Del. Subsp. Indica (Benth.)"  -> "acacia nilotica"
    "Terminalia Catappa L"                              -> "terminalia catappa"
    "Others"                                            -> ""   (unlabelled)

    Authorities and infraspecific ranks are dropped on purpose: they vary
    between records of the same tree and would split a genuine agreement into
    an artificial disagreement.
    """
    s = str(raw or "").strip().lower()
    if s in UNLABELLED:
        return ""
    s = re.sub(r"\([^)]*\)", " ", s)                    # drop (L.), (Benth.) ...
    s = re.sub(r"\b(subsp|var|ssp|cv|f)\.?\b.*", " ", s)  # drop infraspecifics
    s = re.sub(r"[^a-z\s]", " ", s)                      # drop punctuation/digits
    tokens = [t for t in s.split() if len(t) > 1]        # drop stray initials
    if not tokens:
        return ""
    key = " ".join(tokens[:2])                           # genus + species
    return "" if key in UNLABELLED else key


def group_by_distance(xy: np.ndarray, eps: float) -> np.ndarray:
    """Single-linkage groups at `eps` metres. Returns a group id per point."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree

    pairs = cKDTree(xy).query_pairs(eps, output_type="ndarray")
    if len(pairs) == 0:
        return np.arange(len(xy))
    n = len(xy)
    graph = coo_matrix(
        (np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)
    )
    _, labels = connected_components(graph, directed=False)
    return labels


def vote(df: pd.DataFrame, agree: float) -> pd.DataFrame:
    """One row per group: winning species, its share, and a verdict.

    Fully vectorised — a per-group Python loop takes minutes at 700k points
    and roughly half a million groups.
    """
    # per-group point counts and centroid
    agg = df.groupby("group", sort=True).agg(
        n_points=("species", "size"),
        lon=("lon", "mean"),
        lat=("lat", "mean"),
        spread_m=("spread", "max"),
        first_id=("KGISTreeID", "first"),
    )

    named = df.loc[df["species"] != ""]
    # winning species per group = largest (group, species) count
    pair = (named.groupby(["group", "species"], sort=False)
                 .size().rename("n").reset_index())
    pair = pair.sort_values(["group", "n"], ascending=[True, False])
    top = pair.drop_duplicates("group").set_index("group")

    stats = named.groupby("group").agg(
        n_labelled=("species", "size"),
        n_distinct_species=("species", "nunique"),
    )

    res = agg.join(stats).join(top[["species", "n"]])
    res["n_labelled"] = res["n_labelled"].fillna(0).astype(int)
    res["n_distinct_species"] = res["n_distinct_species"].fillna(0).astype(int)
    res["species"] = res["species"].fillna("")
    res["agreement"] = (res["n"] / res["n_labelled"]).fillna(0.0).round(3)

    # verdict
    res["status"] = np.where(
        res["n_labelled"] == 0, "UNLABELLED",
        np.where(res["n_points"] == 1, "SINGLE",
                 np.where(res["agreement"] >= agree, "CONSENSUS", "AMBIGUOUS")))

    res = res.drop(columns=["n"]).reset_index().rename(columns={"group": "group_id"})
    res["spread_m"] = res["spread_m"].round(2)
    return res[["group_id", "n_points", "n_labelled", "species", "agreement",
                "n_distinct_species", "status", "lon", "lat", "spread_m",
                "first_id"]]


def summarise(res: pd.DataFrame, eps: float, agree: float) -> dict:
    tot_g, tot_p = len(res), int(res["n_points"].sum())
    out = {"eps_m": eps, "agree": agree, "groups": tot_g, "points": tot_p}
    for st in ("CONSENSUS", "SINGLE", "AMBIGUOUS", "UNLABELLED"):
        sub = res[res["status"] == st]
        out[f"{st}_groups"] = len(sub)
        out[f"{st}_pct"] = round(100 * len(sub) / tot_g, 1) if tot_g else 0.0
    multi = res[res["n_points"] > 1]
    checkable = multi[multi["status"].isin(["CONSENSUS", "AMBIGUOUS"])]
    out["cross_checkable_groups"] = len(checkable)
    out["pass_rate_of_checkable"] = (
        round(100 * (checkable["status"] == "CONSENSUS").sum() / len(checkable), 1)
        if len(checkable) else 0.0
    )
    out["largest_group"] = int(res["n_points"].max()) if tot_g else 0
    out["groups_over_10_points"] = int((res["n_points"] > 10).sum())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("census", help="bbmp_tree_census.gpkg (or .geojson)")
    ap.add_argument("--eps", type=float, default=3.0,
                    help="grouping radius in metres (default 3.0)")
    ap.add_argument("--agree", type=float, default=0.80,
                    help="share needed for consensus (default 0.80)")
    ap.add_argument("--sweep", action="store_true",
                    help="report the verdict across several eps values and exit")
    ap.add_argument("--out-prefix", default="census_vote",
                    help="write <prefix>.csv and <prefix>.geojson")
    a = ap.parse_args()

    import geopandas as gpd

    print(f"reading {a.census} ...", flush=True)
    g = gpd.read_file(a.census)
    if "TreeName" not in g or "KGISTreeID" not in g:
        print("ERROR: expected columns TreeName and KGISTreeID", file=sys.stderr)
        return 2
    print(f"  {len(g):,} points, CRS {g.crs}")

    # ── 1. classify ────────────────────────────────────────────────────
    g["species"] = g["TreeName"].map(normalize_species)
    n_unl = int((g["species"] == "").sum())
    print(f"\nclassify: {g['TreeName'].nunique()} raw names -> "
          f"{g.loc[g['species'] != '', 'species'].nunique()} species keys")
    print(f"  unlabelled points ('Others' etc): {n_unl:,} "
          f"({100 * n_unl / len(g):.1f}%)")

    u = g.to_crs(UTM_CRS)
    xy = np.c_[u.geometry.x.values, u.geometry.y.values]
    wgs = g if (g.crs and g.crs.to_epsg() == 4326) else g.to_crs(4326)
    base = pd.DataFrame({
        "KGISTreeID": g["KGISTreeID"].values,
        "species": g["species"].values,
        "lon": wgs.geometry.x.values,
        "lat": wgs.geometry.y.values,
    })

    eps_list = [1.0, 2.0, 3.0, 4.0, 5.0, 7.0] if a.sweep else [a.eps]
    summaries = []
    res = None
    for eps in eps_list:
        print(f"\ngrouping at eps = {eps} m ...", flush=True)
        labels = group_by_distance(xy, eps)
        df = base.copy()
        df["group"] = labels
        # max distance of any member from its group centroid, in metres
        cx = pd.Series(xy[:, 0]).groupby(labels).transform("mean").values
        cy = pd.Series(xy[:, 1]).groupby(labels).transform("mean").values
        df["spread"] = np.hypot(xy[:, 0] - cx, xy[:, 1] - cy)
        res = vote(df, a.agree)
        s = summarise(res, eps, a.agree)
        summaries.append(s)
        print(f"  {s['groups']:,} groups from {s['points']:,} points | "
              f"CONSENSUS {s['CONSENSUS_pct']}%  SINGLE {s['SINGLE_pct']}%  "
              f"AMBIGUOUS {s['AMBIGUOUS_pct']}%  UNLABELLED {s['UNLABELLED_pct']}%")
        print(f"  of groups that can actually be cross-checked "
              f"({s['cross_checkable_groups']:,}), "
              f"{s['pass_rate_of_checkable']}% agree at >= {a.agree:.0%}")
        print(f"  largest group {s['largest_group']} points; "
              f"{s['groups_over_10_points']:,} groups exceed 10 points "
              f"(watch for single-linkage chaining)")

    sw = pd.DataFrame(summaries)
    print("\n=== sensitivity to eps ===")
    print(sw[["eps_m", "groups", "CONSENSUS_pct", "SINGLE_pct", "AMBIGUOUS_pct",
              "UNLABELLED_pct", "pass_rate_of_checkable", "largest_group"]]
          .to_string(index=False))

    if a.sweep:
        sw.to_csv(f"{a.out_prefix}_sweep.csv", index=False)
        print(f"\nwrote {a.out_prefix}_sweep.csv — pick an eps, then rerun without --sweep")
        return 0

    # ── 4. outputs for QGIS ────────────────────────────────────────────
    res = res.sort_values(["status", "n_points"], ascending=[True, False])
    csv_path = f"{a.out_prefix}.csv"
    res.to_csv(csv_path, index=False)

    gj = gpd.GeoDataFrame(
        res, geometry=gpd.points_from_xy(res["lon"], res["lat"]), crs=4326
    )
    gj_path = f"{a.out_prefix}.geojson"
    gj.to_file(gj_path, driver="GeoJSON")

    print(f"\nwrote {csv_path} and {gj_path}  ({len(res):,} rows)")
    print("In QGIS: style by `status` — AMBIGUOUS are the ones to eyeball first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
