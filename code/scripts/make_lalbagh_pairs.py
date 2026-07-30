#!/usr/bin/env python3
"""
make_lalbagh_pairs.py — Pair up whole-ortho detectree2 overlays for the deck.

The tile-size story needs the FULL image per run, not a crop: the point is how
much of the garden is left un-outlined, which a 20 m snippet cannot show. Emits
one PNG per pair of runs, each labelled with the parameters it was run at.

Also trims the white matplotlib margin detectree2's overlay.png ships with.
"""
import argparse, glob, os, sys
from PIL import Image, ImageDraw
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compare_gt import _font   # noqa: E402
Image.MAX_IMAGE_PIXELS = None


def trim(im):
    a = np.asarray(im.convert("RGB"))
    m = a.min(axis=2) < 240
    r, c = np.where(m.any(1))[0], np.where(m.any(0))[0]
    return im.crop((c[0], r[0], c[-1] + 1, r[-1] + 1)) if len(r) else im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--pairs", nargs="+", required=True,
                    help="run:label:stat  run:label:stat  (two per output)")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--px", type=int, default=1500)
    a = ap.parse_args()
    P = glob.glob(f"data/storage/projects/{a.project}*/")[0]
    os.makedirs(a.outdir, exist_ok=True)

    specs = [s.split(":", 2) for s in a.pairs]
    for n in range(0, len(specs), 2):
        chunk = specs[n:n + 2]
        imgs = []
        for run, label, stat in chunk:
            f = glob.glob(f"{P}work/{run}/detectree/*/overlay.png")[0]
            im = trim(Image.open(f).convert("RGB"))
            im = im.resize((a.px, round(a.px * im.height / im.width)), Image.LANCZOS)
            imgs.append((im, label, stat))
        gap, pad, head, cap = 18, 20, 60, 56
        h = max(i.height for i, _, _ in imgs)
        W = pad * 2 + len(imgs) * a.px + (len(imgs) - 1) * gap
        H = head + h + cap + pad
        canvas = Image.new("RGB", (W, H), (22, 24, 21))
        d = ImageDraw.Draw(canvas)
        d.text((pad, 14), "Lalbagh — every tree the model outlined, whole garden",
               fill=(240, 240, 235), font=_font(34))
        for k, (im, label, stat) in enumerate(imgs):
            x = pad + k * (a.px + gap)
            canvas.paste(im, (x, head))
            d.text((x, head + h + 8), label, fill=(255, 190, 120), font=_font(30))
            d.text((x, head + h + 34), stat, fill=(160, 162, 155), font=_font(21))
        out = f"{a.outdir}/lalbagh_pair_{n//2 + 1}.png"
        canvas.save(out)
        print(f"wrote {out}  {canvas.size}")


if __name__ == "__main__":
    main()
