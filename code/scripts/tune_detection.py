#!/usr/bin/env python3
"""
tune_detection.py — sweep a hardcoded detection knob, re-run detection for each
value, score every run against the (weak) census ground truth, and tabulate
TP / FP / recall so you can see the tuning curve in one shot.

HOW THE PIPELINE IS TRIGGERED
  In-process: it imports ``run_detectree2_pipeline`` from predict.py and calls it
  directly — NO API server, NO Celery, and it skips the clustering/KMZ stages.
  Each run writes its own ``<out>/<param>=<val>/`` folder containing
  ``downsampled.tif`` + ``tree_crowns.geojson``. Because predictor=None, a fresh
  predictor is built per value, so conf_threshold / detections_per_image take
  effect (they bake into the predictor at build time).

SCORING
  For each run it shells out to consensus_gt.py on that run's own downsampled.tif
  + crowns, parses the printed metrics, and appends a CSV row. Optionally writes
  the overlay PNG per run (--overlay).

CAVEAT (read before trusting the numbers)
  The BBMP census is INCOMPLETE and ~50% usable, so treat these metrics as a
  RELATIVE signal across settings, not absolute accuracy. Watch whether TP (green
  crowns) rises faster than FP — do not tune to maximise census-match.

Requires (on the machine with the GPU + weights): torch, detectron2, detectree2,
plus the geo stack. This script is CPU-orchestration only; the heavy work is the
detection call.

Usage:
  python tune_detection.py \
    --ortho run_2/detectree/jp_.../<native or any>.tif \
    --model models/urban_trees_Cambridge_20230630.pth \
    --census bbmp_tree_census.gpkg --out tune_out \
    --utm EPSG:32643 --gps-err 5.5 --exg-thresh 18 --match-tol 5.5 \
    --sweep conf_threshold=0.85,0.6,0.4,0.25

Sweepable params (one --sweep; others stay at pipeline defaults):
  Whatever run_detectree2_pipeline() currently accepts. As of the target-GSD
  update that is: tile_size, buffer, iou_threshold, conf_threshold, target_gsd_m.
  The script checks up front and tells you if a parameter is not exposed, rather
  than failing mid-run.

  NOTE: detections_per_image, area_min and area_max are currently HARDCODED in
  predict.py (6, and the 4-2000 m2 filter). To sweep them, expose them as
  arguments first.
"""
import argparse, os, re, subprocess, sys, csv

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.abspath(os.path.join(HERE, ".."))          # .../code
sys.path.insert(0, CODE)

NUMERIC = {"conf_threshold", "area_max", "area_min", "downsample_scale",
           "tile_size", "buffer", "iou_threshold", "target_gsd_m"}
INT = {"detections_per_image", "tile_size", "buffer"}


def check_sweepable(param):
    """Fail fast with a useful message if predict.py does not expose `param`.

    predict.py's signature changes over time (e.g. detections_per_image and the
    area filter were parameterised, then reverted; target_gsd_m was added). A
    silent TypeError deep inside the first run is a bad way to find that out.
    """
    import inspect
    from predict import run_detectree2_pipeline
    accepted = set(inspect.signature(run_detectree2_pipeline).parameters)
    if param not in accepted:
        raise SystemExit(
            f"\nERROR: '{param}' is not a parameter of run_detectree2_pipeline().\n"
            f"Currently sweepable: {', '.join(sorted(accepted - {'ortho_path','predictor','output_dir','model_path'}))}\n"
            f"To sweep '{param}', expose it as an argument in code/predict.py first.\n"
        )

_PATS = {
    "crowns":     re.compile(r"crowns:\s*(\d+)"),
    "TP":         re.compile(r"validated by census \(TP\)\s*:\s*(\d+)"),
    "FP":         re.compile(r"likely FP\)\s*:\s*(\d+)"),
    "green_noc":  re.compile(r"census missed\)\s*:\s*(\d+)"),
    "merged":     re.compile(r"merged trees\)\s*:\s*(\d+)"),
    "usable":     re.compile(r"usable as GT.*:\s*(\d+)"),
    "FN":         re.compile(r"FN, missed\)\s*:\s*(\d+)"),
    "precision":  re.compile(r"precision.*:\s*([0-9.]+)"),
    "recall":     re.compile(r"recall.*:\s*([0-9.]+)"),
}


