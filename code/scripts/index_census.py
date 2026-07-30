#!/usr/bin/env python3
"""
index_census.py — ONE-TIME: convert the raw BBMP census .geojson (no spatial
index, ~238 MB) into a spatially-indexed GeoPackage (.gpkg, ~97 MB, R-tree).

Raw GeoJSON has no spatial index, so every bbox query must re-parse all ~702k
features (~2.5 s each run). After this conversion, consensus_gt.py reads only a
tile's bbox from the .gpkg in ~0.04 s (≈60x faster) and never re-parses the
whole file. Run this once; commit the .gpkg or keep it beside the raw file.

Also drops the `loadtesting*` junk rows here so downstream never sees them.

Usage:
  python index_census.py bbmp_tree_census_july_2026.geojson bbmp_tree_census.gpkg
"""
import sys, time, geopandas as gpd, ijson


def main(src, dst):
    t = time.time()
    lon, lat, name, kid = [], [], [], []
    with open(src, "rb") as f:
        for ft in ijson.items(f, "features.item"):
            x, y = ft["geometry"]["coordinates"]
            p = ft["properties"]
            lon.append(float(x)); lat.append(float(y))
            name.append(p.get("TreeName")); kid.append(str(p.get("KGISTreeID")))
    gdf = gpd.GeoDataFrame(
        {"TreeName": name, "KGISTreeID": kid},
        geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326")
    n0 = len(gdf)
    gdf = gdf[~gdf.KGISTreeID.str.startswith("loadtesting")].reset_index(drop=True)
    print(f"parsed {n0} -> kept {len(gdf)} (dropped {n0-len(gdf)} loadtesting) "
          f"in {time.time()-t:.1f}s")
    t = time.time()
    drv = "GPKG" if dst.lower().endswith(".gpkg") else None
    gdf.to_file(dst, driver=drv)   # GPKG writes an R-tree spatial index
    print(f"wrote {dst} in {time.time()-t:.1f}s")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: index_census.py <raw.geojson> <out.gpkg>")
    main(sys.argv[1], sys.argv[2])
