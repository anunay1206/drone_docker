#!/usr/bin/env python3
"""
make_gt_report.py — Turn a compare_gt.py output folder into something presentable:
a narrative report.html, a presentation.pptx, and a coverage chart.

compare_gt.py answers "what happened"; this answers "what do we tell people". It
reads only stats.json + summary.csv from the compare folder, so it works for any
run without re-deriving anything, and two runs can be reported identically.

It also auto-picks representative tiles from summary.csv — the tile that best
shows each situation (agreement, model misses, coverage gap, merged crowns) —
rather than making you hunt through 121 pngs for something worth showing.

Usage:
  python make_gt_report.py --compare ./gt_compare \
      --out docs/progress_reports/census_vs_model_2026-07-30 \
      --title "BBMP census vs detectree2" \
      --note "detector run: tile_size=5 buffer=10 conf=0.75"
"""
import argparse
import csv
import html
import json
import os
import shutil

from PIL import Image, ImageDraw, ImageFont

FG = (232, 232, 226)
MUTED = (154, 156, 149)
BG = (22, 24, 21)
ACCENT = (110, 190, 120)
WARN = (240, 170, 60)


def _font(size, bold=False):
    base = "/usr/share/fonts/truetype/dejavu/"
    p = base + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    return ImageFont.truetype(p, size) if os.path.exists(p) else ImageFont.load_default()


def coverage_chart(stats, path, w=1400, h=520):
    """Bar chart of census point density across the ortho's width.

    This is the figure that stops someone concluding 'the model hallucinates
    trees' when the truth is that nobody surveyed the western strip.
    """
    bins = stats["coverage"]["width_bins"]
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    f_t, f_l, f_v = _font(26, True), _font(16), _font(17, True)
    d.text((30, 22), "Census points across the ortho, west → east",
           fill=FG, font=f_t)
    d.text((30, 58), "the survey simply does not cover the western strip",
           fill=MUTED, font=f_l)
    x0, y0, bw = 40, h - 70, (w - 90) / len(bins)
    top = y0 - 300
    mx = max(1, max(b["points"] for b in bins))
    for i, b in enumerate(bins):
        # floor the height so an empty strip still shows as a visible orange stub —
        # "nobody surveyed here" is the whole point of the chart
        bh = max(4, (y0 - top) * b["points"] / mx)
        x = x0 + i * bw
        colour = WARN if b["points"] <= mx * 0.05 else ACCENT
        d.rectangle([x + 6, y0 - bh, x + bw - 8, y0], fill=colour)
        d.text((x + 8, y0 - bh - 24), str(b["points"]), fill=FG, font=f_v)
        d.text((x + 8, y0 + 10), f"{b['from_pct']}-{b['to_pct']}%",
               fill=MUTED, font=_font(13))
    d.line([x0, y0, w - 40, y0], fill=(70, 74, 68), width=2)
    d.text((30, h - 34), "% of ortho width (0 = west edge)", fill=MUTED, font=f_l)
    img.save(path)


def pick_tiles(rows):
    """Choose the tiles worth showing, one per story we need to tell."""
    def top(key, extra=None):
        c = [r for r in rows if extra is None or extra(r)]
        return max(c, key=key)["tile"] if c else None
    picks = []
    t = top(lambda r: int(r["pt_confirmed"]))
    if t:
        picks.append((t, "Where both agree",
                      "Crowns the census confirms — the case that would make good "
                      "training data."))
    t = top(lambda r: int(r["pt_missed_by_model"]))
    if t:
        picks.append((t, "Census trees the model missed",
                      "Cyan dots sit on obvious canopy with no crown drawn. Either "
                      "the detector under-segments, or its tiling is dropping them."))
    t = top(lambda r: int(r["crown_green_nopt"]),
            lambda r: int(r["census"]) == 0)
    if t:
        picks.append((t, "Coverage gap, not a false positive",
                      "The model finds green crowns here and the census has nothing "
                      "at all — because this area was never surveyed."))
    t = top(lambda r: int(r["crown_merged"]))
    if t and int(dict(rows[0]).get("crown_merged", 0)) is not None:
        picks.append((t, "Merged crowns",
                      "One polygon spanning several census trees — the detector "
                      "under-counts by fusing neighbours."))
    seen, out = set(), []
    for tile, title, cap in picks:
        if tile not in seen:
            seen.add(tile)
            out.append((tile, title, cap))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", required=True, help="a compare_gt.py --out folder")
    ap.add_argument("--out", required=True, help="presentation folder to create")
    ap.add_argument("--title", default="Census vs model — tree crown ground truth")
    ap.add_argument("--note", default="", help="one line describing the detector run")
    ap.add_argument("--move", action="store_true",
                    help="move the compare folder in instead of copying (saves disk)")
    a = ap.parse_args()

    stats = json.load(open(f"{a.compare}/stats.json"))
    rows = [r for r in csv.DictReader(open(f"{a.compare}/summary.csv"))
            if r["tile"] != "TOTAL"]
    os.makedirs(a.out, exist_ok=True)

    # bring the evidence along so the folder is self-contained and shareable
    for name in ("overview.png", "summary.csv", "stats.json", "index.html"):
        src = f"{a.compare}/{name}"
        if os.path.exists(src):
            shutil.copy2(src, f"{a.out}/{name}")
    dst_tiles = f"{a.out}/tiles"
    if not os.path.exists(dst_tiles):
        if a.move:
            shutil.move(f"{a.compare}/tiles", dst_tiles)
        else:
            shutil.copytree(f"{a.compare}/tiles", dst_tiles)

    coverage_chart(stats, f"{a.out}/coverage.png")
    picks = pick_tiles(rows)
    write_report(a, stats, picks, rows)
    write_pptx(a, stats, picks)
    print(f"report : {os.path.abspath(a.out)}/report.html")
    print(f"deck   : {os.path.abspath(a.out)}/presentation.pptx")


