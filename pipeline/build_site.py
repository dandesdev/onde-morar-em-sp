"""Build the static data for the "Onde Morar Em SP" website (whole city of São Paulo).

Usage: python -P scripts/build_site.py   (run from the project root)

Inputs: data/city/raw (GeoSampa, rail, districts) and data/city/processed (NDVI, OSM, SSP crime CSV).
Outputs: site/data/
  (large files are written as *.json.gz / graph.bin.gz)
  cells.json     grid definition + per-cell attributes (columnar)
  graph.bin      transit network for in-browser routing (see GRAPH LAYOUT below)
  graph.json     sizes/offsets of graph.bin sections + line/station labels
  bairros.json, distritos.json, rail.json, stations.json, greens.json, bigroads.json, pois.json, meta.json

Reuses the scoring functions of scripts/build.py (the CGE-only map) by pointing its paths at the city data.
"""
import gzip
import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from scipy.spatial import cKDTree
from shapely.geometry import box
from shapely.ops import unary_union

import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))  # run with python -P, so add pipeline/ explicitly
import build as B  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]  # repository root
# OMSP_WORK holds downloads (raw/) and intermediate files (processed/); OMSP_OUT is the site's data/ folder.
WORK = Path(os.environ.get("OMSP_WORK", ROOT / "work"))
B.RAW = WORK / "raw"
B.PROC = WORK / "processed"
OUT = Path(os.environ.get("OMSP_OUT", ROOT / "data"))
MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
CRS = B.CRS

CITY_LL = (-46.84, -24.02, -46.35, -23.34)
B.STUDY_LL = CITY_LL
LAT_REF = -23.68
DLAT = 250 / 110574  # ~250 m cells on a lon/lat grid, so the browser can draw them as one image
DLON = 250 / (111320 * math.cos(math.radians(LAT_REF)))

K_HUBS = 16  # street nodes kept per cell for the browser routing
THIN = 250   # m between kept (non-station) street nodes


# ---------------------------------------------------------------- grid
def build_grid(distritos):
    lon0, lat0, lon1, lat1 = CITY_LL
    ncols = int(math.ceil((lon1 - lon0) / DLON))
    nrows = int(math.ceil((lat1 - lat0) / DLAT))
    ii, jj = np.meshgrid(np.arange(ncols), np.arange(nrows))
    ii, jj = ii.ravel(), jj.ravel()
    lon = lon0 + (ii + 0.5) * DLON
    lat = lat0 + (jj + 0.5) * DLAT
    city = unary_union(distritos.to_crs(4326).geometry)
    pts = gpd.GeoSeries(gpd.points_from_xy(lon, lat), crs=4326)
    inside = pts.within(city).to_numpy()
    ii, jj, lon, lat = ii[inside], jj[inside], lon[inside], lat[inside]
    boxes = [box(x - DLON / 2, y - DLAT / 2, x + DLON / 2, y + DLAT / 2) for x, y in zip(lon, lat)]
    g = gpd.GeoDataFrame({"i": ii, "j": jj, "lon": lon, "lat": lat}, geometry=boxes, crs=4326).to_crs(CRS)
    c = gpd.GeoSeries(gpd.points_from_xy(lon, lat), crs=4326).to_crs(CRS)
    g["px"], g["py"] = c.x.to_numpy(), c.y.to_numpy()
    j = gpd.sjoin(gpd.GeoDataFrame(geometry=c, crs=CRS), distritos[["nm_distrito_municipal", "geometry"]],
                  predicate="within", how="left")
    g["distrito"] = j.groupby(level=0)["nm_distrito_municipal"].first().map(B.accented_district).to_numpy()
    print("grid", ncols, "x", nrows, "->", len(g), "cells in the city", flush=True)
    return g.reset_index(drop=True), dict(lon0=lon0, lat0=lat0, dlon=DLON, dlat=DLAT, ncols=ncols, nrows=nrows)


