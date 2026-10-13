"""Scoring library shared by build_site.py (originally the CGE-only map builder).

Usage: python -P scripts/build.py   (run from the project root)

Inputs come from data/raw (GeoSampa, SSP-SP) and data/processed (NDVI, OSM).
Outputs go to web/data/*.js as `window.DATA_<NAME> = {...}` so the map works from file://.
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import shapely
from scipy.signal import fftconvolve
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.spatial import cKDTree
from shapely.geometry import Point, box, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
RAW, PROC, OUT = ROOT / "data/raw", ROOT / "data/processed", ROOT / "web/data"
CRS = 31983  # SIRGAS 2000 / UTM 23S, metres
CELL = 250

CGE_LL = (-46.6760, -23.5275)  # Rua Barão de Tefé, 72
STUDY_LL = (-46.775, -23.615, -46.595, -23.445)

WALK = 75 / 1.3  # m of straight line per minute (75 m/min on a street grid ~1.3x longer)
MAX_ACCESS = 1000  # straight-line metres people will walk to a stop or station

# Peak-hour assumptions. Half the headway is the expected wait.
LINE_INFO = {
    # name in GeoSampa: (label, colour, wait min, speed m/min, dwell min)
    "AZUL": ("Linha 1-Azul", "#0455A1", 1.0, 600, 0.5),
    "VERDE": ("Linha 2-Verde", "#007E5E", 1.5, 600, 0.5),
    "VERMELHA": ("Linha 3-Vermelha", "#EE372F", 1.0, 600, 0.5),
    "AMARELA": ("Linha 4-Amarela", "#FFD400", 1.0, 650, 0.5),
    "LILÁS": ("Linha 5-Lilás", "#9B3894", 1.5, 600, 0.5),
    "PRATA": ("Linha 15-Prata", "#A9A9A9", 2.0, 500, 0.5),
    "RUBI": ("Linha 7-Rubi", "#CA016B", 4.0, 750, 0.7),
    "DIAMANTE": ("Linha 8-Diamante", "#97A098", 3.0, 750, 0.7),
    "ESMERALDA": ("Linha 9-Esmeralda", "#01A9A7", 2.5, 750, 0.7),
    "TURQUESA": ("Linha 10-Turquesa", "#049FC3", 4.0, 750, 0.7),
    "CORAL": ("Linha 11-Coral", "#F68368", 3.0, 750, 0.7),
    "SAFIRA": ("Linha 12-Safira", "#133C8D", 4.0, 750, 0.7),
    "JADE": ("Linha 13-Jade", "#00B352", 8.0, 750, 0.7),
    "LARANJA": ("Linha 6-Laranja", "#F68B1F", 2.0, 600, 0.5),
}
TRANSFER_WALK = 3.0
LINE6_CORE = ["BRASILÂNDIA", "VILA CARDOSO", "ITABERABA-HOSPITAL VILA PENTEADO", "JOÃO PAULO I", "FREGUESIA DO Ó",
              "SANTA MARINA", "ÁGUA BRANCA", "SESC POMPEIA", "PERDIZES", "PUC-CARDOSO DE ALMEIDA", "FAAP-PACAEMBU",
              "HIGIENÓPOLIS-MACKENZIE", "14 BIS", "BELA VISTA", "SÃO JOAQUIM"]
LINE6_ASSISTED = ["JOÃO PAULO I", "FREGUESIA DO Ó", "SANTA MARINA", "ÁGUA BRANCA", "SESC POMPEIA", "PERDIZES"]

BUS_SPEED = 14_000 / 60  # m/min, peak average incl. stops, mixed traffic
BUS_SPEED_CORRIDOR = 20_000 / 60  # on exclusive bus corridors
BUS_WAIT = 5.0  # no frequency data in GeoSampa; ~10 min headway assumed
BUS_STOP_GAP = 350  # m between modelled stops
RAIL_ACCESS = 2.0  # min from street to platform
RAIL_EGRESS = 1.0
HUB = 60  # m, street nodes are snapped to this grid
TRANSFER_RADIUS = 300  # m walked between stops when transferring

CRIME_RADIUS = 400  # m, kernel radius for crime density
# Crime time windows. build_site.py sets them from the newest month present in the data.
LAST12 = [(2025, m) for m in range(9, 13)] + [(2026, m) for m in range(1, 9)]  # last 12 statistical months
BASE_YEARS = (2022, 2023)       # baseline for the robbery trend
VIOLENT_FROM_YEAR = 2024        # violent crimes are rare: use ~2.5 years instead of 12 months
VIOLENT_SPAN_YEARS = 2 + 8 / 12
NDVI_FILE = "ndvi_2026-07-19.tif"


def load(path, crs=4326):
    g = gpd.read_file(path)
    if g.crs is None:
        g = g.set_crs(crs)
    return g.to_crs(CRS)


def to_xy(lon, lat):
    p = gpd.GeoSeries([Point(lon, lat)], crs=4326).to_crs(CRS).iloc[0]
    return p.x, p.y


# ---------------------------------------------------------------- grid
def build_grid(distritos):
    study = gpd.GeoSeries([box(*STUDY_LL)], crs=4326).to_crs(CRS).iloc[0]
    city = unary_union(distritos.geometry)
    x0, y0, x1, y1 = study.bounds
    xs = np.arange(x0, x1, CELL)
    ys = np.arange(y0, y1, CELL)
    cells = [box(x, y, x + CELL, y + CELL) for y in ys for x in xs]
    g = gpd.GeoDataFrame(geometry=cells, crs=CRS)
    g = g[g.centroid.within(city)].reset_index(drop=True)
    g["px"], g["py"] = g.centroid.x, g.centroid.y
    j = gpd.sjoin(gpd.GeoDataFrame(geometry=g.centroid, crs=CRS), distritos[["nm_distrito_municipal", "geometry"]],
                  predicate="within", how="left")
    g["distrito"] = j.groupby(level=0)["nm_distrito_municipal"].first().map(accented_district)
    print("cells", len(g))
    return g


def accented_district(name):
    """GeoSampa stores district names without accents; borrow the spelling from OSM (admin_level 9)."""
    if not hasattr(accented_district, "names"):
        import unicodedata
        key = lambda t: unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().upper().strip()
        pl = json.load(open(PROC / "osm_places.geojson", encoding="utf-8"))["features"]
        accented_district.key = key
        accented_district.names = {key(f["properties"]["name"]): f["properties"]["name"]
                                    for f in pl if f["properties"].get("admin_level") == "9" and f["properties"].get("name")}
    if not isinstance(name, str):
        return name
    return accented_district.names.get(accented_district.key(name), name.title())


# ---------------------------------------------------------------- rail
def rail_network(scenario):
    """Stations as nodes, consecutive stations (per line MST) as edges. Returns nodes df + adjacency."""
    rows = []
    for fname, ok in (("estacao_metro", True), ("estacao_trem", True)):
        for f in json.load(open(RAW / f"{fname}.json", encoding="utf-8"))["features"]:
            p = f["properties"]
            if p.get("tx_situacao_metro_trem") not in (None, "OPERANDO"):
                continue
            lon, lat = f["geometry"]["coordinates"][:2]
            rows.append((p["nm_estacao_metro_trem"].replace(" - ", "-"), p["nm_linha_metro_trem"], lon, lat))
    l6 = {f["properties"]["nm_estacao_metro_trem"]: f["geometry"]["coordinates"][:2]
          for f in json.load(open(RAW / "estacao_metro_projetada.json", encoding="utf-8"))["features"]
          if f["properties"]["nm_linha_metro_trem"] == "LARANJA"}
    if scenario == "l6":
        rows += [(n, "LARANJA", *l6[n]) for n in LINE6_CORE]
    st = pd.DataFrame(rows, columns=["name", "line", "lon", "lat"]).drop_duplicates(["name", "line"])
    pts = gpd.GeoSeries(gpd.points_from_xy(st.lon, st.lat), crs=4326).to_crs(CRS)
    st["x"], st["y"] = pts.x.values, pts.y.values
    st = st[st.line.isin(LINE_INFO)].reset_index(drop=True)

    n = len(st)
    adj = [[] for _ in range(n)]
    for line, grp in st.groupby("line"):
        idx = grp.index.to_numpy()
        xy = grp[["x", "y"]].to_numpy()
        d = np.hypot(*(xy[:, None, :] - xy[None, :, :]).transpose(2, 0, 1))
        mst = minimum_spanning_tree(d).tocoo()
        _, _, _, speed, dwell = LINE_INFO[line]
        for a, b, dist in zip(mst.row, mst.col, mst.data):
            t = dwell + dist * 1.1 / speed
            adj[idx[a]].append((idx[b], t))
            adj[idx[b]].append((idx[a], t))
    # transfers between lines at the same / adjacent stations
    tree = cKDTree(st[["x", "y"]].to_numpy())
    for a, b in tree.query_pairs(350):
        if st.line[a] != st.line[b]:
            adj[a].append((b, TRANSFER_WALK + LINE_INFO[st.line[b]][2]))
            adj[b].append((a, TRANSFER_WALK + LINE_INFO[st.line[a]][2]))
    return st, adj


def runs_at_commute_hours(ln):
    """Drop night lines (N###) and lines whose first/last trip don't cover 07:00-19:00."""
    if str(ln.cd_linha_geometria).startswith("N"):
        return False
    try:
        first = pd.Timestamp(ln.ho_primeira_viagem)
        last = pd.Timestamp(ln.ho_ultima_viagem)
    except (TypeError, ValueError):
        return True
    epoch = pd.Timestamp("1970-01-01T00:00:00Z")
    # stored in UTC (e.g. 07:40Z = 04:40 in São Paulo); convert to local minutes since midnight
    f_min = ((first - epoch).total_seconds() / 60 - 180) % (24 * 60)
    l_min = ((last - epoch).total_seconds() / 60 - 180) % (24 * 60)
    if l_min < f_min:  # service runs past midnight
        l_min += 24 * 60
    return f_min <= 7 * 60 and l_min >= 19 * 60


def commute(grid, cge_xy, scenario):
    """Frequency-based multimodal model: walk + bus + metro/train, reverse Dijkstra from CGE.

    Node kinds: 'hub' = a street location (60 m snapped), 'veh' = being on a bus/train at a stop, 'cge'.
    Edges: hub->veh = boarding (wait + platform access), veh->veh = riding, veh->hub = alighting,
    hub<->hub = walking up to TRANSFER_RADIUS, hub->cge = final walk.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra

    kind, label, name, xy = [], [], [], []
    hubs = {}
    rows, cols, wts = [], [], []

    def node(k, lab="", nm="", x=0.0, y=0.0):
        kind.append(k); label.append(lab); name.append(nm); xy.append((x, y))
        return len(kind) - 1

    def hub(x, y):
        key = (int(x // HUB), int(y // HUB))
        if key not in hubs:
            hubs[key] = node("hub", x=key[0] * HUB + HUB / 2, y=key[1] * HUB + HUB / 2)
        return hubs[key]

    def edge(a, b, w):
        rows.append(a); cols.append(b); wts.append(max(w, 1e-3))

    ext = gpd.GeoSeries([box(*STUDY_LL)], crs=4326).to_crs(CRS).iloc[0].buffer(3000)

    # ---- buses
    lines = load(RAW / "geosampa/linha_onibus.geojson")
    lines = lines[lines.apply(runs_at_commute_hours, axis=1)]
    corridors = unary_union(load(RAW / "geosampa/corredor_onibus.geojson").geometry).buffer(30)
    for _, ln in lines.iterrows():
        g = ln.geometry
        if g is None or not g.intersects(ext):
            continue
        s = np.append(np.arange(0, g.length, BUS_STOP_GAP), g.length)
        pts = [g.interpolate(v) for v in s]
        prev = None
        code = f"ônibus {ln.cd_linha_geometria}"
        for k, p in enumerate(pts):
            if not ext.contains(p):
                prev = None
                continue
            v = node("veh", code, ln.nm_linha_onibus_geometria.title(), p.x, p.y)
            h = hub(p.x, p.y)
            edge(h, v, BUS_WAIT + 1.0)
            edge(v, h, 0.0)
            if prev is not None:
                mid = g.interpolate((s[k - 1] + s[k]) / 2)
                speed = BUS_SPEED_CORRIDOR if corridors.contains(mid) else BUS_SPEED
                edge(prev, v, (s[k] - s[k - 1]) / speed)
            prev = v

    # ---- rail
    st, adj = rail_network(scenario)
    rail_ids = []
    for i, r in st.iterrows():
        lab = LINE_INFO[r.line][0].replace("Linha ", "L").split("-")[0]
        v = node("veh", lab, r["name"].title(), r.x, r.y)
        h = hub(r.x, r.y)
        edge(h, v, RAIL_ACCESS + LINE_INFO[r.line][2])
        edge(v, h, RAIL_EGRESS)
        rail_ids.append(v)
    for u, nbrs in enumerate(adj):
        for v, t in nbrs:
            if st.line[u] == st.line[v]:
                edge(rail_ids[u], rail_ids[v], t)

    # ---- walking between hubs, and to CGE
    hub_ids = np.array(list(hubs.values()))
    hxy = np.array([xy[i] for i in hub_ids])
    tree = cKDTree(hxy)
    for a, b in tree.query_pairs(TRANSFER_RADIUS):
        w = np.hypot(*(hxy[a] - hxy[b])) / WALK
        edge(hub_ids[a], hub_ids[b], w)
        edge(hub_ids[b], hub_ids[a], w)
    cge = node("cge", x=cge_xy[0], y=cge_xy[1])
    for a in tree.query_ball_point(cge_xy, 1500):
        edge(hub_ids[a], cge, np.hypot(*(hxy[a] - np.array(cge_xy))) / WALK)

    n = len(kind)
    G = coo_matrix((wts, (rows, cols)), shape=(n, n)).tocsr()
    G.sum_duplicates()
    # time from every node to CGE = shortest path from CGE on the reversed graph
    dist, pred = dijkstra(G.T.tocsr(), indices=cge, return_predecessors=True)
    print(f"  {scenario}: {n} nodes, {G.nnz} edges")

    cxy = grid[["px", "py"]].to_numpy()
    walk = np.hypot(cxy[:, 0] - cge_xy[0], cxy[:, 1] - cge_xy[1]) / WALK
    best_t = walk.copy()
    best_h = np.full(len(grid), -1)
    best_a = np.zeros(len(grid))
    for i, cand in enumerate(tree.query_ball_point(cxy, MAX_ACCESS)):
        if not cand:
            continue
        cand = np.asarray(cand)
        acc = np.hypot(*(hxy[cand] - cxy[i]).T) / WALK
        tot = acc + dist[hub_ids[cand]]
        j = tot.argmin()
        if tot[j] < best_t[i]:
            best_t[i], best_h[i], best_a[i] = tot[j], hub_ids[cand[j]], acc[j]

    def describe(h, acc):
        legs, walk_min, u = [], acc, h
        while u != cge and u >= 0:
            v = pred[u]
            if v < 0:
                break
            w = G[u, v]
            if kind[u] == "hub" and kind[v] in ("hub", "cge"):
                walk_min += w
            if kind[v] == "veh" and (not legs or legs[-1][0] != label[v] or kind[u] == "hub"):
                legs.append([label[v], name[v], name[v]])
            elif kind[v] == "veh":
                legs[-1][2] = name[v]
            u = v
        parts = []
        for lab, a, b in legs:
            parts.append(f"{lab} ({a} → {b})" if lab.startswith("L") else f"{lab} {a}")
        return parts, walk_min

    mode, desc = [], []
    for i in range(len(grid)):
        if best_h[i] < 0:
            mode.append("a pé"); desc.append(f"A pé, {walk[i]:.0f} min"); continue
        parts, wm = describe(best_h[i], best_a[i])
        has_bus = any(p.startswith("ônibus") for p in parts)
        has_rail = any(p.startswith("L") for p in parts)
        mode.append("ônibus + metrô/trem" if has_bus and has_rail else "ônibus" if has_bus else "metrô/trem" if has_rail else "a pé")
        desc.append(" → ".join(parts) + f"; caminhada total ~{wm:.0f} min")
    return best_t, np.array(mode), desc


# ---------------------------------------------------------------- environment
def ndvi(grid):
    with rasterio.open(PROC / NDVI_FILE) as src:  # EPSG:32723 ≈ 31983 (sub-metre)
        a = src.read(1)
        tr = src.transform
    rows, cols = np.indices(a.shape)
    xs, ys = rasterio.transform.xy(tr, rows.ravel(), cols.ravel())
    xs, ys, v = np.asarray(xs), np.asarray(ys), a.ravel()
    ok = ~np.isnan(v)
    tree = cKDTree(grid[["px", "py"]].to_numpy())
    dist, idx = tree.query(np.c_[xs[ok], ys[ok]], distance_upper_bound=CELL * 0.71)
    m = np.isfinite(dist)
    idx, v = idx[m], v[ok][m]
    n = np.bincount(idx, minlength=len(grid))
    mean = np.bincount(idx, weights=v, minlength=len(grid)) / np.maximum(n, 1)
    canopy = np.bincount(idx, weights=(v > 0.45).astype(float), minlength=len(grid)) / np.maximum(n, 1)
    return mean, canopy


def street_trees(grid):
    t = load(RAW / "geosampa/arvore.geojson")
    tree = cKDTree(np.c_[t.geometry.x, t.geometry.y])
    return np.array([len(x) for x in tree.query_ball_point(grid[["px", "py"]].to_numpy(), 200)])


def roads(grid):
    r = load(RAW / "geosampa/classificacao_viaria_cet.geojson")
    big = r[r.dc_tipo_classificacao_viaria.isin(["Arterial", "VTR", "Rodovia"])]
    big_u = unary_union(big.geometry)
    pts = gpd.GeoSeries(gpd.points_from_xy(grid.px, grid.py), crs=CRS)
    d_big = pts.distance(big_u).to_numpy()
    buf = gpd.GeoDataFrame(geometry=pts.buffer(200), crs=CRS)
    r["len"] = r.length
    inter = gpd.overlay(r[["dc_tipo_classificacao_viaria", "geometry"]], buf.reset_index(), how="intersection",
                        keep_geom_type=True)
    inter["len"] = inter.length
    tot = inter.groupby("index")["len"].sum()
    loc = inter[inter.dc_tipo_classificacao_viaria.isin(["Local", "Vias de pedestre"])].groupby("index")["len"].sum()
    share = (loc / tot).reindex(range(len(grid))).fillna(0).to_numpy()
    return d_big, share, big


def lighting(grid):
    l = load(RAW / "geosampa/iluminacao_publica.geojson")
    tree = cKDTree(np.c_[l.geometry.x, l.geometry.y])
    return np.array([len(x) for x in tree.query_ball_point(grid[["px", "py"]].to_numpy(), 150)])


# ---------------------------------------------------------------- errands
def errands(grid):
    pois = load(PROC / "osm_pois.geojson")
    feiras = load(RAW / "geosampa/equipamento_feira_livre.geojson")
    cxy = grid[["px", "py"]].to_numpy()
    groups = {
        "mercado": ["supermarket", "hortifruti"],
        "padaria": ["bakery"],
        "farmacia": ["pharmacy"],
        "pet": ["vet", "petshop"],
        "mini": ["minimarket", "butcher", "market"],
    }
    res = {}
    for k, cats in groups.items():
        sub = pois[pois.cat.isin(cats)]
        d, _ = cKDTree(np.c_[sub.geometry.x, sub.geometry.y]).query(cxy)
        res[k] = d / WALK
        res[k + "_n"] = np.array([len(x) for x in cKDTree(np.c_[sub.geometry.x, sub.geometry.y]).query_ball_point(cxy, 600)])
    d, _ = cKDTree(np.c_[feiras.geometry.x, feiras.geometry.y]).query(cxy)
    res["feira"] = d / WALK
    # green space a dog can use: parks + squares >= 1500 m2
    praca = load(RAW / "geosampa/GEOSAMPA_v_praca_largo.geojson")
    parque = load(RAW / "geosampa/pde_parque_municipal.geojson")
    parque = parque[parque.tx_tipo_situacao_projeto.fillna("").str.contains("Exist|Implant", case=False)]
    osm_parks = [shape(p["area_geom"]) for p in pois[pois.cat.isin(["park", "dog_park"])].to_dict("records")
                 if isinstance(p.get("area_geom"), dict)]
    osm_parks = gpd.GeoSeries(osm_parks, crs=4326).to_crs(CRS)
    greens = pd.concat([praca.geometry, parque.geometry, osm_parks], ignore_index=True)
    greens = greens[greens.area >= 1500]
    pts = gpd.GeoSeries(gpd.points_from_xy(grid.px, grid.py), crs=CRS)
    sidx = greens.sindex
    near = sidx.nearest(pts, return_distance=True, return_all=False)
    dist = np.full(len(grid), np.inf)
    dist[near[0][0]] = near[1]
    res["verde"] = dist / WALK
    big = greens[greens.area >= 30000]
    near = big.sindex.nearest(pts, return_distance=True, return_all=False)
    dist = np.full(len(grid), np.inf)
    dist[near[0][0]] = near[1]
    res["parque"] = dist / WALK
    return res, pois, greens


# ---------------------------------------------------------------- crime
def crime(grid, bairro_of_cell):
    df = pd.read_csv(PROC / "ssp_study.csv", low_memory=False, usecols=[
        "NOME_DELEGACIA_CIRCUNSCRICAO", "NUM_BO", "ANO_BO", "NATUREZA_APURADA", "DESCR_SUBTIPOLOCAL",
        "DESC_PERIODO", "ANO_ESTATISTICA", "MES_ESTATISTICA", "LATITUDE", "LONGITUDE"])
    df = df.drop_duplicates(["NOME_DELEGACIA_CIRCUNSCRICAO", "NUM_BO", "ANO_BO", "NATUREZA_APURADA"])
    sub = df["DESCR_SUBTIPOLOCAL"].astype(str).str.upper().str.strip()
    nat = df["NATUREZA_APURADA"].astype(str).str.upper().str.strip()
    per = df["DESC_PERIODO"].astype(str).str.upper().str.strip()
    street = sub.isin(["VIA PÚBLICA", "TRANSEUNTE", "PRAÇA"])
    df["kind"] = None
    df.loc[street & nat.str.startswith("ROUBO - OUTROS"), "kind"] = "roubo"
    df.loc[street & nat.str.startswith("FURTO - OUTROS"), "kind"] = "furto"
    df.loc[nat.isin(["HOMICÍDIO DOLOSO", "LATROCÍNIO", "TENTATIVA DE HOMICÍDIO", "TENTATIVA DE HOMICIDIO",
                     "LESÃO CORPORAL SEGUIDA DE MORTE"]), "kind"] = "violento"
    df.loc[nat == "ROUBO DE VEÍCULO", "kind"] = "roubo_veiculo"
    df = df[df.kind.notna()].copy()
    df["night"] = per.loc[df.index].isin(["A NOITE", "DE MADRUGADA"])
    ym = list(zip(df.ANO_ESTATISTICA.astype(int), df.MES_ESTATISTICA.astype(int)))
    df["last12"] = [x in set(LAST12) for x in ym]
    df["year"] = df.ANO_ESTATISTICA.astype(int)
    pts = gpd.GeoSeries(gpd.points_from_xy(df.LONGITUDE.astype(float), df.LATITUDE.astype(float)), crs=4326).to_crs(CRS)
    df["x"], df["y"] = pts.x.values, pts.y.values

    # kernel density on a 50 m raster, disc of CRIME_RADIUS
    x0, y0 = grid.px.min() - 2000, grid.py.min() - 2000
    res = 50
    nx = int((grid.px.max() + 2000 - x0) / res) + 1
    ny = int((grid.py.max() + 2000 - y0) / res) + 1
    r = CRIME_RADIUS // res
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    disc = (xx ** 2 + yy ** 2 <= r * r).astype(float)
    area_km2 = disc.sum() * res * res / 1e6
    gi = ((grid.px - x0) / res).astype(int).to_numpy()
    gj = ((grid.py - y0) / res).astype(int).to_numpy()

    def density(mask, years):
        s = df[mask]
        h, _, _ = np.histogram2d(s.x, s.y, bins=[nx, ny], range=[[x0, x0 + nx * res], [y0, y0 + ny * res]])
        k = fftconvolve(h, disc, mode="same")
        return np.clip(k[gi, gj], 0, None) / area_km2 / years  # incidents per km2 per year

    out = {
        "roubo": density(df.last12 & (df.kind == "roubo"), 1),
        "roubo_noite": density(df.last12 & (df.kind == "roubo") & df.night, 1),
        "furto": density(df.last12 & (df.kind == "furto"), 1),
        "violento": density((df.year >= VIOLENT_FROM_YEAR) & (df.kind == "violento"), VIOLENT_SPAN_YEARS),
        "roubo_veic": density(df.last12 & (df.kind == "roubo_veiculo"), 1),
        "roubo_2223": density(df.year.isin(BASE_YEARS) & (df.kind == "roubo"), len(BASE_YEARS)),
    }
    # yearly robbery counts per bairro, for the trend table
    cell_tree = cKDTree(grid[["px", "py"]].to_numpy())
    dd, ii = cell_tree.query(np.c_[df.x, df.y], distance_upper_bound=CELL * 0.71)
    ok = np.isfinite(dd)
    df = df[ok].copy()
    df["bairro"] = bairro_of_cell[ii[ok]]
    df["period"] = np.where(df.last12, "12m", df.year.astype(str))
    rob = df[df.kind == "roubo"]
    yearly = rob.groupby(["bairro", "year"]).size().unstack(fill_value=0)
    yearly["12m"] = rob[rob.last12].groupby("bairro").size()
    viol = df[(df.kind == "violento") & (df.year >= VIOLENT_FROM_YEAR)].groupby("bairro").size()
    return out, yearly.fillna(0).astype(int), viol


# ---------------------------------------------------------------- bairros
def bairros(grid, distritos):
    pl = gpd.read_file(PROC / "osm_places.geojson")
    pl = pl[pl.geometry.geom_type == "Point"].to_crs(CRS)
    pl = pl[pl["name"].notna()]
    # drop favela/conjunto micro-labels that would fragment the map, keep proper bairros
    pl = pl[~pl["name"].str.contains("Comunidade|Favela|Conjunto|Conjuto|Setor", case=False)]
    pl = pl.drop_duplicates("name")
    pl = pl[pl.within(unary_union(distritos.geometry).buffer(300))]
    # extra anchors for names people use that OSM lacks as place nodes
    # Santa Marina = Line 6 station; Jardim das Perdizes = Rua Pablo Picasso (Nominatim)
    extra = {"Santa Marina": (-46.69152, -23.51451), "Jardim das Perdizes": (-46.68318, -23.51723)}
    for k, (lon, lat) in extra.items():
        if k not in set(pl["name"]):
            x, y = to_xy(lon, lat)
            pl = pd.concat([pl, gpd.GeoDataFrame({"name": [k]}, geometry=[Point(x, y)], crs=CRS)], ignore_index=True)
    tree = cKDTree(np.c_[pl.geometry.x, pl.geometry.y])
    _, idx = tree.query(grid[["px", "py"]].to_numpy())
    names = pl["name"].to_numpy()[idx]
    return names, pl


def pct(x, higher_better=True):
    s = pd.Series(x).replace([np.inf, -np.inf], np.nan)
    r = s.rank(pct=True)
    r = r if higher_better else 1 - r
    return (r.fillna(0 if higher_better else 0) * 100).round().astype(int).to_numpy()


def walk_score(m):
    """1 when <= 5 min, 0 at >= 15 min."""
    return np.clip((15 - m) / 10, 0, 1)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    distritos = load(RAW / "distritos.json")
    grid = build_grid(distritos)
    cge_xy = to_xy(*CGE_LL)

    print("commute")
    c_now, mode_now, desc_now = commute(grid, cge_xy, "now")
    c_l6, mode_l6, desc_l6 = commute(grid, cge_xy, "l6")
    print("ndvi")
    nd, canopy = ndvi(grid)
    trees = street_trees(grid)
    print("roads")
    d_big, local_share, big_roads = roads(grid)
    lights = lighting(grid)
    print("errands")
    er, pois, greens = errands(grid)
    print("bairros")
    bairro, places = bairros(grid, distritos)
    grid["bairro"] = bairro
    print("crime")
    cr, yearly, viol = crime(grid, bairro)

    # ---- sub-scores 0-100 (higher = better)
    s_green = (0.6 * pct(nd) + 0.4 * pct(trees)).round()
    s_errands = (100 * (walk_score(er["mercado"]) + walk_score(er["padaria"]) + walk_score(er["farmacia"])
                        + walk_score(er["verde"]) + 0.5 * walk_score(er["pet"])) / 4.5).round()
    s_calm = (100 * (0.6 * np.clip(d_big / 300, 0, 1) + 0.4 * local_share)).round()
    s_safe = pct(cr["roubo"] + 0.5 * cr["roubo_noite"] + 0.2 * cr["furto"] + 5 * cr["violento"], higher_better=False)

    def r1(a):
        return [None if not np.isfinite(v) else round(float(v), 1) for v in a]

    cells = gpd.GeoDataFrame({
        "b": grid.bairro, "d": grid.distrito,
        "cn": r1(c_now), "c6": r1(c_l6), "mn": mode_now, "m6": mode_l6, "dn": desc_now, "d6": desc_l6,
        "ndvi": np.round(nd, 2), "can": np.round(canopy * 100).astype(int), "arv": trees,
        "dbig": np.round(d_big).astype(int), "loc": np.round(local_share * 100).astype(int), "luz": lights,
        "tm": r1(er["mercado"]), "tp": r1(er["padaria"]), "tf": r1(er["farmacia"]), "tv": r1(er["verde"]),
        "tpk": r1(er["parque"]), "tpet": r1(er["pet"]), "tfe": r1(er["feira"]),
        "nm": er["mercado_n"], "np": er["padaria_n"], "nf": er["farmacia_n"],
        "rob": np.round(cr["roubo"]).astype(int), "robn": np.round(cr["roubo_noite"]).astype(int),
        "fur": np.round(cr["furto"]).astype(int), "vio": np.round(cr["violento"], 1),
        "rveh": np.round(cr["roubo_veic"]).astype(int), "rob0": np.round(cr["roubo_2223"]).astype(int),
        "sg": s_green.astype(int), "se": s_errands.astype(int), "sc": s_calm.astype(int), "ss": s_safe,
    }, geometry=grid.geometry, crs=CRS).to_crs(4326)

    # ---- bairro table + polygons (cells dissolved)
    cells_m = cells.to_crs(CRS)
    agg = cells_m.groupby("b").agg(
        distrito=("d", lambda s: s.mode().iat[0]), n=("b", "size"),
        cn=("cn", "median"), c6=("c6", "median"), sg=("sg", "mean"), se=("se", "mean"), sc=("sc", "mean"),
        ss=("ss", "mean"), rob=("rob", "mean"), robn=("robn", "mean"), ndvi=("ndvi", "mean"))
    agg = agg.join(yearly, how="left").join(viol.rename("vio_24_26"), how="left").fillna(0)
    area_km2 = agg.n * CELL * CELL / 1e6
    for col in [c for c in agg.columns if isinstance(c, (int, np.integer)) or c == "12m"]:
        agg[f"r_{col}"] = (agg[col] / area_km2).round(1)
    polys = cells_m[["b", "geometry"]].dissolve("b").join(agg).reset_index().to_crs(4326)
    polys["geometry"] = polys.geometry.simplify(0.0002)

    # ---- rail for display
    rail = []
    for fname, status in (("linha_metro", "op"), ("linha_trem", "op")):
        for f in json.load(open(RAW / f"{fname}.json", encoding="utf-8"))["features"]:
            ln = f["properties"]["nm_linha_metro_trem"]
            if ln in LINE_INFO:
                rail.append({"type": "Feature", "geometry": f["geometry"],
                             "properties": {"line": LINE_INFO[ln][0], "color": LINE_INFO[ln][1], "status": status}})
    for f in json.load(open(RAW / "linha_metro_projetada.json", encoding="utf-8"))["features"]:
        if f["properties"]["nm_linha_metro_trem"] == "LARANJA":
            rail.append({"type": "Feature", "geometry": f["geometry"],
                         "properties": {"line": "Linha 6-Laranja", "color": LINE_INFO["LARANJA"][1], "status": "l6"}})
    st_rows = {}
    for fname in ("estacao_metro", "estacao_trem"):
        for f in json.load(open(RAW / f"{fname}.json", encoding="utf-8"))["features"]:
            p = f["properties"]
            ln = p["nm_linha_metro_trem"]
            if ln not in LINE_INFO or p.get("tx_situacao_metro_trem") not in (None, "OPERANDO"):
                continue
            key = p["nm_estacao_metro_trem"].replace(" - ", "-")
            lon, lat = f["geometry"]["coordinates"][:2]
            s = st_rows.setdefault(key, {"name": key.title(), "lon": lon, "lat": lat, "lines": [], "status": "op"})
            s["lines"].append(LINE_INFO[ln][0])
    for f in json.load(open(RAW / "estacao_metro_projetada.json", encoding="utf-8"))["features"]:
        p = f["properties"]
        if p["nm_linha_metro_trem"] != "LARANJA" or p["nm_estacao_metro_trem"] not in LINE6_CORE:
            continue
        key = p["nm_estacao_metro_trem"]
        lon, lat = f["geometry"]["coordinates"][:2]
        s = st_rows.setdefault(key, {"name": key.title(), "lon": lon, "lat": lat, "lines": [], "status": "l6"})
        s["lines"].append("Linha 6-Laranja" + (" (operação assistida 10h-15h)" if key in LINE6_ASSISTED else " (em obras)"))
    stations = [s for s in st_rows.values()
                if STUDY_LL[0] - 0.02 <= s["lon"] <= STUDY_LL[2] + 0.02 and STUDY_LL[1] - 0.02 <= s["lat"] <= STUDY_LL[3] + 0.02]

    poi_out = [{"c": r["cat"], "n": r["name"], "x": round(p.x, 5), "y": round(p.y, 5)}
               for (_, r), p in zip(pois.iterrows(), pois.to_crs(4326).geometry)
               if r["cat"] not in ("park",)]
    feiras = gpd.read_file(RAW / "geosampa/equipamento_feira_livre.geojson")
    poi_out += [{"c": "feira", "n": f"Feira {r.nm_equipamento.title()} ({(r.tx_dia_funcionamento or '').title()})",
                 "x": round(r.geometry.x, 5), "y": round(r.geometry.y, 5)} for _, r in feiras.iterrows()]
    green_out = gpd.GeoDataFrame(geometry=greens, crs=CRS).to_crs(4326)
    green_out["geometry"] = green_out.geometry.simplify(0.00005)
    dist_out = distritos.to_crs(4326)[["nm_distrito_municipal", "geometry"]]
    dist_out = dist_out[dist_out.intersects(box(*STUDY_LL))]
    dist_out["geometry"] = dist_out.geometry.simplify(0.0002)
    big_out = big_roads.to_crs(4326)[["dc_tipo_classificacao_viaria", "geometry"]]
    big_out = big_out[big_out.intersects(box(*STUDY_LL))]
    big_out["geometry"] = big_out.geometry.simplify(0.00005)

    def write(name, obj):
        txt = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        (OUT / f"{name}.js").write_text(f"window.DATA_{name.upper()} = {txt};\n", encoding="utf-8")

    cells["geometry"] = shapely.set_precision(cells.geometry.values, 1e-5)
    write("cells", cells.to_json(drop_id=True))
    write("bairros", polys.to_json(drop_id=True, default=str))
    write("rail", {"type": "FeatureCollection", "features": rail})
    write("stations", stations)
    write("pois", poi_out)
    write("greens", green_out.to_json(drop_id=True))
    write("distritos", dist_out.to_json(drop_id=True))
    write("bigroads", big_out.to_json(drop_id=True))
    write("meta", {"cge": CGE_LL, "built": pd.Timestamp.now().isoformat(timespec="minutes"),
                   "crime_window": "set/2025 a ago/2026", "ndvi_date": "2026-07-19", "osm_date": "2026-10-11"})
    agg.sort_values("ss", ascending=False).to_csv(PROC / "bairros_summary.csv", encoding="utf-8")
    print("done", {p.name: f"{p.stat().st_size / 1e6:.1f} MB" for p in OUT.glob("*.js")})


if __name__ == "__main__":
    main()