def parse_metrics(text):
    out = {}
    for k, p in _PATS.items():
        m = p.search(text)
        out[k] = (float(m.group(1)) if "." in m.group(1) else int(m.group(1))) if m else None
    return out


def cast(param, val):
    if param in INT:
        return int(float(val))
    if param in NUMERIC:
        return float(val)
    return val


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ortho", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--census", required=True)
    ap.add_argument("--out", default="tune_out")
    ap.add_argument("--utm", default="EPSG:32643")
    ap.add_argument("--gps-err", type=float, default=5.5)
    ap.add_argument("--exg-thresh", type=float, default=18.0)
    ap.add_argument("--match-tol", type=float, default=5.5)
    ap.add_argument("--sweep", required=True,
                    help="param=v1,v2,v3  e.g. conf_threshold=0.85,0.6,0.4")
    ap.add_argument("--overlay", action="store_true")
    a = ap.parse_args()

    param, vals = a.sweep.split("=", 1)
    param = param.strip()
    values = [cast(param, v.strip()) for v in vals.split(",")]
    os.makedirs(a.out, exist_ok=True)

    check_sweepable(param)                        # fail fast if predict.py lacks it
    from predict import run_detectree2_pipeline   # imported here so --help works w/o torch

    rows = []
    for v in values:
        tag = f"{param}={v}"
        run_dir = os.path.join(a.out, tag.replace("/", "_"))
        os.makedirs(run_dir, exist_ok=True)
        print(f"\n=== detection run: {tag} ===")
        kwargs = dict(ortho_path=a.ortho, model_path=a.model, output_dir=run_dir,
                      predictor=None)   # fresh predictor each run
        kwargs[param] = v
        try:
            geojson_path, _overlay, _used = run_detectree2_pipeline(**kwargs)
        except Exception as e:
            print(f"  detection FAILED for {tag}: {e}")
            rows.append({"param": param, "value": v, "error": str(e)[:120]})
            continue

        ortho_used = os.path.join(run_dir, "downsampled.tif")   # crowns live in this space
        cmd = [sys.executable, os.path.join(HERE, "consensus_gt.py"),
               "--ortho", ortho_used, "--crowns", geojson_path, "--census", a.census,
               "--out", run_dir, "--utm", a.utm, "--gps-err", str(a.gps_err),
               "--exg-thresh", str(a.exg_thresh), "--match-tol", str(a.match_tol)]
        if a.overlay:
            cmd.append("--overlay")
        res = subprocess.run(cmd, capture_output=True, text=True)
        sys.stdout.write(res.stdout)
        if res.returncode != 0:
            sys.stderr.write(res.stderr)
        m = parse_metrics(res.stdout)
        m.update({"param": param, "value": v})
        rows.append(m)

    cols = ["param", "value", "crowns", "TP", "FP", "green_noc", "merged",
            "FN", "usable", "precision", "recall", "error"]
    csv_path = os.path.join(a.out, "tune_summary.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    print(f"\nsaved {csv_path}")

    # tuning curve
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ok = [r for r in rows if r.get("TP") is not None]
        if ok:
            x = [r["value"] for r in ok]
            fig, ax1 = plt.subplots(figsize=(8, 5))
            ax1.plot(x, [r["crowns"] for r in ok], "-o", color="gray", label="crowns")
            ax1.plot(x, [r["TP"] for r in ok], "-o", color="#00A000", label="TP (validated)")
            ax1.plot(x, [r["FP"] for r in ok], "-o", color="#C00000", label="FP (likely false)")
            ax1.set_xlabel(param); ax1.set_ylabel("count"); ax1.legend(loc="upper left", fontsize=8)
            ax2 = ax1.twinx()
            ax2.plot(x, [r["recall"] for r in ok], "--s", color="#0050C0", label="recall")
            ax2.plot(x, [r["precision"] for r in ok], "--^", color="#8000C0", label="precision")
            ax2.set_ylabel("precision / recall"); ax2.legend(loc="upper right", fontsize=8)
            ax1.set_title(f"Detection tuning: {param} (census weak-GT)")
            fig.tight_layout(); fig.savefig(os.path.join(a.out, "tune_curve.png"), dpi=130)
            print(f"saved {os.path.join(a.out, 'tune_curve.png')}")
    except Exception as e:
        print(f"(plot skipped: {e})")


if __name__ == "__main__":
    main()
