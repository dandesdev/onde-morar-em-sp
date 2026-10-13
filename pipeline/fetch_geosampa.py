"""Download GeoSampa (Prefeitura de São Paulo) WFS layers as GeoJSON (EPSG:4326).

Usage: python -P pipeline/fetch_geosampa.py <work>/raw [lon_min,lat_min,lon_max,lat_max]

City layers inside the bbox go to <raw>/geosampa/<layer>.geojson; districts and rail (whole layers)
go to <raw>/<name>.json. Every file is replaced only after a complete download.
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

WFS = "https://wms.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/wfs"
BBOX = "-24.02,-46.84,-23.34,-46.35,urn:ogc:def:crs:EPSG::4326"  # WFS 2.0 EPSG:4326 is lat,lon
PAGE = 10000  # server caps responses at 10240 features

# layer -> (sort key for paging, properties to keep; None keeps all)
LAYERS = {
    "arvore": ("cd_identificador", ["cd_identificador"]),
    "iluminacao_publica": ("cd_identificador_iluminacao_publica", ["cd_identificador_iluminacao_publica", "dc_tipo_iluminacao_publica"]),
    "classificacao_viaria_cet": ("cd_identificador", ["cd_identificador", "dc_tipo_classificacao_viaria", "nm_logradouro_completo"]),
    "equipamento_feira_livre": ("cd_identificador", ["cd_identificador", "nm_equipamento", "tx_endereco_equipamento", "tx_dia_funcionamento"]),
    "pde_parque_municipal": ("cd_identificador", ["cd_identificador", "nm_parque", "tx_tipo_categoria_parque_unidade_conservacao", "tx_tipo_situacao_projeto"]),
    "GEOSAMPA_v_praca_largo": ("cd_identificador", ["cd_identificador", "categoria", "nome", "area_metro", "bairro"]),
    "linha_onibus": ("cd_identificador", ["cd_identificador", "cd_linha_geometria", "nm_linha_onibus_geometria", "sentido_linha_onibus", "ho_primeira_viagem", "ho_ultima_viagem"]),
    "corredor_onibus": (None, None),
}
# whole layers (no bbox), small
BASE = {
    "distrito_municipal": "distritos.json",
    "estacao_metro": "estacao_metro.json", "linha_metro": "linha_metro.json",
    "estacao_metro_projetada": "estacao_metro_projetada.json", "linha_metro_projetada": "linha_metro_projetada.json",
    "estacao_trem": "estacao_trem.json", "linha_trem": "linha_trem.json",
}
MIN_FEATURES = {"arvore": 100000, "iluminacao_publica": 100000, "classificacao_viaria_cet": 50000, "linha_onibus": 500,
                "distrito_municipal": 96}


def get(params):
    for attempt in range(5):
        try:
            r = requests.get(WFS, params=params, timeout=600)
            r.raise_for_status()
            return r.json()["features"]
        except Exception as e:  # network hiccups on large pages
            print(f"  retry {attempt + 1} {params.get('typeNames')}@{params.get('startIndex', 0)}: {e}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"failed {params.get('typeNames')}")


def fetch(layer, sort_key, props, bbox=True):
    feats, start = [], 0
    while True:
        params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": f"geoportal:{layer}",
                  "outputFormat": "application/json", "srsName": "EPSG:4326", "count": PAGE, "startIndex": start}
        if bbox:
            params["bbox"] = BBOX
        if sort_key:
            params["sortBy"] = sort_key
        page = get(params)
        if props is not None:
            for f in page:
                f["properties"] = {k: f["properties"].get(k) for k in props}
        feats.extend(page)
        if len(page) < PAGE:
            break
        start += PAGE
    if len(feats) < MIN_FEATURES.get(layer, 1):
        raise RuntimeError(f"{layer}: only {len(feats)} features, refusing to replace the previous file")
    print(f"  {layer}: {len(feats)}", flush=True)
    return feats


def save(path: Path, feats):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    os.replace(tmp, path)


def main(raw: Path):
    (raw / "geosampa").mkdir(parents=True, exist_ok=True)
    for layer, name in BASE.items():
        save(raw / name, fetch(layer, None, None, bbox=False))
    for layer, (sort_key, props) in LAYERS.items():
        save(raw / "geosampa" / f"{layer}.geojson", fetch(layer, sort_key, props))
    print("geosampa ok", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 2:
        x0, y0, x1, y1 = sys.argv[2].split(",")
        BBOX = f"{y0},{x0},{y1},{x1},urn:ogc:def:crs:EPSG::4326"
    main(Path(sys.argv[1]))