# ---------------------------------------------------------------- faster versions for 25k cells
def ndvi_fast(grid):
    """Mean NDVI and share of pixels > 0.45 in a 250 m window, via summed-area tables."""
    with rasterio.open(B.PROC / B.NDVI_FILE) as src:
        a = src.read(1)
        tr = src.transform
    valid = ~np.isnan(a)
    v = np.where(valid, a, 0).astype("float64")

    def sat(x):
        s = np.zeros((x.shape[0] + 1, x.shape[1] + 1))
        s[1:, 1:] = x.cumsum(0).cumsum(1)
        return s

    S, N, G = sat(v), sat(valid.astype("float64")), sat((v > 0.45) & valid)
    col = ((grid.px.to_numpy() - tr.c) / tr.a).astype(int)
    row = ((grid.py.to_numpy() - tr.f) / tr.e).astype(int)
    h = 12  # pixels each side -> 250 m window
    r0, r1 = np.clip(row - h, 0, a.shape[0]), np.clip(row + h + 1, 0, a.shape[0])
    c0, c1 = np.clip(col - h, 0, a.shape[1]), np.clip(col + h + 1, 0, a.shape[1])

    def box_sum(s):
        return s[r1, c1] - s[r0, c1] - s[r1, c0] + s[r0, c0]

    n = box_sum(N)
    return box_sum(S) / np.maximum(n, 1), box_sum(G) / np.maximum(n, 1)