def _findings(stats):
    cov, r, c = stats["coverage"], stats["rates"], stats["counts"]
    cn = stats["canopy"]
    ac = stats["area_cap"]
    return [
        ("A hard-coded crown-area cap forecloses most of the canopy",
         f"<code>predict.py</code> drops every crown outside "
         f"<code>{ac['cap_min_src']} &lt; area &lt; {ac['cap_max_src']}</code>, measured "
         f"in the <i>raster</i> CRS (so really ~{ac['cap_max_true_m2']:.0f} true m&sup2;, "
         f"about 15.5 m across). It is binding: the largest crown here is "
         f"{ac['crown_area_max_src']}, {ac['crowns_over_cap']} crowns exceed the cap, and "
         f"{ac['crowns_at_floor']} sit on the floor. Meanwhile "
         f"<b>{ac['canopy_in_blobs_over_cap_pct']}%</b> of this ortho's canopy lies in "
         f"connected blobs bigger than the cap — canopy no single permitted crown can "
         f"enclose. A large blob could in principle be tiled by several sub-cap crowns, "
         f"so this is not strict impossibility, but it is a structural ceiling that no "
         f"UI parameter can lift. <b>Test this before changing models.</b>"),
        ("The detector encloses only a fraction of the canopy",
         f"By area, not by count: {cn['canopy_pct_of_extent']}% of the ortho is "
         f"vegetation ({cn['canopy_m2']:,} true m&sup2; by ExG), but the crown polygons "
         f"cover only <b>{cn['canopy_covered_pct']}%</b> of it — "
         f"{cn['canopy_missed_m2']:,} m&sup2; left un-enclosed. This caps every recall "
         f"number below: the census cannot be fairly evaluated as ground truth against a "
         f"detector operating at this level. "
         f"<i>Do not read {cn['crown_on_canopy_pct']}% crown-area-on-vegetation as "
         f"precision</i> — crowns legitimately enclose shadow and gaps, and that figure "
         f"swings 87%&rarr;45% as the ExG cutoff moves 10&rarr;25. ExG also scores mown "
         f"grass as vegetation (14&ndash;21% of the mask by texture), which makes the "
         f"coverage figure mildly conservative rather than inflated."),
        ("The census does not cover the whole ortho",
         f"The western <b>{cov['unsurveyed_west_pct']:.0f}%</b> of the extent has "
         f"almost no census points, and <b>{cov['crowns_outside_census_bbox']} of "
         f"{c['crowns']}</b> detected crowns fall outside the census bounding box "
         f"entirely. Those crowns are counted as &ldquo;no census point&rdquo; for a "
         f"reason that has nothing to do with the detector, so the "
         f"{r['crowns_census_supported']:.0%} support figure is pessimistic. Any "
         f"usability verdict must be computed clipped to the surveyed area."),
        ("Half the census points are not on canopy",
         f"Only <b>{r['census_points_on_canopy']:.0%}</b> of the "
         f"{c['consensus_trees']} consensus trees sit on vegetation by ExG. The rest "
         f"land on roofs, roads or bare ground — GPS error, a felled tree, or a "
         f"sapling too small to see. That is a hard ceiling on how much of this "
         f"census can ever be training ground truth, independent of the model."),
        ("Survey points are duplicated — but the collapse over-merges",
         f"{c['census_raw_points']} raw points collapse to {c['consensus_trees']} "
         f"consensus trees at eps = {stats['params']['gps_err']:g} m; training on raw "
         f"points without that collapse would weight some trees far more than others. "
         f"<b>Caveat:</b> DBSCAN with <code>min_samples=1</code> is single-linkage and "
         f"chains transitively, so the largest cluster "
         f"({c['max_pts_collapsed']} points) spans well beyond GPS error and merges "
         f"distinct neighbouring trees rather than repeat visits to one. The consensus "
         f"count is eps-sensitive (594 / 702 / 808 at 4 / 3 / 2 m on JP Nagar), which "
         f"moves the denominator of every census rate above."),
    ]


