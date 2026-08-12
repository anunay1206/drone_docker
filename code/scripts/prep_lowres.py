"""Prep the low-res/ Mission orthos for the detectree2 pipeline.

Two problems with these rasters as delivered:

  1. They are 4-band RGBA (band 4 = alpha), not 3-band RGB. detectree2's
     tiling writes whatever band count it reads, so tiles come out RGBA and
     the alpha channel rides into the network as a 4th plane / gets dropped
     silently depending on the reader. Strip it.

  2. `nodata` is None even though 18-37% of each image is outside the flight
     footprint and stored as RGB=(0,0,0). detectree2's `nan_threshold` check
     sums the bands and tests `== 0`, so black IS caught -- but the alpha
     channel adds a constant 255 to every valid pixel, which breaks the
     companion white-saturation test (`== 765`). Stripping alpha fixes that
     too. We also write nodata=0 so rasterio/GDAL report the mask correctly
     for anything downstream (overlays, area stats).

Nothing else is touched: CRS, transform, GSD and pixel values are preserved
bit-for-bit for bands 1-3. Output is tiled + LZW so it reads faster than the
source during tiling.

Usage:
    python code/scripts/prep_lowres.py low-res/ low-res-rgb/
"""

import os
import sys
import glob

import rasterio
from rasterio.enums import Resampling  # noqa: F401  (kept for overview step)


def prep_one(src_path: str, dst_path: str) -> dict:
    with rasterio.open(src_path) as src:
        if src.count < 3:
            raise ValueError(f"{src_path}: expected >=3 bands, got {src.count}")

        meta = src.meta.copy()
        meta.update(
            count=3,
            nodata=0,
            driver="GTiff",
            compress="LZW",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="IF_SAFER",
        )

        info = {
            "src_bands": src.count,
            "gsd_m": abs(src.transform.a),
            "crs": str(src.crs),
            "width": src.width,
            "height": src.height,
        }

        with rasterio.open(dst_path, "w", **meta) as dst:
            # Copy band-by-band in windows so we never hold the whole
            # 21496 x 14611 raster in RAM.
            for _, window in src.block_windows(1):
                dst.write(src.read([1, 2, 3], window=window), window=window)

            dst.set_band_description(1, "red")
            dst.set_band_description(2, "green")
            dst.set_band_description(3, "blue")

    return info


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    in_dir, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)

    # NOTE: the `._Mission*.tif` files in low-res/ are macOS AppleDouble
    # resource forks, not rasters. Exclude them or rasterio will throw.
    paths = sorted(
        p for p in glob.glob(os.path.join(in_dir, "*.tif"))
        if not os.path.basename(p).startswith("._")
    )
    if not paths:
        print(f"no .tif found in {in_dir}")
        return 1

    for p in paths:
        dst = os.path.join(out_dir, os.path.basename(p))
        if os.path.exists(dst):
            print(f"skip (exists): {dst}")
            continue
        info = prep_one(p, dst)
        print(
            f"{os.path.basename(p):<38} "
            f"{info['src_bands']}band -> 3band  "
            f"{info['gsd_m'] * 100:.2f} cm/px  {info['crs']}  "
            f"{info['width']}x{info['height']}"
        )

    print(f"\ndone -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
