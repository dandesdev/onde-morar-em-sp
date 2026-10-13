"""Assemble the folder that GitHub Pages publishes.

Usage:
  python -P pipeline/assemble.py _site --data data              # code + freshly built data
  python -P pipeline/assemble.py _site --data-from-live URL     # code + the data currently online
"""
import argparse
import shutil
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
WEB_FILES = ["index.html", "fontes.html", "app.js", "router.js", "style.css", ".nojekyll"]
DATA_FILES = ["meta.json", "stations.json", "graph.json", "graph.bin.gz", "cells.json.gz", "bairros.json.gz",
              "rail.json.gz", "distritos.json.gz", "greens.json.gz", "bigroads.json.gz", "pois.json.gz"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--data")
    src.add_argument("--data-from-live")
    a = ap.parse_args()
    out = Path(a.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    for name in WEB_FILES:
        shutil.copy2(ROOT / name, out / name)
    for name in DATA_FILES:
        if a.data:
            shutil.copy2(Path(a.data) / name, out / "data" / name)
        else:
            r = requests.get(f"{a.data_from_live.rstrip('/')}/data/{name}", timeout=120)
            r.raise_for_status()
            (out / "data" / name).write_bytes(r.content)
    print("assembled", out, sorted(p.name for p in (out / "data").iterdir()))


if __name__ == "__main__":
    main()