def roads_fast(grid):
    """Distance to the nearest arterial/VTR/highway and share of local streets within 200 m.

    Roads are densified into points every 20 m, so both measures become KD-tree lookups.
    """
    r = B.load(B.RAW / "geosampa/classificacao_viaria_cet.geojson")
    xs, ys, cls = [], [], []
    for geom, c in zip(r.geometry, r.dc_tipo_classificacao_viaria):
        if geom is None:
            continue
        for part in getattr(geom, "geoms", [geom]):
            n = max(int(part.length // 20), 1)
            for d in np.linspace(0, part.length, n + 1):
                p = part.interpolate(d)
                xs.append(p.x); ys.append(p.y); cls.append(c)
    xs, ys, cls = np.array(xs), np.array(ys), np.array(cls)
    big = np.isin(cls, ["Arterial", "VTR", "Rodovia"])
    local = np.isin(cls, ["Local", "Vias de pedestre"])
    cxy = grid[["px", "py"]].to_numpy()
    d_big, _ = cKDTree(np.c_[xs[big], ys[big]]).query(cxy)
    tree = cKDTree(np.c_[xs, ys])
    share = np.array([local[ix].mean() if ix else 0.0 for ix in tree.query_ball_point(cxy, 200)])
    big_roads = r[r.dc_tipo_classificacao_viaria.isin(["Arterial", "VTR", "Rodovia"])]
    return d_big, share, big_roads


# ---------------------------------------------------------------- transit graph
def transit_graph(grid):
    """Directed graph of the frequency-based transit model (same rules as build.commute).

    Node kinds: 0 = street hub, 1 = on a bus at a stop, 2 = on a train/metro at a station.
    Line 6 (Laranja) nodes/edges are kept apart so the browser can switch the scenario.
    """
    kind, lab, xy = [], [], []
    labels, label_ix = [], {}
    hubs = {}
    edges, edges_l6 = [], []

    def label(key, entry):
        if key not in label_ix:
            label_ix[key] = len(labels)
            labels.append(entry)
        return label_ix[key]

    def node(k, li=0, x=0.0, y=0.0):
        kind.append(k); lab.append(li); xy.append((x, y))
        return len(kind) - 1

    def hub(x, y):
        key = (int(x // B.HUB), int(y // B.HUB))
        if key not in hubs:
            hubs[key] = node(0, 0, key[0] * B.HUB + B.HUB / 2, key[1] * B.HUB + B.HUB / 2)
        return hubs[key]

    label(("none",), {"t": "-"})
    ext = gpd.GeoSeries([box(*CITY_LL)], crs=4326).to_crs(CRS).iloc[0].buffer(3000)

    lines = B.load(B.RAW / "geosampa/linha_onibus.geojson")
    lines = lines[lines.apply(B.runs_at_commute_hours, axis=1)]
    corridors = unary_union(B.load(B.RAW / "geosampa/corredor_onibus.geojson").geometry).buffer(30)
    for _, ln in lines.iterrows():
        g = ln.geometry
        if g is None or not g.intersects(ext):
            continue
        li = label(("bus", ln.cd_linha_geometria),
                   {"t": "b", "c": ln.cd_linha_geometria, "n": str(ln.nm_linha_onibus_geometria).title()})
        s = np.append(np.arange(0, g.length, B.BUS_STOP_GAP), g.length)
        prev = None
        for k, v_s in enumerate(s):
            p = g.interpolate(v_s)
            if not ext.contains(p):
                prev = None
                continue
            v = node(1, li, p.x, p.y)
            h = hub(p.x, p.y)
            edges.append((h, v, B.BUS_WAIT + 1.0))
            edges.append((v, h, 0.0))
            if prev is not None:
                mid = g.interpolate((s[k - 1] + v_s) / 2)
                speed = B.BUS_SPEED_CORRIDOR if corridors.contains(mid) else B.BUS_SPEED
                edges.append((prev, v, (v_s - s[k - 1]) / speed))
            prev = v
    print("bus nodes done", len(kind), flush=True)

    st, adj = B.rail_network("l6")
    rail_ids = []
    for _, r in st.iterrows():
        short = B.LINE_INFO[r.line][0].replace("Linha ", "L").split("-")[0]
        li = label(("rail", r.line, r["name"]), {"t": "r", "c": short, "n": r["name"].title(), "col": B.LINE_INFO[r.line][1]})
        v = node(2, li, r.x, r.y)
        h = hub(r.x, r.y)
        target = edges_l6 if r.line == "LARANJA" else edges
        target.append((h, v, B.RAIL_ACCESS + B.LINE_INFO[r.line][2]))
        target.append((v, h, B.RAIL_EGRESS))
        rail_ids.append(v)
    for u, nbrs in enumerate(adj):
        for v, t in nbrs:
            if st.line[u] == st.line[v]:
                (edges_l6 if st.line[u] == "LARANJA" else edges).append((rail_ids[u], rail_ids[v], t))

    hub_ids = np.array(list(hubs.values()))
    hxy = np.array([xy[i] for i in hub_ids])
    for a, b in cKDTree(hxy).query_pairs(B.TRANSFER_RADIUS):
        w = np.hypot(*(hxy[a] - hxy[b])) / B.WALK
        edges.append((hub_ids[a], hub_ids[b], w))
        edges.append((hub_ids[b], hub_ids[a], w))

    # cells -> street hubs they can walk to. Plain k-nearest picks a cluster of bus stops on the closest
    # street and misses the metro station 600 m away, so: always keep station hubs in range, then thin the
    # rest to >= THIN m apart (shorter walks between stops are already graph edges).
    rail_hub = {hub(r.x, r.y) for _, r in st.iterrows()}
    cxy = grid[["px", "py"]].to_numpy()
    cell_hub = np.full((len(grid), K_HUBS), 0xFFFFFFFF, dtype="<u4")
    cell_walk = np.full((len(grid), K_HUBS), 255, dtype="u1")  # 0.25 min units, 255 = none
    tree = cKDTree(hxy)
    for c, cand in enumerate(tree.query_ball_point(cxy, B.MAX_ACCESS)):
        if not cand:
            continue
        cand = np.asarray(cand)
        dist = np.hypot(*(hxy[cand] - cxy[c]).T)
        order = cand[np.argsort(dist)]
        kept = [h for h in order if hub_ids[h] in rail_hub][:4]
        for h in order:
            if len(kept) >= K_HUBS:
                break
            if h in kept:
                continue
            if all(np.hypot(*(hxy[h] - hxy[k])) >= THIN for k in kept if hub_ids[k] not in rail_hub):
                kept.append(h)
        kept.sort(key=lambda h: np.hypot(*(hxy[h] - cxy[c])))
        for k, h in enumerate(kept):
            cell_hub[c, k] = hub_ids[h]
            cell_walk[c, k] = min(round(np.hypot(*(hxy[h] - cxy[c])) / B.WALK * 4), 254)

    print(f"graph: {len(kind)} nodes ({len(hubs)} hubs), {len(edges)} edges + {len(edges_l6)} Line 6 edges", flush=True)
    return kind, lab, xy, labels, edges, edges_l6, cell_hub, cell_walk


def write_graph(kind, lab, xy, labels, edges, edges_l6, cell_hub, cell_walk):
    """GRAPH LAYOUT (little endian), sections in this order, each 4-byte aligned:
      lon Float32[N], lat Float32[N], kind Uint8[N], label Uint16[N],
      roff Uint32[N+1], rsrc Uint32[E], rw Uint16[E]      reverse CSR: edges INTO each node, weight in 0.1 min
      l6from Uint32[E6], l6to Uint32[E6], l6w Uint16[E6]
      cellHub Uint32[C*K], cellWalk Uint8[C*K]            walk in 0.25 min, 255 = none
    """
    n = len(kind)
    ll = gpd.GeoSeries(gpd.points_from_xy([p[0] for p in xy], [p[1] for p in xy]), crs=CRS).to_crs(4326)
    e = np.array(edges, dtype="float64")
    src, dst, w = e[:, 0].astype("<u4"), e[:, 1].astype("<u4"), np.minimum(np.round(e[:, 2] * 10), 65535).astype("<u2")
    order = np.argsort(dst, kind="stable")
    src, dst, w = src[order], dst[order], w[order]
    roff = np.zeros(n + 1, dtype="<u4")
    np.add.at(roff, dst.astype(np.int64) + 1, 1)
    roff = np.cumsum(roff).astype("<u4")
    e6 = np.array(edges_l6, dtype="float64")
    sections = [
        ("lon", ll.x.to_numpy().astype("<f4")), ("lat", ll.y.to_numpy().astype("<f4")),
        ("kind", np.array(kind, dtype="u1")), ("label", np.array(lab, dtype="<u2")),
        ("roff", roff), ("rsrc", src), ("rw", w),
        ("l6from", e6[:, 0].astype("<u4")), ("l6to", e6[:, 1].astype("<u4")),
        ("l6w", np.round(e6[:, 2] * 10).astype("<u2")),
        ("cellHub", cell_hub.ravel()), ("cellWalk", cell_walk.ravel()),
    ]
    layout, buf, off = {}, bytearray(), 0
    for name, arr in sections:
        pad = (-len(buf)) % 4
        buf += b"\0" * pad
        layout[name] = {"offset": len(buf), "length": int(arr.size), "type": arr.dtype.str}
        buf += arr.tobytes()
    (OUT / "graph.bin.gz").write_bytes(gzip.compress(bytes(buf), 9))
    meta = {"nodes": n, "edges": int(len(src)), "edgesL6": int(len(e6)), "k": K_HUBS, "sections": layout,
            "labels": labels, "walk_m_per_min": B.WALK}
    (OUT / "graph.json").write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"graph.bin {len(buf) / 1e6:.1f} MB raw, {(OUT / 'graph.bin.gz').stat().st_size / 1e6:.1f} MB gzipped", flush=True)


# ---------------------------------------------------------------- main
def set_windows():
    """Point the crime windows at the newest statistical month in the data; find the NDVI raster."""
    ym = pd.read_csv(B.PROC / "ssp_study.csv", usecols=["ANO_ESTATISTICA", "MES_ESTATISTICA"]).dropna().astype(int)
    last_y, last_m = max(zip(ym.ANO_ESTATISTICA, ym.MES_ESTATISTICA))
    first_y = int(ym.ANO_ESTATISTICA.min())
    months = [(last_y - (1 if m > last_m else 0), m) for m in range(1, 13)]
    B.LAST12 = sorted(months)
    B.BASE_YEARS = (last_y - 4, last_y - 3)
    B.VIOLENT_FROM_YEAR = last_y - 2
    B.VIOLENT_SPAN_YEARS = 2 + last_m / 12
    start_y, start_m = B.LAST12[0]
    ndvis = sorted(B.PROC.glob("ndvi*.tif"))
    if not ndvis:
        raise SystemExit("no NDVI raster in " + str(B.PROC))
    B.NDVI_FILE = ndvis[-1].name
    w = {
        "crime_window": f"{MONTHS[start_m - 1]}/{start_y} a {MONTHS[last_m - 1]}/{last_y}",
        "crime_years": f"jan/{first_y} a {MONTHS[last_m - 1]}/{last_y}",
        "crime_last": f"{last_y}-{last_m:02d}",
        "base_years": list(B.BASE_YEARS),
        "base_label": f"{B.BASE_YEARS[0]}-{str(B.BASE_YEARS[1])[2:]}",
    }
    print("windows", w, "ndvi", B.NDVI_FILE, flush=True)
    return w


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    windows = set_windows()
    distritos = B.load(B.RAW / "distritos.json")
    grid, gdef = build_grid(distritos)

    print("graph", flush=True)
    write_graph(*transit_graph(grid))
    print("ndvi", flush=True)
    nd, canopy = ndvi_fast(grid)
    trees = B.street_trees(grid)
    print("roads", flush=True)
    d_big, local_share, big_roads = roads_fast(grid)
    lights = B.lighting(grid)
    print("errands", flush=True)
    er, pois, greens = B.errands(grid)
    print("bairros", flush=True)
    bairro, _ = B.bairros(grid, distritos)
    grid["bairro"] = bairro
    print("crime", flush=True)
    cr, yearly, viol = B.crime(grid, bairro)

    s_green = (0.6 * B.pct(nd) + 0.4 * B.pct(trees)).round().astype(int)
    s_errands = (100 * (B.walk_score(er["mercado"]) + B.walk_score(er["padaria"]) + B.walk_score(er["farmacia"])
                        + B.walk_score(er["verde"]) + 0.5 * B.walk_score(er["pet"])) / 4.5).round().astype(int)
    s_calm = (100 * (0.6 * np.clip(d_big / 300, 0, 1) + 0.4 * local_share)).round().astype(int)
    s_safe = B.pct(cr["roubo"] + 0.5 * cr["roubo_noite"] + 0.2 * cr["furto"] + 5 * cr["violento"], higher_better=False)

    bnames = sorted(set(grid.bairro.dropna()))
    dnames = sorted(set(grid.distrito.dropna()))
    bix = {b: k for k, b in enumerate(bnames)}
    dix = {d: k for k, d in enumerate(dnames)}

    def mins(a):  # minutes with one decimal, null when unreachable
        return [None if not np.isfinite(v) else round(float(v), 1) for v in a]

    def ints(a):
        return [int(v) for v in np.round(a)]

    cells = {
        "grid": gdef, "n": len(grid), "bairros": bnames, "distritos": dnames,
        "i": grid.i.astype(int).tolist(), "j": grid.j.astype(int).tolist(),
        "b": [bix.get(b, -1) for b in grid.bairro], "d": [dix.get(d, -1) for d in grid.distrito],
        "ndvi": [round(float(v), 2) for v in nd], "can": ints(canopy * 100), "arv": ints(trees),
        "dbig": ints(np.minimum(d_big, 9999)), "loc": ints(local_share * 100), "luz": ints(lights),
        "tm": mins(er["mercado"]), "tp": mins(er["padaria"]), "tf": mins(er["farmacia"]), "tv": mins(er["verde"]),
        "tpk": mins(er["parque"]), "tpet": mins(er["pet"]), "tfe": mins(er["feira"]),
        "nm": ints(er["mercado_n"]), "np": ints(er["padaria_n"]), "nf": ints(er["farmacia_n"]),
        "rob": ints(cr["roubo"]), "robn": ints(cr["roubo_noite"]), "fur": ints(cr["furto"]),
        "vio": [round(float(v), 1) for v in cr["violento"]], "rveh": ints(cr["roubo_veic"]), "rob0": ints(cr["roubo_2223"]),
        "sg": s_green.tolist(), "se": s_errands.tolist(), "sc": s_calm.tolist(), "ss": [int(v) for v in s_safe],
    }
    write("cells", cells)

    # bairro polygons (cells dissolved) + yearly robbery counts for the trend column
    g = grid[["bairro", "geometry"]].copy()
    agg = pd.DataFrame({"n": grid.groupby("bairro").size()})
    agg["distrito"] = grid.groupby("bairro")["distrito"].agg(lambda s: s.mode().iat[0] if len(s.mode()) else None)
    agg = agg.join(yearly, how="left").fillna(0)
    polys = g.dissolve("bairro").join(agg).reset_index().to_crs(4326)
    polys["geometry"] = polys.geometry.simplify(0.0003)
    polys.columns = [str(c) for c in polys.columns]
    write("bairros", polys.to_json(drop_id=True, default=str))

    dist_out = distritos.to_crs(4326)[["nm_distrito_municipal", "geometry"]].copy()
    dist_out["nm_distrito_municipal"] = dist_out.nm_distrito_municipal.map(B.accented_district)
    dist_out["geometry"] = dist_out.geometry.simplify(0.0003)
    write("distritos", dist_out.to_json(drop_id=True))

    write_rail()
    green_out = gpd.GeoDataFrame(geometry=greens, crs=CRS).to_crs(4326)
    green_out["geometry"] = green_out.geometry.simplify(0.0001)
    write("greens", green_out.to_json(drop_id=True))
    big_out = big_roads.to_crs(4326)[["geometry"]].copy()
    big_out["geometry"] = big_out.geometry.simplify(0.0001)
    write("bigroads", big_out.to_json(drop_id=True))

    poi_out = [{"c": r["cat"], "n": r["name"], "x": round(p.x, 5), "y": round(p.y, 5)}
               for (_, r), p in zip(pois.iterrows(), pois.to_crs(4326).geometry) if r["cat"] != "park"]
    feiras = gpd.read_file(B.RAW / "geosampa/equipamento_feira_livre.geojson")
    poi_out += [{"c": "feira", "n": f"Feira {str(r.nm_equipamento).title()} ({(r.tx_dia_funcionamento or '').title()})",
                 "x": round(r.geometry.x, 5), "y": round(r.geometry.y, 5)} for _, r in feiras.iterrows()]
    write("pois", poi_out)
    sources = {}
    if (WORK / "sources.json").exists():
        sources = json.loads((WORK / "sources.json").read_text(encoding="utf-8"))
    write("meta", {"built": pd.Timestamp.now(tz="America/Sao_Paulo").isoformat(timespec="minutes"), **windows,
                   "ndvi_date": sources.get("ndvi", {}).get("data_date") or B.NDVI_FILE[5:15],
                   "osm_date": sources.get("osm", {}).get("data_date"),
                   "geosampa_date": sources.get("geosampa", {}).get("fetched"),
                   "ssp_date": sources.get("ssp", {}).get("fetched"),
                   "sources": sources, "city_bbox": CITY_LL})
    print("done", {p.name: f"{p.stat().st_size / 1e6:.1f} MB" for p in sorted(OUT.glob("*"))}, flush=True)


def write_rail():
    rail = []
    for fname in ("linha_metro", "linha_trem"):
        for f in json.load(open(B.RAW / f"{fname}.json", encoding="utf-8"))["features"]:
            ln = f["properties"]["nm_linha_metro_trem"]
            if ln in B.LINE_INFO:
                rail.append({"type": "Feature", "geometry": f["geometry"],
                             "properties": {"line": B.LINE_INFO[ln][0], "color": B.LINE_INFO[ln][1], "status": "op"}})
    for f in json.load(open(B.RAW / "linha_metro_projetada.json", encoding="utf-8"))["features"]:
        if f["properties"]["nm_linha_metro_trem"] == "LARANJA":
            rail.append({"type": "Feature", "geometry": f["geometry"],
                         "properties": {"line": "Linha 6-Laranja", "color": B.LINE_INFO["LARANJA"][1], "status": "l6"}})
    write("rail", {"type": "FeatureCollection", "features": rail})
    st_rows = {}
    for fname in ("estacao_metro", "estacao_trem"):
        for f in json.load(open(B.RAW / f"{fname}.json", encoding="utf-8"))["features"]:
            p = f["properties"]
            ln = p["nm_linha_metro_trem"]
            if ln not in B.LINE_INFO or p.get("tx_situacao_metro_trem") not in (None, "OPERANDO"):
                continue
            key = (p["nm_estacao_metro_trem"].replace(" - ", "-"), "op")
            lon, lat = f["geometry"]["coordinates"][:2]
            s = st_rows.setdefault(key, {"name": key[0].title(), "lon": round(lon, 6), "lat": round(lat, 6), "lines": [], "status": "op"})
            if B.LINE_INFO[ln][0] not in s["lines"]:
                s["lines"].append(B.LINE_INFO[ln][0])
    for f in json.load(open(B.RAW / "estacao_metro_projetada.json", encoding="utf-8"))["features"]:
        p = f["properties"]
        if p["nm_linha_metro_trem"] != "LARANJA" or p["nm_estacao_metro_trem"] not in B.LINE6_CORE:
            continue
        name = p["nm_estacao_metro_trem"]
        lon, lat = f["geometry"]["coordinates"][:2]
        note = " (operação assistida 10h-15h)" if name in B.LINE6_ASSISTED else " (em obras)"
        st_rows[(name, "l6")] = {"name": name.title(), "lon": round(lon, 6), "lat": round(lat, 6),
                                 "lines": ["Linha 6-Laranja" + note], "status": "l6"}
    write("stations", list(st_rows.values()))


SMALL = {"meta", "stations"}  # everything else is shipped gzipped and unpacked in the browser


def write(name, obj):
    txt = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if name in SMALL:
        (OUT / f"{name}.json").write_text(txt, encoding="utf-8")
    else:
        (OUT / f"{name}.json.gz").write_bytes(gzip.compress(txt.encode("utf-8"), 9))


if __name__ == "__main__":
    main()
