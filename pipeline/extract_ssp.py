"""Extract SSP-SP police reports (BOs) inside the study area into one CSV.

Usage: python -P pipeline/extract_ssp.py <ssp xlsx dir> <work>/processed/ssp_study.csv [lon_min,lat_min,lon_max,lat_max]
"""
import sys
import os
import time
import unicodedata
from pathlib import Path

import pandas as pd
from python_calamine import CalamineWorkbook

LAT_MIN, LAT_MAX = -24.02, -23.34
LON_MIN, LON_MAX = -46.84, -46.35

KEEP = [
    "NOME_DELEGACIA_CIRCUNSCRICAO", "NUM_BO", "ANO_BO", "DATA_OCORRENCIA_BO",
    "HORA_OCORRENCIA_BO", "DESC_PERIODO", "DESCR_TIPOLOCAL", "DESCR_SUBTIPOLOCAL",
    "BAIRRO", "LOGRADOURO", "LATITUDE", "LONGITUDE", "RUBRICA", "DESCR_CONDUTA",
    "NATUREZA_APURADA", "MES_ESTATISTICA", "ANO_ESTATISTICA",
]


def normalize(col: str) -> str:
    """Column names drift between yearly files (accents, typos, DESC/DESCR)."""
    c = unicodedata.normalize("NFKD", str(col)).encode("ascii", "ignore").decode().upper().strip()
    c = c.replace("CIRCUNCRICAO", "CIRCUNSCRICAO")
    return {"DESCR_PERIODO": "DESC_PERIODO"}.get(c, c)


def main(src_dir: Path, out: Path):
    frames = []
    for f in sorted(src_dir.glob("SPDadosCriminais_*.xlsx")):
        wb = CalamineWorkbook.from_path(str(f))
        for name in wb.sheet_names:
            if name.upper().startswith("CAMPOS"):
                continue
            t = time.time()
            rows = wb.get_sheet_by_name(name).to_python()
            df = pd.DataFrame(rows[1:], columns=[normalize(c) for c in rows[0]])
            del rows
            mun_col = next(c for c in ["NOME_MUNICIPIO_CIRCUNSCRICAO", "NOME_MUNICIPIO", "CIDADE"] if c in df.columns)
            df = df[df[mun_col].astype(str).str.upper().isin(["S.PAULO", "SAO PAULO", "SÃO PAULO"])]
            lat = pd.to_numeric(df["LATITUDE"], errors="coerce")
            lon = pd.to_numeric(df["LONGITUDE"], errors="coerce")
            df = df[lat.between(LAT_MIN, LAT_MAX) & lon.between(LON_MIN, LON_MAX)]
            df = df[[c for c in KEEP if c in df.columns]].copy()
            df["SHEET"] = f"{f.stem}:{name}"
            frames.append(df)
            missing = [c for c in KEEP if c not in df.columns]
            print(f"{f.name}:{name} kept {len(df)} rows in {time.time() - t:.0f}s missing={missing}", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    all_df = pd.concat(frames, ignore_index=True)
    if len(all_df) < 500_000:
        raise SystemExit(f"only {len(all_df)} records; refusing to replace {out}")
    tmp = out.with_suffix(".csv.tmp")
    all_df.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, out)
    print("total", len(all_df))


if __name__ == "__main__":
    if len(sys.argv) > 3:
        LON_MIN, LAT_MIN, LON_MAX, LAT_MAX = map(float, sys.argv[3].split(","))
    main(Path(sys.argv[1]), Path(sys.argv[2]))
