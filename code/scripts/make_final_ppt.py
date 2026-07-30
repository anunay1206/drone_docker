#!/usr/bin/env python3
"""
make_final_ppt.py — Build the short final-presentation deck.

Deliberately narrow in scope, unlike make_gt_report.py (per-run reports) and
make_summary_md.py (the full run log). The story here is fixed:

  1-2  how the census GeoJSON was clipped to each ortho's footprint
  3-4  the crown / point categories and the rule that assigns them
  5    how duplicate points and multi-point crowns were handled
  6+   model crowns vs census-marked crowns, tile by tile

Category colours are imported from compare_gt so the swatches on the slides are
the exact RGB values used to draw the tiles — they cannot drift apart.

Usage:
  python make_final_ppt.py --out docs/progress_reports/FINAL_census_vs_model.pptx
"""
import argparse
import json
import os
import sys

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare_gt import CROWN_CATS, POINT_CATS  # noqa: E402  same RGB as the tiles

DARK = RGBColor(0x16, 0x18, 0x15)
LIGHT = RGBColor(0xE8, 0xE8, 0xE2)
MUTED = RGBColor(0x9A, 0x9C, 0x95)
GREEN = RGBColor(0x6E, 0xBE, 0x78)
AMBER = RGBColor(0xF0, 0xAA, 0x3C)
MONO = "Consolas"

REPORTS = "docs/progress_reports"
RUNS = {
    "A": "census_vs_model_2026-07-30",
    "B": "census_vs_model_2026-07-30_tile40",
    "C": "census_vs_model_2026-07-30_C",
    "D": "census_vs_model_2026-07-30_bigortho",
    "E": "census_vs_model_2026-07-30_bigortho_C",
}


def load(label):
    return json.load(open(f"{REPORTS}/{RUNS[label]}/stats.json"))


class Deck:
    def __init__(self):
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Inches(13.333), Inches(7.5)

    def slide(self, title, sub=""):
        s = self.prs.slides.add_slide(self.prs.slide_layouts[6])
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = DARK
        tb = s.shapes.add_textbox(Inches(.55), Inches(.28), Inches(12.2), Inches(1.2))
        tf = tb.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = title
        p.font.size, p.font.bold, p.font.color.rgb = Pt(29), True, LIGHT
        if sub:
            q = tf.add_paragraph()
            q.text = sub
            q.font.size, q.font.color.rgb = Pt(14.5), MUTED
        return s

    def bullets(self, s, items, top=1.55, left=.6, width=12.1, size=16.5):
        """items: (text, kind) with kind in {h, b, sub, code, warn}.

        The box is sized to the space actually left on the slide, so no frame
        ever extends past the bottom edge.
        """
        avail = self.prs.slide_height.inches - top - .25
        tb = s.shapes.add_textbox(Inches(left), Inches(top), Inches(width),
                                  Inches(max(.5, avail)))
        tf = tb.text_frame
        tf.word_wrap = True
        first = True
        for txt, kind in items:
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            p.text = txt
            p.font.size = Pt(size if kind != "sub" else size - 3)
            p.font.bold = kind == "h"
            p.font.color.rgb = {"h": GREEN, "warn": AMBER,
                                "sub": MUTED}.get(kind, LIGHT)
            if kind == "code":
                p.font.name = MONO
                p.font.size = Pt(size - 2.5)
            if kind in ("sub", "code"):
                p.level = 1
            p.space_after = Pt(9 if kind != "sub" else 5)
        return tb

    def swatch_rows(self, s, cats, rules, top=1.75, left=.7, w=12.0, rh=.66):
        """One row per category: the exact tile colour, the key, and the rule."""
        for i, (key, (label, rgb)) in enumerate(cats.items()):
            y = top + i * rh
            box = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left),
                                     Inches(y), Inches(.34), Inches(.34))
            box.fill.solid()
            box.fill.fore_color.rgb = RGBColor(*rgb)
            box.line.color.rgb = RGBColor(0xD0, 0xD0, 0xCC)
            box.line.width = Pt(.75)
            box.text_frame.text = ""
            tb = s.shapes.add_textbox(Inches(left + .52), Inches(y - .07),
                                      Inches(w - .6), Inches(.5))
            tf = tb.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            r1 = p.add_run(); r1.text = f"{key}   "
            r1.font.name, r1.font.size, r1.font.bold = MONO, Pt(14), True
            r1.font.color.rgb = RGBColor(*rgb)
            r2 = p.add_run(); r2.text = f"“{label}”    "
            r2.font.size, r2.font.color.rgb = Pt(13.5), LIGHT
            r3 = p.add_run(); r3.text = rules[key]
            r3.font.size, r3.font.color.rgb = Pt(13.5), MUTED
            r3.font.italic = True

    def table(self, s, head, rows, top=1.7, left=.65, widths=None, size=13):
        n, m = len(rows) + 1, len(head)
        widths = widths or [12.0 / m] * m
        tbl = s.shapes.add_table(n, m, Inches(left), Inches(top),
                                 Inches(sum(widths)), Inches(.4 * n)).table
        for j, wd in enumerate(widths):
            tbl.columns[j].width = Inches(wd)
        for j, h in enumerate(head):
            c = tbl.cell(0, j)
            c.text = str(h)
            c.fill.solid()
            c.fill.fore_color.rgb = RGBColor(0x2A, 0x2E, 0x27)
            pr = c.text_frame.paragraphs[0]
            pr.font.size, pr.font.bold, pr.font.color.rgb = Pt(size), True, GREEN
        for i, row in enumerate(rows, 1):
            for j, v in enumerate(row):
                c = tbl.cell(i, j)
                c.text = str(v)
                c.fill.solid()
                c.fill.fore_color.rgb = RGBColor(0x1E, 0x21, 0x1C)
                pr = c.text_frame.paragraphs[0]
                pr.font.size, pr.font.color.rgb = Pt(size), LIGHT
                if j > 0:
                    pr.alignment = PP_ALIGN.RIGHT
        return tbl

    def picture(self, s, path, top=1.6, max_h=5.4, max_w=12.1):
        if not os.path.exists(path):
            return
        iw, ih = Image.open(path).size
        k = min(max_w / (iw / 96), max_h / (ih / 96))
        w, h = (iw / 96) * k, (ih / 96) * k
        s.shapes.add_picture(path, Inches(.6 + (max_w - w) / 2), Inches(top),
                             Inches(w), Inches(h))

    def caption(self, s, text, top=6.85):
        tb = s.shapes.add_textbox(Inches(.6), Inches(top), Inches(12.1), Inches(.5))
        tf = tb.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = text
        p.font.size, p.font.color.rgb = Pt(12), MUTED

    def save(self, path):
        self.prs.save(path)
        return len(self.prs.slides._sldIdLst)


