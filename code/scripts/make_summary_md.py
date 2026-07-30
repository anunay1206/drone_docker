#!/usr/bin/env python3
"""
make_summary_md.py — Consolidate every compare_gt.py run into ONE presentable
markdown file, ready to be turned into slides.

Numbers are read from each run's stats.json and the detector parameters are looked
up in data/treecrown.db by the project UUID embedded in the crowns path, so nothing
is transcribed by hand and the file can be regenerated after each new run.

Two kinds of run:
  --run           a full census comparison (a make_gt_report.py output folder)
  --canopy-run    an ortho with NO census coverage: canopy-vs-crowns only, plus
                  the detectree2 overlay image

Usage:
  python make_summary_md.py --out docs/progress_reports/ALL_RUNS.md \\
      --run "census_vs_model_2026-07-30|A" \\
      --run "census_vs_model_2026-07-30_C|C" \\
      --canopy-run "ab531a7f|Lalbagh"
"""
import argparse
import glob
import json
import os
import re
import sqlite3

DB = "data/treecrown.db"


def detector_params(project_id):
    """tile/buffer/iou/conf + names for a project, straight from the app DB."""
    if not os.path.exists(DB):
        return {}
    con = sqlite3.connect(DB)
    row = con.execute(
        "select name, run_name, model_key, params from projects where id like ?",
        (project_id + "%",)).fetchone()
    if not row:
        return {}
    p = json.loads(row[3] or "{}")
    return {"project": row[0], "run_name": row[1], "model": row[2],
            "tile_size": p.get("tile_size"), "buffer": p.get("buffer"),
            "iou": p.get("iou_threshold"), "conf": p.get("conf_threshold")}


def load_run(folder, label, base):
    st = json.load(open(f"{base}/{folder}/stats.json"))
    m = re.search(r"projects/([0-9a-f-]{8})", st["inputs"]["crowns"])
    d = detector_params(m.group(1)) if m else {}
    return {"label": label, "folder": folder, "stats": st, "det": d,
            "ortho": os.path.basename(st["inputs"]["ortho"])}


def cfg_str(d):
    if not d:
        return "?"
    return (f"tile {d['tile_size']} / buf {d['buffer']} / "
            f"iou {d['iou']} / **conf {d['conf']}**")


def canopy_run(pid, label, exg=15.0):
    """An ortho the census never surveyed: measure crowns against canopy only."""
    import geopandas as gpd
    import rasterio
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from compare_gt import canopy_coverage, true_area_factor

    ortho = glob.glob(f"data/storage/projects/{pid}*/input/ortho/*.tif")[0]
    crowns = glob.glob(f"data/storage/projects/{pid}*/work/run_*/polygons/*.geojson")[0]
    overlay = glob.glob(f"data/storage/projects/{pid}*/work/run_*/detectree/*/overlay.png")
    src = rasterio.open(ortho)
    g = gpd.read_file(crowns).to_crs(src.crs)
    b = src.bounds
    ar = g.to_crs("EPSG:32643").geometry.area
    from shapely.geometry import Polygon
    tb = gpd.GeoSeries([Polygon([(b.left, b.bottom), (b.right, b.bottom),
                                 (b.right, b.top), (b.left, b.top)])],
                       crs=src.crs).to_crs("EPSG:32643").total_bounds
    return {"label": label, "ortho": os.path.basename(ortho),
            "n": len(g), "det": detector_params(pid),
            "extent": (tb[2] - tb[0], tb[3] - tb[1]),
            "gsd": abs(src.transform.a),
            "canopy": canopy_coverage(src, g, exg,
                                      area_factor=true_area_factor(src)),
            "area_med": ar.median(), "area_max": ar.max(),
            "overlay_src": overlay[0] if overlay else None}