def write_report(a, stats, picks, rows):
    r, c, cov, inp = (stats["rates"], stats["counts"], stats["coverage"],
                      stats["inputs"])
    p = stats["params"]

    def stat(v, label, sub=""):
        return (f"<div class='stat'><b>{v}</b>{html.escape(label)}"
                + (f"<div class='muted'>{html.escape(sub)}</div>" if sub else "")
                + "</div>")

    cats = "".join(
        f"<tr><td>{html.escape(k.replace('_',' '))}</td><td class='n'>{v}</td>"
        f"<td class='n'>{v/max(1,c['crowns']):.0%}</td></tr>"
        for k, v in c["crown_cats"].items())
    pts = "".join(
        f"<tr><td>{html.escape(k.replace('_',' '))}</td><td class='n'>{v}</td>"
        f"<td class='n'>{v/max(1,c['consensus_trees']):.0%}</td></tr>"
        for k, v in c["point_cats"].items())
    finds = "".join(
        f"<div class='find'><h3>{i+1}. {html.escape(t)}</h3><p>{body}</p></div>"
        for i, (t, body) in enumerate(_findings(stats)))
    tiles = "".join(
        f"<figure><a href='tiles/{t}.png' target='_blank'>"
        f"<img src='tiles/{t}.png'></a>"
        f"<figcaption><b>{html.escape(title)}</b> <span class='muted'>({t})</span>"
        f"<br>{html.escape(cap)}</figcaption></figure>"
        for t, title, cap in picks)

    doc = f"""<!doctype html><meta charset="utf-8">
<title>{html.escape(a.title)}</title>
<style>
 body{{margin:0 auto;max-width:1180px;padding:34px 26px 70px;background:#161815;
  color:#e8e8e2;font:15px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}}
 h1{{font-size:27px;margin:0 0 6px}} h2{{font-size:19px;margin:38px 0 12px;
  border-bottom:1px solid #2c2f28;padding-bottom:6px}} h3{{font-size:16px;margin:0 0 6px}}
 .muted{{color:#9a9c95}} code{{background:#23261f;padding:1px 5px;border-radius:3px;font-size:13px}}
 .hero{{display:flex;gap:14px;flex-wrap:wrap;margin:20px 0}}
 .stat{{background:#1e211c;border:1px solid #2f332b;border-radius:7px;
  padding:13px 18px;min-width:158px}}
 .stat b{{display:block;font-size:27px;font-weight:700;color:#fff}}
 table{{border-collapse:collapse;font-size:14px;min-width:290px}}
 th,td{{padding:5px 13px;border-bottom:1px solid #2c2f28;text-align:left}}
 td.n{{text-align:right;font-variant-numeric:tabular-nums}}
 .row{{display:flex;gap:38px;flex-wrap:wrap}}
 .find{{background:#1b1e19;border-left:3px solid #6ebe78;border-radius:0 6px 6px 0;
  padding:13px 18px;margin:0 0 13px}}
 .find:nth-child(2){{border-left-color:#f0aa3c}}
 img{{max-width:100%;border-radius:6px;display:block}}
 figure{{margin:0 0 26px}} figcaption{{font-size:13.5px;color:#c3c5be;margin-top:7px}}
 .verdict{{background:#1e211c;border:1px solid #3d5f42;border-radius:7px;padding:16px 20px}}
</style>

<h1>{html.escape(a.title)}</h1>
<div class="muted">
Can the BBMP July-2026 tree census be used as training ground truth for
detectree2 crown detection?{(" &middot; " + html.escape(a.note)) if a.note else ""}
</div>

<h2>What we did</h2>
<p>The census gives <em>points</em>; the detector gives <em>polygons</em>. To compare
them without eyeballing a basemap, two objective signals stand in for the eye:</p>
<ol>
 <li><b>Greenness (ExG = 2G − R − B)</b> sampled from the ortho itself at every
  census point and inside every crown. A point on a roof, road or shadow scores
  low; canopy scores high. This decides whether a census point is physically on a
  tree at all.</li>
 <li><b>Proximity</b> between census points and crowns within a GPS-error
  tolerance ({p['gps_err']:g} m), which decides which detections a survey confirms.</li>
</ol>
<p>Repeat survey visits to one tree are collapsed with DBSCAN (eps =
{p['gps_err']:g} m) into a single <em>consensus tree</em> weighted by how many raw
points agreed, so duplicates don't inflate the counts. The ortho is then cut into
{stats['tiles']['rows']}&times;{stats['tiles']['cols']} tiles of {p['tile_m']:g} m and each
tile is rendered as two panels over identical imagery — model on the left, census
on the right — so a disagreement is something you can see.</p>
<p class="muted">
 ortho <code>{html.escape(os.path.basename(inp['ortho']))}</code>
 ({inp['ortho_px'][0]}&times;{inp['ortho_px'][1]} px @ {inp['gsd_units']:.3f} {inp['ortho_crs']} units,
 {inp['extent_units'][0]:.0f}&times;{inp['extent_units'][1]:.0f} m extent) ·
 ExG canopy cutoff {p['exg_thresh']:g} · match tolerance {p['match_tol']:g} m ·
 distances in {html.escape(p['utm'])}
</p>

<h2>Headline numbers</h2>
<div class="hero">
 {stat(f"{r['census_points_on_canopy']:.0%}", "census points on canopy", "ceiling on usable GT")}
 {stat(f"{r['crowns_census_supported']:.0%}", "crowns census-supported")}
 {stat(f"{r['on_canopy_census_found']:.0%}", "on-canopy census found")}
 {stat(c['crowns'], "model crowns")}
 {stat(c['consensus_trees'], "consensus trees", f"from {c['census_raw_points']} raw points")}
</div>
<div class="row">
 <div><h3>Crowns</h3><table><tr><th>category</th><th>n</th><th>share</th></tr>
  {cats}</table></div>
 <div><h3>Census consensus trees</h3><table><tr><th>category</th><th>n</th><th>share</th></tr>
  {pts}</table></div>
</div>

<h2>What we found</h2>
{finds}

<h2>Canopy coverage by area</h2>
<div class="row">
 <div><table>
  <tr><th>measure</th><th>value</th></tr>
  <tr><td>canopy in extent (ExG)</td><td class="n">{stats['canopy']['canopy_m2']:,} m&sup2;</td></tr>
  <tr><td>&nbsp;&nbsp;as share of ortho</td><td class="n">{stats['canopy']['canopy_pct_of_extent']}%</td></tr>
  <tr><td>crown polygon area</td><td class="n">{stats['canopy']['crown_area_m2']:,} m&sup2;</td></tr>
  <tr><td><b>canopy enclosed by crowns</b></td><td class="n"><b>{stats['canopy']['canopy_covered_pct']}%</b></td></tr>
  <tr><td>crown area on vegetation <span class="muted">(sanity check, not precision)</span></td><td class="n">{stats['canopy']['crown_on_canopy_pct']}%</td></tr>
  <tr><td>canopy in blobs &gt; {stats['area_cap']['cap_max_src']} m² cap</td><td class="n"><b>{stats['area_cap']['canopy_in_blobs_over_cap_pct']}%</b></td></tr>
  <tr><td>largest crown / cap <span class="muted">(raster CRS)</span></td><td class="n">{stats['area_cap']['crown_area_max_src']} / {stats['area_cap']['cap_max_src']}</td></tr>
  <tr><td>canopy missed</td><td class="n">{stats['canopy']['canopy_missed_m2']:,} m&sup2;</td></tr>
 </table></div>
</div>
<p class="muted">Count-independent: a detector can report many crowns and still
enclose almost none of the vegetation. High purity with low coverage means the
detections are correct but incomplete.</p>

<h2>Census coverage</h2>
<img src="coverage.png">
<p class="muted">Point count per 10% strip of the ortho's width. The orange bars are
strips the survey never reached.</p>

<h2>Evidence</h2>
{tiles}

<h2>Full extent</h2>
<a href="overview.png" target="_blank"><img src="overview.png"></a>
<p class="muted">Left: model crowns. Right: census consensus trees, dot size growing
with how many raw survey points agreed.</p>

<h2>Verdict</h2>
<div class="verdict">
<p><b>Not usable as-is; usable for a clipped, filtered subset.</b> Three conditions
have to hold before this census can train a detector:</p>
<ol>
 <li>Clip to the surveyed footprint — otherwise
  {cov['crowns_outside_census_bbox']} crowns are scored against a survey that was
  never done there.</li>
 <li>Keep only on-canopy consensus points ({c['point_cats']['confirmed'] + c['point_cats']['missed_by_model']}
  of {c['consensus_trees']}); the rest are not on a tree.</li>
 <li>Collapse duplicates first — but tune it: single-linkage DBSCAN at
  {stats['params']['gps_err']:g} m over-merges neighbouring trees (largest cluster
  {c['max_pts_collapsed']} points, spanning past GPS error).</li>
</ol>
<p>Separately, the low
<b>{r['on_canopy_census_found']:.0%}</b> recall is at least partly a detector
configuration problem rather than a census problem, and should be re-measured
after the detector is re-tuned. Points are <em>points</em>, not crown outlines, so
even the clean subset supports detection/counting supervision — not segmentation
masks — unless crowns are drawn around them.</p>
</div>

<h2>Files</h2>
<ul>
 <li><code>index.html</code> — all {stats['tiles']['written']} tiles, browsable</li>
 <li><code>tiles/</code> — the side-by-side pngs</li>
 <li><code>overview.png</code>, <code>coverage.png</code> — figures</li>
 <li><code>summary.csv</code> — per-tile counts · <code>stats.json</code> — all numbers</li>
</ul>
"""
    with open(f"{a.out}/report.html", "w") as f:
        f.write(doc)