def build(out):
    C, E = load("C"), load("E")
    d = Deck()

    # 1 · title
    s = d.slide("Tree-crown detection vs the BBMP census",
                "Is a municipal point census usable as ground truth for "
                "detectree2 crown detection?  ·  2026-07-30")
    d.bullets(s, [
        ("What we compared", "h"),
        ("Model output: crown polygons from detectree2 on drone orthomosaics.", "b"),
        ("Reference: BBMP July-2026 tree census — 701,016 GPS points, "
         "city-wide. Points, not outlines.", "b"),
        ("Three orthos: JP Nagar (nominal 5 cm), the full drone_imagery block "
         "(3 cm), Lalbagh (3 cm).", "b"),
        ("Every number here is produced by scripts in code/scripts/ — no "
         "manual measurement, no eyeballing a basemap.", "sub"),
    ], top=2.5, size=17)

    # 2 · clipping logic
    s = d.slide("Step 1 · Finding the census trees inside the photo",
                "The census covers all of Bangalore. One drone photo covers a few "
                "streets. First job: keep only the trees that are actually in it.")
    d.bullets(s, [
        ("The three steps", "h"),
        ("1.  Take the four corners of the photo and convert them to "
         "latitude/longitude — the coordinate system the census uses.", "b"),
        ("2.  Ask the census file for just the trees inside that box.", "b"),
        ("3.  Convert those trees to a metre-based system (UTM 43N) before "
         "measuring any distance.", "b"),
        ("Why step 3 matters", "h"),
        ("The photo's own coordinates stretch distances by about 2.6% in "
         "Bangalore. Measuring there would make our 4 m GPS allowance quietly "
         "wrong — so we measure in real metres instead.", "warn"),
        ("Why the census file needed preparing first", "h"),
        ("It ships as one 228 MB text file with no index, so every search would "
         "read the whole thing. We converted it once into an indexed database "
         "(93 MB) — searches are now instant. 1,093 junk test rows removed, "
         "leaving 701,016 real trees.", "b"),
        ("Scripts:  index_census.py  (run once)  →  "
         "consensus_gt.load_census_in_bbox()  (per photo).   "
         "Cross-checked two different ways: both return 958 trees for JP Nagar.",
         "sub"),
    ], top=1.62, size=16)

    # 3 · clipping numbers
    s = d.slide("Step 1 · What the clip produced",
                "701,016 city-wide points → only those inside each image")
    d.table(s, ["ortho", "true extent", "GSD (nominal / true)",
                "census pts inside", "consensus trees"],
            [["jp_nagar…5cm_sample.tif", "493 × 490 m", "5.0 / 4.87 cm", "958", "594"],
             ["drone_imagery.tif", "1108 × 1118 m", "3.0 / 2.92 cm", "1,369", "841"],
             ["test_lalbagh_3cm.tif", "274 × 257 m", "3.0 / 2.92 cm", "0", "—"]],
            top=1.85, widths=[3.9, 2.0, 2.2, 2.0, 1.9], size=14)
    d.bullets(s, [
        ("Lalbagh returned zero points — the census stops at the garden "
         "boundary (0 inside, 369 within ~0.6 km, 42,711 within ~2.2 km). It is "
         "Horticulture Department land, not BBMP ward trees, so no census "
         "comparison is possible there.", "warn"),
        ("The clip is also what makes the coverage gap visible: the census "
         "footprint is a bounded ward blob, not the whole image.", "b"),
    ], top=4.15, size=15.5)

    # 4 · crown categories
    s = d.slide("Step 2 · How every model crown is categorised",
                "compare_gt.CROWN_CATS — these are the exact RGB values drawn "
                "on the tiles")
    d.swatch_rows(s, CROWN_CATS, {
        "validated":  "← exactly 1 consensus tree inside the crown + 4 m buffer",
        "merged":     "← 2 or more consensus trees in one crown → under-count",
        "green_nopt": "← 0 census trees but crown ExG > 15 → real, unsurveyed",
        "likely_fp":  "← 0 census trees and ExG ≤ 15 → not on vegetation",
    })
    d.bullets(s, [
        ("The rule, in order (compare_gt.classify)", "h"),
        ("n_clusters >= 2 -> merged;  == 1 -> validated;  == 0 -> green_nopt if "
         "ExG > 15 else likely_fp", "code"),
        ("Two independent signals do the work: proximity (is a surveyed tree "
         "here?) and greenness (is this vegetation at all?). Greenness is what "
         "separates \"the survey missed it\" from \"the detector was wrong\".", "b"),
    ], top=4.55, size=15.5)

    # 5 · point categories
    s = d.slide("Step 2 · How every census tree is categorised",
                "compare_gt.POINT_CATS — same colours as the right-hand panel")
    d.swatch_rows(s, POINT_CATS, {
        "confirmed":         "← a crown claims it AND it is on canopy → usable GT",
        "matched_offcanopy": "← a crown claims it but ExG ≤ 15 → suspect location",
        "missed_by_model":   "← on canopy, no crown → detector false negative",
        "bad_census":        "← off canopy, no crown → GPS error / felled / sapling",
    })
    d.bullets(s, [
        ("The rule (compare_gt.classify)", "h"),
        ("matched -> confirmed if on_canopy else matched_offcanopy;   "
         "unmatched -> missed_by_model if on_canopy else bad_census", "code"),
        ("matched = the point lies inside a crown buffered by 4 m.    "
         "on_canopy = ExG sampled at the point > 15.", "b"),
        ("ExG = 2G − R − B, read straight off the ortho. It detects "
         "vegetation, so ~14–21% of the mask is mown grass, not tree crown.", "sub"),
    ], top=4.55, size=15.5)

    # 6 · duplicates / overlaps
    s = d.slide("Step 2 · Duplicates and multi-point crowns",
                "The census surveys the same tree repeatedly — raw counts are "
                "meaningless without collapsing them")
    d.bullets(s, [
        ("Duplicate survey points → one consensus tree", "h"),
        ("DBSCAN(eps = 4 m, min_samples = 1) on the UTM coordinates. Each cluster "
         "becomes one consensus tree at the centroid, carrying n_pts as an "
         "agreement weight. JP Nagar: 958 raw points → 594 consensus trees.", "b"),
        ("Dot size on every tile grows with n_pts and counts ≥ 5 are labelled, "
         "so heavily-surveyed trees stay visible.", "sub"),
        ("Several consensus trees inside one crown → merged", "h"),
        ("Each crown is buffered by 4 m (GPS error) and spatially joined against "
         "the consensus points; n_clusters counts DISTINCT clusters inside. "
         "n_clusters ≥ 2 means the detector fused neighbouring trees into one "
         "polygon — an under-count, not a false positive.", "b"),
        ("Collapsing first is what makes this readable: without it, 18 raw points "
         "on one crown would look like 18 separate trees.", "sub"),
        ("Overlapping crowns", "h"),
        ("Crowns may overlap, so a point inside two buffered crowns counts for "
         "both. Verified against an independent brute-force STRtree "
         "recomputation: 672/672 crowns agree, matched sets identical, maximum "
         "matched distance 3.979 m ≤ the 4 m tolerance.", "b"),
        ("Known limit: min_samples=1 is single-linkage, so clusters chain. The "
         "largest spans 15.3 m — several trees merged, not one re-surveyed. "
         "Cluster count is eps-sensitive: 594 / 702 / 808 at 4 / 3 / 2 m.", "warn"),
    ], top=1.6, size=14.5)

    # 7 · headline comparison
    s = d.slide("Model crowns vs census trees — the numbers",
                "JP Nagar run C and drone_imagery run E "
                "(tile 40 / buffer 5 / conf 0.35)")
    pc, ec = C["counts"], E["counts"]
    d.table(s, ["", "JP Nagar (C)", "drone_imagery (E)"],
            [["model crowns", f"{pc['crowns']:,}", f"{ec['crowns']:,}"],
             ["census consensus trees", pc["consensus_trees"], ec["consensus_trees"]],
             ["confirmed by a crown", pc["point_cats"]["confirmed"],
              ec["point_cats"]["confirmed"]],
             ["matched but not green", pc["point_cats"]["matched_offcanopy"],
              ec["point_cats"]["matched_offcanopy"]],
             ["on canopy, no crown", pc["point_cats"]["missed_by_model"],
              ec["point_cats"]["missed_by_model"]],
             ["off canopy, no crown", pc["point_cats"]["bad_census"],
              ec["point_cats"]["bad_census"]],
             ["merged crowns", pc["crown_cats"]["merged"], ec["crown_cats"]["merged"]],
             ["on-canopy census found",
              f"{C['rates']['on_canopy_census_found']:.0%}",
              f"{E['rates']['on_canopy_census_found']:.0%}"],
             ["census points on canopy",
              f"{C['rates']['census_points_on_canopy']:.0%}",
              f"{E['rates']['census_points_on_canopy']:.0%}"],
             ["canopy enclosed (by area)",
              f"{C['canopy']['canopy_covered_pct']}%",
              f"{E['canopy']['canopy_covered_pct']}%"]],
            top=1.72, widths=[5.2, 3.4, 3.4], size=13.5)
    d.caption(s, "Only half the census points sit on canopy at all — the "
                 "ceiling on usable ground truth. Of those that do, the model now "
                 "finds ~56%.", top=6.75)

    # 8+ · visual comparison
    for tile, title, cap in [
        ("r07_c05", "Where the two agree",
         "Crowns (left): 6 validated · 9 merged · 4 green-no-point · 1 likely-FP. "
         "Census trees (right): 10 confirmed · 6 matched-but-not-green · 2 missed · "
         "3 off-canopy. Most polygons here are ORANGE — one crown covering several "
         "surveyed trees — so the detector under-counts even where it agrees."),
        ("r02_c09", "Census trees the model missed",
         "4 crowns vs 22 census trees. 15 cyan points sit on obvious canopy with "
         "no polygon drawn — detector false negatives. The tile is not one-sided "
         "though: 4 red points are off-canopy census error and 1 is matched but "
         "not green."),
        ("r04_c01", "Crowns the census never surveyed",
         "14 crowns, 0 census points in the tile — 12 of the 14 are YELLOW "
         "(green crown, no census tree). A survey coverage gap, and why raw "
         "precision understates the model. (1 crown still counts as validated: "
         "its 4 m buffer reaches a consensus point in the neighbouring tile.)"),
    ]:
        s = d.slide(title,
                    f"MODEL crowns (left)  vs  CENSUS trees (right) · identical "
                    f"imagery · JP Nagar run C · tile {tile} · 50 m across")
        d.picture(s, f"{REPORTS}/{RUNS['C']}/tiles/{tile}.png", top=1.66, max_h=5.0)
        d.caption(s, cap)

    s = d.slide("Full extent — all crowns, all census trees",
                "JP Nagar run C · left: 672 model crowns · right: 594 "
                "census consensus trees")
    d.picture(s, f"{REPORTS}/{RUNS['C']}/overview.png", top=1.66, max_h=5.0)
    d.caption(s, "The census footprint stops well short of the image's western "
                 "edge — 126 of 672 crowns fall outside it entirely.")

    s = d.slide("Lalbagh — detection only, no census",
                "163 crowns · the census contains zero points inside this ortho")
    d.picture(s, f"{REPORTS}/assets/lalbagh_overlay.png", top=1.66, max_h=5.0)
    d.caption(s, "8.1% of the canopy enclosed; 93.7% of crown area on vegetation "
                 "(a sanity check, not a precision score). It fires on isolated "
                 "specimens, stays silent on closed canopy — 97% of which sits in "
                 "blobs above predict.py's 200 m² cap.")

    # Lalbagh tile-size sweep
    s = d.slide("Lalbagh · how big should a tile be?",
                "Crown-size limit first raised 200 → 2000 m², then six runs at "
                "different tile sizes. Same photo, same model, confidence 0.35 "
                "throughout.")
    d.table(s, ["run", "tile", "buffer", "iou", "crowns", "biggest crown",
                "canopy enclosed", "on vegetation"],
            [["1", "40", "5", "0.8", "163", "186 m²", "8.1%", "93.7%"],
             ["2", "50", "5", "0.8", "164", "398 m²", "13.3%", "93.7%"],
             ["5", "60", "5", "0.8", "122", "753 m²", "13.6%", "92.9%"],
             ["3", "50", "10", "0.9", "130", "786 m²", "14.9%", "92.2%"],
             ["4", "80", "5", "0.9", "96", "334 m²", "12.7%", "89.1%"],
             ["6", "100", "10", "0.9", "46", "619 m²", "11.3%", "87.1%"]],
            top=1.72, widths=[.85, .85, 1.05, .85, 1.2, 2.0, 2.3, 2.1], size=12.5)
    d.bullets(s, [
        ("Runs 1, 2, 5 are the clean comparison — only tile size moves", "h"),
        ("Coverage 8.1% → 13.3% → 13.6%. Almost the whole gain happens between "
         "tile 40 and 50; after that it flattens.", "b"),
        ("Past tile 50 it gets worse, not better", "h"),
        ("Crowns collapse 164 → 46 and accuracy falls 93.7% → 87.1%. The median "
         "crown swells to 128 m²: the model is fusing many trees into one blob, "
         "not finding bigger trees. Bigger tiles mean each tree is fewer pixels "
         "once the tile is resized for the network.", "b"),
        ("Runs 3, 4 and 6 changed buffer and/or iou as well, so they are not "
         "like-for-like. Run 3 has the best coverage (14.9%) but moved two "
         "settings at once — worth repeating with only the buffer changed.", "warn"),
    ], top=4.5, size=14.5)

    # final
    s = d.slide("What this means for the census as ground truth")
    d.bullets(s, [
        ("Usable — but only as a clipped, filtered subset", "h"),
        ("Clip to the surveyed footprint; keep only on-canopy consensus points "
         "(~half of them); collapse duplicates before training.", "b"),
        ("It supervises detection and counting, not segmentation — the census "
         "gives points, not crown outlines.", "b"),
        ("Two detector faults found on the way, both mechanical", "h"),
        ("buffer > tile_size made the core tile smaller than a crown; "
         "conf_threshold 0.75 (applied three times in predict.py) discarded many "
         "ambiguous closed-canopy crowns. Fixing both took canopy coverage "
         "3.1% → 15.1% and recall 16% → 56%, with the census unchanged.", "b"),
        ("Next: raise the hard crown-area cap", "h"),
        ("predict.py drops any crown outside 4–200 m², yet 80% of JP "
         "Nagar's and 97% of Lalbagh's canopy sits in connected blobs larger than "
         "that. Test this before swapping detector models.", "warn"),
    ], top=1.55, size=15.5)

    return d.save(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=f"{REPORTS}/FINAL_census_vs_model.pptx")
    a = ap.parse_args()
    n = build(a.out)
    print(f"wrote {os.path.abspath(a.out)}  ({n} slides)")


if __name__ == "__main__":
    main()
