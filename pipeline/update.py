"""Weekly data update for Onde Morar Em SP.

Usage: python -P pipeline/update.py [--only build] [--skip ssp,osm,...]

Steps: ssp (crime microdata), geosampa (city layers), ndvi (satellite), osm (OpenStreetMap), build, check.
A source that fails keeps its previous files (restored from the CI cache) and the run goes on; the build and
the checks must pass, otherwise the script exits non-zero and nothing is published.

Folders: OMSP_WORK (default ./work) holds raw/ and processed/ inputs and sources.json;
OMSP_OUT (default ./data) receives the website data.
"""
import argparse
import json
import os
import subprocess
import sys
import traceback
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PIPE = ROOT / "pipeline"
WORK = Path(os.environ.get("OMSP_WORK", ROOT / "work")).resolve()
OUT = Path(os.environ.get("OMSP_OUT", ROOT / "data")).resolve()
RAW, PROC, DL = WORK / "raw", WORK / "processed", WORK / "downloads"
BBOX = "-46.84,-24.02,-46.35,-23.34"
SSP_URL = "https://www.ssp.sp.gov.br/assets/estatistica/transparencia/spDados/SPDadosCriminais_{year}.xlsx"
OSM_URL = "https://download.geofabrik.de/south-america/brazil/sudeste-latest.osm.pbf"

# files each source must leave behind (fresh or from the previous run)
OUTPUTS = {
    "ssp": [PROC / "ssp_study.csv"],
    "geosampa": [RAW / "distritos.json", RAW / "linha_trem.json", RAW / "geosampa/linha_onibus.geojson", RAW / "geosampa/arvore.geojson"],
    "ndvi": [PROC / "ndvi.tif"],
    "osm": [PROC / "osm_pois.geojson", PROC / "osm_places.geojson"],
}


def py(script, *args):
    env = {**os.environ, "PYTHONUTF8": "1", "OMSP_WORK": str(WORK), "OMSP_OUT": str(OUT)}
    subprocess.run([sys.executable, "-P", str(PIPE / script), *map(str, args)], check=True, env=env)


def download(url, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "onde-morar-em-sp data update"}) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    os.replace(tmp, dest)
    print(f"  downloaded {dest.name} ({dest.stat().st_size / 1e6:.0f} MB)", flush=True)


# ---------------------------------------------------------------- sources
def step_ssp():
    folder = DL / "ssp"
    this_year = date.today().year
    got = 0
    for year in range(this_year - 4, this_year + 1):
        try:
            download(SSP_URL.format(year=year), folder / f"SPDadosCriminais_{year}.xlsx")
            got += 1
        except requests.HTTPError as e:
            if year == this_year:  # early January the new year's file may not exist yet
                print(f"  {year}: {e} (skipped)", flush=True)
                continue
            raise
    py("extract_ssp.py", folder, PROC / "ssp_study.csv", BBOX)
    return {"files": got}


def step_geosampa():
    py("fetch_geosampa.py", RAW, BBOX)
    return {}


def step_ndvi():
    py("fetch_ndvi.py", PROC / "ndvi.tif", BBOX)
    return {"data_date": json.loads((PROC / "ndvi.tif.json").read_text())["date"]}


def step_osm():
    pbf = DL / "osm" / "sudeste-latest.osm.pbf"
    download(OSM_URL, pbf)
    py("extract_osm.py", pbf, PROC, BBOX)
    return {"data_date": json.loads((PROC / "osm.json").read_text())["date"]}


STEPS = {"ssp": step_ssp, "geosampa": step_geosampa, "ndvi": step_ndvi, "osm": step_osm}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated steps to run (ssp,geosampa,ndvi,osm,build,check)")
    ap.add_argument("--skip", default="", help="comma-separated steps to skip")
    args = ap.parse_args()
    wanted = set((args.only or "ssp,geosampa,ndvi,osm,build,check").split(",")) - set(filter(None, args.skip.split(",")))
    for d in (RAW, PROC, DL, OUT):
        d.mkdir(parents=True, exist_ok=True)

    sources_file = WORK / "sources.json"
    sources = json.loads(sources_file.read_text(encoding="utf-8")) if sources_file.exists() else {}
    now = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="minutes")  # dates shown on the site are local
    problems = []
    for name, fn in STEPS.items():
        if name not in wanted:
            continue
        print(f"== {name}", flush=True)
        try:
            extra = fn()
            sources[name] = {"status": "ok", "fetched": now[:10], **extra}
        except Exception as e:  # keep the previous files for this source
            traceback.print_exc()
            msg = f"{type(e).__name__}: {e}"[:300]
            prev = sources.get(name, {})
            sources[name] = {**prev, "status": "failed, using previous data", "last_error": msg, "last_attempt": now[:10]}
            problems.append(f"{name}: {msg}")
        missing = [str(p) for p in OUTPUTS[name] if not p.exists()]
        if missing:
            raise SystemExit(f"{name}: no usable data (missing {missing})")
    sources_file.write_text(json.dumps(sources, indent=1, ensure_ascii=False), encoding="utf-8")

    if "build" in wanted:
        print("== build", flush=True)
        py("build_site.py")
    if "check" in wanted:
        print("== check", flush=True)
        check()
    if problems:
        print("\nWARNING: some sources failed and kept their previous data:\n  " + "\n  ".join(problems), flush=True)
    print("update finished", flush=True)


def check():
    """Refuse to publish obviously broken data."""
    meta = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
    graph = json.loads((OUT / "graph.json").read_text(encoding="utf-8"))
    import gzip
    cells = json.loads(gzip.decompress((OUT / "cells.json.gz").read_bytes()))
    assert cells["n"] > 20000, f"only {cells['n']} cells"
    assert graph["nodes"] > 80000 and graph["edges"] > 500000, f"graph too small: {graph['nodes']} nodes"
    assert sum(cells["rob"]) > 0 and max(cells["ss"]) >= 50 and min(cells["ss"]) <= 10, "crime layer looks empty"
    assert sum(cells["arv"]) > 0 and sum(cells["nm"]) > 0, "trees or shops missing"
    last = date.fromisoformat(meta["crime_last"] + "-01")
    assert (date.today() - last).days < 200, f"crime data stops at {meta['crime_last']}"
    # routing sanity: known commutes to CGE-SP must stay plausible
    subprocess.run(["node", str(PIPE / "test_router.mjs"), "--check"], check=True,
                   env={**os.environ, "OMSP_OUT": str(OUT)})
    print("checks ok", flush=True)


if __name__ == "__main__":
    main()