def write_pptx(a, stats, picks):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Emu, Inches, Pt

    r, c, cov = stats["rates"], stats["counts"], stats["coverage"]
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    DARK, LIGHT, GREEN = RGBColor(0x16, 0x18, 0x15), RGBColor(0xE8, 0xE8, 0xE2), \
        RGBColor(0x6E, 0xBE, 0x78)

    def slide(title, sub=""):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = DARK
        tb = s.shapes.add_textbox(Inches(.55), Inches(.35), Inches(12.2), Inches(.9))
        p = tb.text_frame.paragraphs[0]
        p.text = title
        p.font.size, p.font.bold, p.font.color.rgb = Pt(30), True, LIGHT
        if sub:
            p2 = tb.text_frame.add_paragraph()
            p2.text = sub
            p2.font.size, p2.font.color.rgb = Pt(14), RGBColor(0x9A, 0x9C, 0x95)
        return s

    def bullets(s, items, top=1.6, size=17):
        tb = s.shapes.add_textbox(Inches(.6), Inches(top), Inches(12.1),
                                  Inches(5.4))
        tf = tb.text_frame
        tf.word_wrap = True
        for i, (txt, bold) in enumerate(items):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = txt
            p.font.size = Pt(size)
            p.font.bold = bold
            p.font.color.rgb = GREEN if bold else LIGHT
            p.space_after = Pt(10)

    def picture(s, path, top=1.55, max_h=5.5):
        if not os.path.exists(path):
            return
        iw, ih = Image.open(path).size
        max_w = 12.1
        scale = min(max_w / (iw / 96), max_h / (ih / 96))
        w, h = (iw / 96) * scale, (ih / 96) * scale
        s.shapes.add_picture(path, Inches(.6 + (max_w - w) / 2), Inches(top),
                             Inches(w), Inches(h))

    s = slide(a.title, a.note or "BBMP July-2026 census as detectree2 training GT")
    bullets(s, [
        ("The question: can a municipal point census supervise crown detection?", True),
        (f"{c['census_raw_points']} census points and {c['crowns']} detected crowns over a "
         f"{stats['inputs']['extent_units'][0]:.0f} × {stats['inputs']['extent_units'][1]:.0f} m ortho.", False),
        ("Answer: only a clipped, de-duplicated, on-canopy subset — and the "
         "detector needs re-tuning before recall means anything.", False),
    ], top=2.4, size=19)

    s = slide("Method", "two objective signals replace eyeballing a basemap")
    bullets(s, [
        ("1 · Greenness (ExG = 2G − R − B), sampled from the ortho", True),
        ("At each census point and inside each crown. Roof/road/shadow scores low, "
         "canopy scores high — so we know if a point is on a tree at all.", False),
        ("2 · Proximity within GPS error", True),
        (f"Crowns buffered by {stats['params']['match_tol']:g} m; a consensus point "
         f"inside that buffer confirms the detection.", False),
        ("Duplicates collapsed with DBSCAN", True),
        (f"eps = {stats['params']['gps_err']:g} m, so repeat visits to one tree become one "
         f"consensus tree weighted by agreement (largest cluster here: "
         f"{c['max_pts_collapsed']} points — over-merges neighbours, see caveat).", False),
        ("Rendered as side-by-side tiles", True),
        (f"{stats['tiles']['rows']}×{stats['tiles']['cols']} tiles of "
         f"{stats['params']['tile_m']:g} m: model left, census right, identical imagery.", False),
    ])

    s = slide("Results")
    hero = [(f"{stats['canopy']['canopy_covered_pct']:.0f}%", "canopy enclosed (area)"),
            (f"{stats['area_cap']['canopy_in_blobs_over_cap_pct']:.0f}%", "canopy > area cap"),
            (f"{r['census_points_on_canopy']:.0%}", "census points on canopy"),
            (f"{r['crowns_census_supported']:.0%}", "crowns census-supported"),
            (f"{r['on_canopy_census_found']:.0%}", "on-canopy census found"),
            (str(c["crowns"]), "model crowns"),
            (str(c["consensus_trees"]), "consensus trees")]
    for i, (v, lab) in enumerate(hero):
        bx = prs.slides[-1].shapes.add_textbox(
            Inches(.6 + i * 2.45), Inches(1.9), Inches(2.3), Inches(1.5))
        tf = bx.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = v
        p.font.size, p.font.bold, p.font.color.rgb = Pt(40), True, GREEN
        p2 = tf.add_paragraph()
        p2.text = lab
        p2.font.size, p2.font.color.rgb = Pt(13), LIGHT
    rowtxt = [(f"crowns — {k.replace('_',' ')}: {v}", False)
              for k, v in c["crown_cats"].items()]
    rowtxt += [(f"census — {k.replace('_',' ')}: {v}", False)
               for k, v in c["point_cats"].items()]
    bullets(s, rowtxt, top=3.7, size=15)

    for i, (title, body) in enumerate(_findings(stats)):
        s = slide(f"Finding {i+1}", title)
        plain = (body.replace("<b>", "").replace("</b>", "")
                 .replace("&ldquo;", "“").replace("&rdquo;", "”")
                 .replace("&mdash;", "—"))
        bullets(s, [(plain, False)], top=2.0, size=19)
        if i == 0:
            picture(s, f"{a.out}/coverage.png", top=3.4, max_h=3.6)

    for tile, title, cap in picks:
        s = slide(title, f"{cap}   ({tile})")
        picture(s, f"{a.out}/tiles/{tile}.png", top=1.75, max_h=5.3)

    s = slide("Full extent", "left: model crowns · right: census consensus trees")
    picture(s, f"{a.out}/overview.png", top=1.7, max_h=5.4)

    s = slide("Verdict & next steps")
    bullets(s, [
        ("Not usable as-is. Usable as a clipped, filtered subset:", True),
        (f"1 · Clip to the surveyed footprint — {cov['crowns_outside_census_bbox']} of "
         f"{c['crowns']} crowns sit outside the census bbox entirely.", False),
        (f"2 · Keep only on-canopy consensus points — "
         f"{c['point_cats']['confirmed'] + c['point_cats']['missed_by_model']} of "
         f"{c['consensus_trees']}.", False),
        (f"3 · Collapse duplicates — and tune eps; single-linkage over-merges "
         f"(largest cluster {c['max_pts_collapsed']} points).", False),
        ("Raise the predict.py area cap FIRST, then re-measure.", True),
        (f"{stats['area_cap']['canopy_in_blobs_over_cap_pct']:.0f}% of canopy is in blobs "
         f"above the {stats['area_cap']['cap_max_src']} m² cap — a structural ceiling, "
         f"not a tuning issue.", False),
        ("Points supervise detection/counting, not segmentation.", True),
        ("Crown masks would still have to be drawn around the kept points.", False),
    ])

    prs.save(f"{a.out}/presentation.pptx")


if __name__ == "__main__":
    main()