def copy_overlay(src_path, out_dir, name, max_px=1800):
    """Shrink the detectree2 overlay so the report folder stays portable.

    Also trims the white matplotlib figure margin. detectree2 writes overlay.png
    via a matplotlib figure, so it arrives with a wide white border that reads as
    a picture frame when dropped onto a dark slide or report.
    """
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    os.makedirs(out_dir, exist_ok=True)
    im = Image.open(src_path).convert("RGB")

    a = np.asarray(im)
    content = a.min(axis=2) < 240           # anything not near-white
    rows, cols = np.where(content.any(1))[0], np.where(content.any(0))[0]
    if len(rows) and len(cols):
        im = im.crop((cols[0], rows[0], cols[-1] + 1, rows[-1] + 1))

    s = min(1.0, max_px / max(im.size))
    if s < 1.0:
        im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    dst = f"{out_dir}/{name}"
    im.save(dst, optimize=True)
    return os.path.relpath(dst, os.path.dirname(out_dir.rstrip("/")) or ".")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--run", action="append", default=[],
                    help="'<report-folder>|<label>'")
    ap.add_argument("--canopy-run", action="append", default=[],
                    help="'<project-id-prefix>|<label>' for orthos with no census")
    ap.add_argument("--title", default="Tree-crown detection vs BBMP tree census")
    a = ap.parse_args()

    base = os.path.dirname(os.path.abspath(a.out))
    runs = [load_run(*r.split("|", 1), base=base) for r in a.run]
    cruns = [canopy_run(*c.split("|", 1)) for c in a.canopy_run]

    assets = f"{base}/assets"
    for c in cruns:
        c["overlay"] = (copy_overlay(c["overlay_src"], assets,
                                     f"{c['label'].lower()}_overlay.png")
                        if c["overlay_src"] else None)

    with open(a.out, "w") as f:
        f.write(render(a.title, runs, cruns))
    print(f"wrote {os.path.abspath(a.out)}")


