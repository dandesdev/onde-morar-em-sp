"""Build a 10 m NDVI raster of São Paulo from the newest clear Sentinel-2 L2A scene (Planetary Computer).

Usage: python -P pipeline/fetch_ndvi.py <work>/processed/ndvi.tif [lon_min,lat_min,lon_max,lat_max]

Picks the most recent date in the last ~6 months where both MGRS tiles covering the city (23KLP, 23KLQ)
have < 3% cloud (else the clearest such date). Writes the raster and <out>.json with the scene date.
"""
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import planetary_computer as pc
import rasterio
from pystac_client import Client
from rasterio.merge import merge
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

BBOX = (-46.84, -24.02, -46.35, -23.34)
TILES = {"23KLP", "23KLQ"}


def read_band(href, bounds_ll):
    src = rasterio.open(href)
    b = transform_bounds("EPSG:4326", src.crs, *bounds_ll)
    win = from_bounds(*b, transform=src.transform).round_offsets().round_lengths()
    win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
    return src.read(1, window=win).astype("float32"), src.window_transform(win), src.crs


def read_like(href, shape, transform):
    """Read a band onto an existing 10 m grid (used for the 20 m SCL mask)."""
    src = rasterio.open(href)
    win = from_bounds(*rasterio.transform.array_bounds(*shape, transform), transform=src.transform)
    return src.read(1, window=win, out_shape=shape, resampling=rasterio.enums.Resampling.nearest).astype("float32")


def pick_scene(cat):
    since = (date.today() - timedelta(days=180)).isoformat()
    items = cat.search(collections=["sentinel-2-l2a"], bbox=list(BBOX), datetime=f"{since}/{date.today().isoformat()}",
                       query={"eo:cloud_cover": {"lt": 10}}).items()
    by_day = defaultdict(dict)
    for it in items:
        tile = it.properties.get("s2:mgrs_tile")
        if tile in TILES:
            prev = by_day[it.datetime.date()].get(tile)
            if prev is None or it.properties["eo:cloud_cover"] < prev.properties["eo:cloud_cover"]:
                by_day[it.datetime.date()][tile] = it
    full = {d: t for d, t in by_day.items() if set(t) == TILES}
    if not full:
        raise SystemExit("no date with both tiles in the last 180 days")
    worst = {d: max(i.properties["eo:cloud_cover"] for i in t.values()) for d, t in full.items()}
    clear = [d for d, c in worst.items() if c < 3]
    day = max(clear) if clear else min(worst, key=worst.get)
    print(f"scene {day} (max cloud {worst[day]:.2f}%)", flush=True)
    return day, list(full[day].values())


def main(out):
    cat = Client.open("https://planetarycomputer.microsoft.com/api/stac/v1", modifier=pc.sign_inplace)
    day, items = pick_scene(cat)
    parts = []
    for item in items:
        red, tr, crs = read_band(item.assets["B04"].href, BBOX)
        nir, _, _ = read_band(item.assets["B08"].href, BBOX)
        scl = read_like(item.assets["SCL"].href, red.shape, tr)
        ndvi = (nir - red) / np.maximum(nir + red, 1)
        ndvi[(red == 0) | np.isin(scl, [0, 3, 8, 9, 10])] = np.nan  # nodata, shadow, cloud
        mem = rasterio.io.MemoryFile()
        ds = mem.open(driver="GTiff", height=ndvi.shape[0], width=ndvi.shape[1], count=1,
                      dtype="float32", crs=crs, transform=tr, nodata=np.nan)
        ds.write(ndvi, 1)
        parts.append(ds)
    mosaic, tr = merge(parts, nodata=np.nan)
    if np.isnan(mosaic).mean() > 0.2:
        raise SystemExit("too much of the city masked (clouds/nodata); keeping the previous raster")
    tmp = out + ".tmp"
    with rasterio.open(tmp, "w", driver="GTiff", height=mosaic.shape[1], width=mosaic.shape[2], count=1,
                       dtype="float32", crs=parts[0].crs, transform=tr, nodata=np.nan, compress="deflate") as dst:
        dst.write(mosaic[0], 1)
    os.replace(tmp, out)
    with open(out + ".json", "w", encoding="utf-8") as f:
        json.dump({"date": day.isoformat(), "items": [i.id for i in items]}, f)
    print("saved", out, mosaic.shape, float(np.nanmean(mosaic)), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 2:
        BBOX = tuple(map(float, sys.argv[2].split(",")))
    main(sys.argv[1])
