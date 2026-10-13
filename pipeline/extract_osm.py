"""Pull daily-errand POIs and neighbourhood (bairro) features for the study area from an OSM .pbf.

Usage: python -P pipeline/extract_osm.py <sudeste.osm.pbf> <work>/processed [lon_min,lat_min,lon_max,lat_max]
Writes osm_pois.geojson, osm_places.geojson and osm.json (data timestamp of the extract).
"""
import json
import os
import sys
from pathlib import Path

import osmium

LON_MIN, LAT_MIN, LON_MAX, LAT_MAX = -46.84, -24.02, -46.35, -23.34

CATEGORIES = {
    ("shop", "supermarket"): "supermarket",
    ("shop", "convenience"): "minimarket",
    ("shop", "greengrocer"): "hortifruti",
    ("shop", "butcher"): "butcher",
    ("shop", "deli"): "minimarket",
    ("shop", "bakery"): "bakery",
    ("amenity", "pharmacy"): "pharmacy",
    ("healthcare", "pharmacy"): "pharmacy",
    ("shop", "chemist"): "pharmacy",
    ("amenity", "veterinary"): "vet",
    ("shop", "pet"): "petshop",
    ("amenity", "marketplace"): "market",
    ("leisure", "park"): "park",
    ("leisure", "dog_park"): "dog_park",
}
PLACE_TYPES = {"suburb", "neighbourhood", "quarter"}


def in_bbox(lon, lat):
    return LON_MIN <= lon <= LON_MAX and LAT_MIN <= lat <= LAT_MAX


def category(tags):
    for (k, v), cat in CATEGORIES.items():
        if tags.get(k) == v:
            return cat
    # Brazilian padarias are often tagged as cafes with a name hint
    name = (tags.get("name") or "").lower()
    if tags.get("amenity") == "cafe" and ("padaria" in name or "panific" in name):
        return "bakery"
    return None


def main(pbf, out_dir: Path):
    wkt = osmium.geom.GeoJSONFactory()
    idx = osmium.index.create_map(f"sparse_file_array,{Path(pbf).with_suffix('.nodes.idx')}")
    fp = (osmium.FileProcessor(pbf)
          .with_locations(idx)
          .with_areas()
          .with_filter(osmium.filter.KeyFilter("shop", "amenity", "leisure", "healthcare", "place", "boundary")))
    pois, places = [], []
    for obj in fp:
        tags = dict(obj.tags)
        cat = category(tags)
        place = tags.get("place") in PLACE_TYPES
        boundary = tags.get("boundary") == "administrative" and tags.get("admin_level") in {"9", "10", "11"}
        if not (cat or place or boundary):
            continue
        if obj.is_node():
            if not obj.location.valid():
                continue
            lon, lat = obj.location.lon, obj.location.lat
            geom = {"type": "Point", "coordinates": [lon, lat]}
        elif obj.is_area():
            try:
                geom = json.loads(wkt.create_multipolygon(obj))
            except Exception:
                continue
            ring = geom["coordinates"][0][0]
            lon = sum(p[0] for p in ring) / len(ring)
            lat = sum(p[1] for p in ring) / len(ring)
        else:
            continue  # plain ways/relations: their area version is handled above
        if not in_bbox(lon, lat):
            continue
        props = {"name": tags.get("name"), "osm": f"{'n' if obj.is_node() else 'a'}{obj.id}"}
        if cat:
            # polygons (parks, big supermarkets) also get a representative point
            props["cat"] = cat
            feat = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}
            if cat in ("park", "dog_park") and geom["type"] != "Point":
                feat["properties"]["area_geom"] = geom
            pois.append(feat)
        if place or boundary:
            props = dict(props, place=tags.get("place"), admin_level=tags.get("admin_level"))
            places.append({"type": "Feature", "geometry": geom, "properties": props})
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(pois) < 5000 or len(places) < 500:
        raise SystemExit(f"only {len(pois)} POIs / {len(places)} places; refusing to replace the previous files")
    for name, feats in (("osm_pois", pois), ("osm_places", places)):
        tmp = out_dir / f"{name}.geojson.tmp"
        tmp.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out_dir / f"{name}.geojson")
        print(name, len(feats))
    stamp = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING).header().get("osmosis_replication_timestamp", "")
    (out_dir / "osm.json").write_text(json.dumps({"date": stamp[:10] or None}), encoding="utf-8")
    print("osm data timestamp", stamp)


if __name__ == "__main__":
    if len(sys.argv) > 3:
        LON_MIN, LAT_MIN, LON_MAX, LAT_MAX = map(float, sys.argv[3].split(","))
    main(sys.argv[1], Path(sys.argv[2]))