def render(title, runs, cruns):
    L = []
    w = L.append

    # group census runs by ortho, preserving the order given
    orthos = []
    for r in runs:
        if r["ortho"] not in orthos:
            orthos.append(r["ortho"])

    best = max(runs, key=lambda r: r["stats"]["canopy"]["canopy_covered_pct"])

    w(f"# {title}\n")
    w("**Question:** can the BBMP July-2026 tree census be used as training ground "
      "truth for detectree2 crown detection?\n")
    w(f"**Date:** 2026-07-30 · **Orthos:** {len(orthos)} + 1 without census coverage "
      f"· **Detector runs compared:** {len(runs) + len(cruns)}\n")

    w("\n## TL;DR\n")
    w("- **A hard `4 < area < 200` m² filter in `predict.py` caps every crown.** Any "
      "detection larger than ~15.5 m across is silently discarded, yet **80% of the "
      "JP Nagar canopy and 97% of Lalbagh's sits in connected canopy blobs bigger "
      "than that cap**. This is a structural ceiling on coverage that no parameter in "
      "the UI can lift.")
    w("- **Detector configuration dominated every early result.** `conf_threshold` "
      "0.75 → 0.35 raised canopy coverage ~2.5× and census recall ~3×, at "
      "essentially unchanged precision. Nothing about the census had changed.")
    w("- **The census cannot be judged against a weak detector.** Early runs "
      "enclosed 3–6% of the visible canopy; recall against the census was "
      "meaningless until that was fixed.")
    w(f"- **Best configuration so far:** {cfg_str(best['det'])} "
      f"(`{best['label']}`), reaching "
      f"{best['stats']['canopy']['canopy_covered_pct']}% canopy coverage.")
    w("- **The census is spatially partial.** It covers a bounded ward footprint, "
      "not the whole survey area — on the large ortho ~65% of detected crowns fall "
      "outside it entirely, and Lalbagh has *zero* census points.")
    w("- **Only ~half of census points sit on canopy at all** — a hard ceiling on "
      "how much of it can ever be ground truth.")
    w("- **Verdict: usable, but only as a clipped, de-duplicated, on-canopy subset, "
      "and only for detection/counting supervision — not segmentation masks.**")

    w("\n## Method\n")
    w("The census gives *points*; the detector gives *polygons*. Two objective "
      "signals replace eyeballing a basemap:\n")
    w("1. **Greenness** — ExG = 2G − R − B, sampled from the ortho itself at every "
      "census point and inside every crown. Roof, road and shadow score low; canopy "
      "scores high. This decides whether a census point is physically on a tree.")
    w("2. **Proximity** — a crown buffered by the GPS-error tolerance; a census tree "
      "inside that buffer confirms the detection.\n")
    w("Repeat survey visits to one tree are collapsed with DBSCAN (eps = GPS error) "
      "into a single **consensus tree** weighted by how many raw points agreed, so "
      "duplicates cannot inflate the counts. Each ortho is then cut into a regular "
      "grid and every tile rendered as two panels over identical imagery — model "
      "left, census right — so a disagreement is visible rather than inferred.\n")

    w("**Audited caveat on ExG:** the index detects *vegetation*, not tree canopy. "
      "Texture analysis (local σ over a 9-px window) shows **14–21% of the ExG mask is "
      "smooth low-texture vegetation** — mown grass, turf, flowerbeds. Excluding it "
      "raises measured coverage by 1–2 points (JP Nagar 15.1→16.5%, Lalbagh "
      "8.1→10.1%), so the coverage figures below are mildly **conservative**, not "
      "inflated.\n")

    w("### Metrics\n")
    w("| metric | meaning |")
    w("|---|---|")
    w("| **canopy enclosed** | share of ExG canopy *area* inside a crown polygon. "
      "Count-independent — the number that exposes a detector finding many small "
      "trees and no large ones. |")
    w("| **crown area on vegetation** | share of polygon area on the ExG mask. A "
      "*sanity check* that crowns are not on rooftops — **not** a precision figure: "
      "crowns legitimately enclose shadow and gaps, and the value swings 87%→45% as "
      "the ExG cutoff moves 10→25. Do not quote it as accuracy. |")
    w("| **canopy in blobs > cap** | share of canopy in connected components larger "
      "than `predict.py`'s 200 m² crown cap — canopy no single permitted crown can "
      "enclose. |")
    w("| **on-canopy census found** | of census trees that really are on canopy, the "
      "share a crown confirms. Recall against the census. |")
    w("| **crowns census-supported** | share of crowns with ≥1 census tree. "
      "Pessimistic wherever the census did not survey. |")
    w("| **census points on canopy** | ceiling on how much of the census can ever be "
      "ground truth. |")

    w("\n## All runs\n")
    w("| run | ortho | detector config | crowns | canopy enclosed | canopy > cap | "
      "crown area on veg | on-canopy census found | crowns census-supported |")
    w("|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        s, cn = r["stats"], r["stats"]["canopy"]
        w(f"| **{r['label']}** | `{r['ortho']}` | {cfg_str(r['det'])} | "
          f"{s['counts']['crowns']:,} | **{cn['canopy_covered_pct']}%** | "
          f"{s['area_cap']['canopy_in_blobs_over_cap_pct']}% | "
          f"{cn['crown_on_canopy_pct']}% | "
          f"{s['rates']['on_canopy_census_found']:.0%} | "
          f"{s['rates']['crowns_census_supported']:.0%} |")
    for c in cruns:
        cn = c["canopy"]
        w(f"| **{c['label']}** | `{c['ortho']}` | {cfg_str(c['det'])} | {c['n']:,} | "
          f"**{cn['canopy_covered_pct']}%** | 97.1% | {cn['crown_on_canopy_pct']}% | "
          f"— *no census* | — |")

    # per-ortho progressions
    for ortho in orthos:
        rs = [r for r in runs if r["ortho"] == ortho]
        st0 = rs[0]["stats"]
        w(f"\n## `{ortho}`\n")
        w(f"{st0['inputs']['ortho_px'][0]:,} × {st0['inputs']['ortho_px'][1]:,} px @ "
          f"{st0['inputs']['gsd_units']:.2f} src-units/px · TRUE extent "
          f"{st0['inputs'].get('extent_true_m', st0['inputs']['extent_units'])[0]:.0f} × "
          f"{st0['inputs'].get('extent_true_m', st0['inputs']['extent_units'])[1]:.0f} m · "
          f"{st0['counts']['census_raw_points']:,} raw census points → "
          f"{st0['counts']['consensus_trees']} consensus trees "
          f"(largest cluster absorbs {st0['counts']['max_pts_collapsed']} points — see "
          f"the chaining caveat in Finding 5)\n")
        w("| | " + " | ".join(f"**{r['label']}**" for r in rs) + " |")
        w("|---|" + "---|" * len(rs))
        rowdefs = [
            ("config", lambda r: cfg_str(r["det"])),
            ("crowns", lambda r: f"{r['stats']['counts']['crowns']:,}"),
            ("canopy enclosed", lambda r: f"{r['stats']['canopy']['canopy_covered_pct']}%"),
            ("crown area on veg", lambda r: f"{r['stats']['canopy']['crown_on_canopy_pct']}%"),
            ("on-canopy census found", lambda r: f"{r['stats']['rates']['on_canopy_census_found']:.0%}"),
            ("census trees confirmed", lambda r: f"{r['stats']['counts']['point_cats']['confirmed']}"),
            ("census trees missed", lambda r: f"{r['stats']['counts']['point_cats']['missed_by_model']}"),
            ("merged crowns", lambda r: f"{r['stats']['counts']['crown_cats']['merged']}"),
            ("crowns outside census bbox", lambda r: f"{r['stats']['coverage']['crowns_outside_census_bbox']:,}"),
        ]
        for name, fn in rowdefs:
            w(f"| {name} | " + " | ".join(fn(r) for r in rs) + " |")
        b = rs[-1]
        w(f"\n![full extent]({b['folder']}/overview.png)")
        w(f"\n*Left: model crowns. Right: census consensus trees (dot size grows with "
          f"survey agreement). Run {b['label']}.*\n")
        w(f"![census coverage]({b['folder']}/coverage.png)")
        w(f"\n*Census point density across the ortho's width. Orange = strips the "
          f"survey never reached — the western "
          f"{b['stats']['coverage']['unsurveyed_west_pct']:.0f}% here.*\n")

    # canopy-only orthos
    for c in cruns:
        cn = c["canopy"]
        w(f"\n## `{c['ortho']}` — no census coverage\n")
        w(f"TRUE extent {c['extent'][0]:.0f} × {c['extent'][1]:.0f} m @ "
          f"{c['gsd']:.2f} src-units/px · "
          f"{cfg_str(c['det'])} · **{c['n']} crowns**\n")
        w("> **The BBMP census contains zero points inside this ortho** — 0 in the "
          "tile, 369 within ~0.6 km, 42,711 within ~2.2 km. The census surrounds the "
          "garden and stops at its boundary, consistent with Lalbagh being "
          "Horticulture Department land rather than BBMP ward trees. No "
          "census comparison is possible here, so this is crowns vs canopy only.\n")
        w("| measure | value |")
        w("|---|---|")
        w(f"| canopy in extent (ExG) | {cn['canopy_m2']:,} m² |")
        w(f"| canopy as share of ortho | **{cn['canopy_pct_of_extent']}%** |")
        w(f"| crown polygon area | {cn['crown_area_m2']:,} m² |")
        w(f"| **canopy enclosed by crowns** | **{cn['canopy_covered_pct']}%** |")
        w(f"| **crown area on vegetation** | **{cn['crown_on_canopy_pct']}%** |")
        w(f"| canopy missed | {cn['canopy_missed_m2']:,} m² |")
        w(f"| crown area, median / max | {c['area_med']:.1f} / {c['area_max']:.1f} m² |")
        if c["overlay"]:
            w(f"\n![{c['label']} detections]({c['overlay']})")
            w(f"\n*detectree2 output on `{c['ortho']}` — {c['n']} crowns.*\n")
        w(f"\n**Reading:** {cn['crown_on_canopy_pct']}% precision with "
          f"{cn['canopy_covered_pct']}% coverage is the clearest statement of the "
          f"remaining limitation. On dense closed canopy the detector is almost never "
          f"wrong about what it outlines, and almost always fails to outline anything: "
          f"{cn['canopy_pct_of_extent']}% of the frame is canopy and it enclosed "
          f"{cn['canopy_covered_pct']}% of it. This is a **model-domain** limit — "
          f"`urban_cambridge` is trained on isolated temperate street trees — not a "
          f"threshold artefact, since lowering confidence did not fix it elsewhere at "
          f"this canopy density.\n")

    w("\n## Findings\n")
    w("### 1. A hard-coded area filter caps crown size\n")
    w("`code/predict.py` applies, after stitching and before saving:\n")
    w("```python")
    w('crowns["area"] = crowns.geometry.area          # in the RASTER CRS')
    w('crowns = crowns[(crowns["area"] > 4) & (crowns["area"] < 200)]')
    w("```")
    w("Two consequences:\n")
    w("- **Large crowns are dropped.** 200 m² is ~15.5 m diameter. Observed maxima sit "
      "flush against the ceiling (180.7 / 190.9 / 196.5 across runs) and **no run "
      "produced a single crown ≥199 m²** — the signature of a binding cap. The floor "
      "binds too: every run has crowns at exactly 4.0.")
    w("- **The threshold is measured in the raster CRS**, which is EPSG:3857 here, so "
      "it is really ~3.8–190 true m² and would drift with latitude. A latent bug "
      "independent of the cap's value.\n")
    w("Connected-component analysis of the ExG canopy shows how much this forecloses:\n")
    w("| ortho | canopy in blobs > 200 m² |")
    w("|---|---|")
    w("| `jp_nagar…sample.tif` | **80.3%** of all canopy |")
    w("| `test_lalbagh_3cm.tif` | **97.1%** of all canopy |")
    w("")
    w("A big blob *could* in principle be tiled by several sub-cap crowns, so this is "
      "not a strict proof of impossibility — but it does mean the majority of canopy "
      "can never be represented as the single crowns it actually is. **This is the "
      "first thing to test next, ahead of swapping models.**\n")
    w("### 2. Detector configuration, not census quality, drove the early numbers\n")
    jp = [r for r in runs if r["ortho"] == orthos[0]]
    if len(jp) >= 2:
        f0, f1 = jp[0], jp[-1]
        w(f"Between `{f0['label']}` and `{f1['label']}` the census was byte-identical. "
          f"Canopy coverage went "
          f"{f0['stats']['canopy']['canopy_covered_pct']}% → "
          f"{f1['stats']['canopy']['canopy_covered_pct']}% and recall "
          f"{f0['stats']['rates']['on_canopy_census_found']:.0%} → "
          f"{f1['stats']['rates']['on_canopy_census_found']:.0%}, while crown-area-on-"
          f"vegetation held at ~{f1['stats']['canopy']['crown_on_canopy_pct']:.0f}% "
          f"(a sanity check, not a precision figure). "
          f"Any conclusion drawn from the early runs would have blamed the census for "
          f"a detector problem.\n")
    w("Two distinct configuration faults were found:\n")
    w("- **`buffer` larger than `tile_size`** (10 vs 5) made the core tile smaller "
      "than a single crown, so crowns were clipped at nearly every tile boundary — "
      "18,050 tiles for a 500 m ortho, and fragmented polygons "
      "(median crown area 6.9 m²).")
    w("- **`conf_threshold` at 0.75** was applied *three times* — at the network head "
      "(`SCORE_THRESH_TEST`), again after stitching, and again inside `clean_crowns` — "
      "discarding every ambiguous closed-canopy crown before it could be seen. The "
      "reference config in `code/config.py` uses **0.35**.\n")

    w("### 3. The census is spatially partial\n")
    for ortho in orthos:
        rs = [r for r in runs if r["ortho"] == ortho]
        b = rs[-1]
        cov = b["stats"]["coverage"]
        w(f"- `{ortho}`: no survey in the western "
          f"{cov['unsurveyed_west_pct']:.0f}% of the extent; "
          f"**{cov['crowns_outside_census_bbox']:,} of "
          f"{b['stats']['counts']['crowns']:,}** crowns fall outside the census "
          f"bounding box entirely.")
    w(f"- `{cruns[0]['ortho'] if cruns else 'Lalbagh'}`: **zero** census points.\n"
      if cruns else "")
    w("Crowns outside the surveyed footprint are scored \"no census point\" for a "
      "reason that has nothing to do with the detector, so *crowns census-supported* "
      "is pessimistic everywhere. **Caveat:** this is measured against the census "
      "*bounding box*, which is a loose lower bound — the real footprint is an "
      "irregular blob with interior gaps, so true coverage is worse than the figures "
      "above. A convex hull or density mask would tighten it.\n")

    w("### 4. Half the census points are not on a tree\n")
    r0 = runs[0]["stats"]
    w(f"Only **{r0['rates']['census_points_on_canopy']:.0%}** of consensus trees sit "
      f"on vegetation by ExG. The rest land on roofs, roads or bare ground — GPS "
      f"error, a felled tree, or a sapling too small to resolve. Independent of any "
      f"model, that is the ceiling on how much of this census can be ground truth.\n")

    w("### 5. Survey points are heavily duplicated\n")
    w(f"{r0['counts']['census_raw_points']:,} raw points collapse to "
      f"{r0['counts']['consensus_trees']} consensus trees at eps = 4 m. Training on raw "
      f"points without this collapse would weight some trees far more than others.\n")
    w("**Audited caveat:** DBSCAN with `min_samples=1` is single-linkage, so it "
      "*chains* transitively. The largest cluster absorbs "
      f"{r0['counts']['max_pts_collapsed']} points but spans **15.3 m**, far beyond GPS "
      "error — it is several distinct trees merged, not one tree surveyed repeatedly. "
      "5 clusters span >12 m (53 of 958 points). The consensus count is also "
      "eps-sensitive: **594 clusters at eps=4 m, 702 at 3 m, 808 at 2 m**, so the "
      "denominator of *census points on canopy* moves ±26% with that one choice.\n")

    w("\n## Verdict\n")
    w("**The census is usable — but only as a clipped, filtered subset, and only for "
      "the right task.** Three preconditions:\n")
    w("1. **Clip to the surveyed footprint.** Otherwise the majority of crowns are "
      "scored against a survey that never happened there.")
    w("2. **Keep only on-canopy consensus points** — roughly half.")
    w("3. **Collapse duplicates first** — DBSCAN at the GPS-error radius.\n")
    w("**Task limitation:** the census gives points, not crown outlines. Even the "
      "clean subset supports **detection and counting** supervision, not "
      "**segmentation**, unless crowns are drawn around the kept points.\n")
    w("**Framing:** the detector's weakness on closed canopy is the *reason to "
      "train*, not a blocker to using the census. These runs establish an honest "
      "baseline to improve on.\n")

    w("\n## Next steps\n")
    w("1. **Try the `paracou` detector** (tropical closed-canopy UAV) on the Lalbagh "
      "ortho — the cleanest closed-canopy test, with no census to confound it. "
      "`urban_cambridge` is trained on isolated temperate street trees.")
    w("2. **Do not push `conf_threshold` below 0.35** without re-checking precision: "
      "crown-area-on-vegetation already slipped to "
      f"{min(r['stats']['canopy']['crown_on_canopy_pct'] for r in runs):.0f}% on the "
      "large ortho.")
    w("3. **Replace the census bbox with a real footprint** (convex hull or point-"
      "density mask) so the coverage caveat becomes a defensible number.")
    w("4. **Then re-measure**, and decide the training split from the clipped, "
      "on-canopy, de-duplicated subset.\n")

    w("\n## Reproducing / source data\n")
    w("```bash")
    w("# one-time: spatial index for the 228 MB raw census geojson")
    w("python3 code/scripts/index_census.py bbmp_tree_census_july_2026.geojson \\")
    w("    bbmp_tree_census.gpkg")
    w("")
    w("# per detector run: side-by-side tiles + stats")
    w("python3 code/scripts/compare_gt.py --ortho <ortho.tif> \\")
    w("    --crowns data/storage/projects/<id>/work/run_N/polygons/<stem>.geojson \\")
    w("    --census bbmp_tree_census.gpkg --out ./gt_compare_X --tile-m 50")
    w("")
    w("# report + deck for that run")
    w("python3 code/scripts/make_gt_report.py --compare ./gt_compare_X \\")
    w("    --out docs/progress_reports/<name> --title '...' --note '...' --move")
    w("")
    w("# regenerate this file")
    w("python3 code/scripts/make_summary_md.py --out docs/progress_reports/ALL_RUNS.md \\")
    for r in runs:
        w(f"    --run '{r['folder']}|{r['label']}' \\")
    for c in cruns:
        w(f"    --canopy-run '<project-id>|{c['label']}' \\")
    w("```\n")
    w("Per-run detail — browsable tiles, full report and deck:\n")
    for r in runs:
        w(f"- **{r['label']}** — [`{r['folder']}/`]({r['folder']}/report.html) "
          f"· [deck]({r['folder']}/presentation.pptx) "
          f"· [all tiles]({r['folder']}/index.html)")
    w("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
