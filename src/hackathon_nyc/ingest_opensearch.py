"""Index NYC Open Data into OpenSearch with NVIDIA NIM embeddings.

Replaces the ChromaDB path in ingest.py. Two things change beyond the backend:

1. One document per record, not five records mashed into a text blob. The old
   chunking forced historical_lookup.py to regex lat/lon back out of
   concatenated text; here coordinates are real indexed fields.
2. Embeddings come from nv-embedqa-e5-v5 (1024 dims) rather than the MiniLM
   ONNX model ChromaDB downloads locally.

Usage:
    export NVIDIA_API_KEY=...  OPENSEARCH_URL=...
    python -m hackathon_nyc.ingest_opensearch --all
    python -m hackathon_nyc.ingest_opensearch --datasets 311_current --limit 300
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("GRIDWATCH_DATA_DIR", Path(__file__).parent.parent.parent / "data"))

EMBED_MODEL = os.getenv("NVIDIA_EMBED_MODEL", "nvidia/nv-embedqa-e5-v5")
EMBED_DIM = 1024
EMBED_URL = "https://integrate.api.nvidia.com/v1/embeddings"
INDEX_PREFIX = os.getenv("OPENSEARCH_INDEX_PREFIX", "nyc_")

# Local JSON files already in the repo, mapped to index suffix.
LOCAL_DATASETS = {
    "311_current": "311_current.json",
    "flood_events": "flood_events.json",
    "collisions": "collisions.json",
    "potholes": "potholes.json",
    "rodent_inspections": "rodent_inspections.json",
    "housing_violations": "housing_violations.json",
}

# Per-dataset coordinate field names. NYC Open Data is not consistent.
LAT_FIELDS = ("latitude", "lat", "y_coord", "start_lat")
LON_FIELDS = ("longitude", "lon", "lng", "x_coord", "start_lon")


def _client():
    from opensearchpy import OpenSearch
    uri = os.getenv("OPENSEARCH_URL", "")
    if not uri:
        raise SystemExit("Set OPENSEARCH_URL (Aiven service URI) before ingesting.")
    kwargs = {"hosts": [uri], "verify_certs": os.getenv("OPENSEARCH_VERIFY_CERTS", "true") != "false"}
    user, password = os.getenv("OPENSEARCH_USER", ""), os.getenv("OPENSEARCH_PASSWORD", "")
    if user and password and "@" not in uri.split("//", 1)[-1]:
        kwargs["http_auth"] = (user, password)
    return OpenSearch(**kwargs)


def _mapping() -> dict:
    """Index mapping: kNN vector plus the fields the dashboard and agent need."""
    return {
        "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "text": {"type": "text"},
                "vector": {
                    "type": "knn_vector",
                    "dimension": EMBED_DIM,
                    "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"},
                },
                "dataset": {"type": "keyword"},
                "lat": {"type": "float"},
                "lon": {"type": "float"},
                "label": {"type": "text"},
                "date": {"type": "keyword"},
            }
        },
    }


def _first(record: dict, names: tuple) -> float | None:
    for n in names:
        v = record.get(n)
        if v not in (None, "", "NaN"):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _coords(record: dict) -> tuple[float | None, float | None]:
    lat, lon = _first(record, LAT_FIELDS), _first(record, LON_FIELDS)
    if lat is None or lon is None:
        # GeoJSON-ish nested shapes: {"the_geom": {"coordinates": [lon, lat]}}
        for key in ("the_geom", "location", "geom"):
            geo = record.get(key)
            if isinstance(geo, dict):
                coords = geo.get("coordinates")
                if isinstance(coords, list) and len(coords) >= 2:
                    try:
                        return float(coords[1]), float(coords[0])
                    except (TypeError, ValueError):
                        pass
    # Keep only NYC-plausible coordinates; bad rows otherwise land in the ocean.
    if lat is not None and lon is not None and 40.4 <= lat <= 41.0 and -74.3 <= lon <= -73.6:
        return lat, lon
    return None, None


# What a person would actually search for, in the order it matters. Anything
# listed here is emitted first so it survives truncation and dominates the
# embedding.
PRIORITY_FIELDS = (
    "complaint_type", "descriptor", "sensor_name", "violation_description",
    "result", "inspection_type", "incident_address", "street_name", "location_type",
    "cross_street_1", "cross_street_2", "borough", "city", "incident_zip",
    "max_depth_inches", "duration_mins", "agency_name", "status",
    "number_of_persons_injured", "number_of_persons_killed", "contributing_factor_vehicle_1",
)

# Identifiers, bookkeeping and geometry. These carry no search signal but, left
# in, they lead the text and swamp the embedding — the first ingest put
# "unique_key: 68574165 | created_date: ..." ahead of the complaint type.
NOISE_FIELDS = {
    "unique_key", "bbl", "bin", "x_coordinate_state_plane", "y_coordinate_state_plane",
    "community_board", "council_district", "police_precinct", "facility_type",
    "park_facility_name", "park_borough", "taxi_company_borough", "vehicle_type",
    "due_date", "resolution_action_updated_date", "closed_date", "defnum",
    "violationid", "the_geom", "location", "geom", "buildingid", "registrationid",
    "flood_profile_depth_inches", "flood_profile_time_secs", "collision_id",
}


def _to_text(record: dict) -> str:
    """Readable one-line rendering. This is what gets embedded and BM25-indexed."""
    def usable(k, v):
        return (v not in (None, "", [], {}, "N/A", "Unspecified")
                and k not in NOISE_FIELDS
                and not isinstance(v, (dict, list)))

    seen, parts = set(), []
    for k in PRIORITY_FIELDS:
        if k in record and usable(k, record[k]):
            parts.append(f"{k}: {record[k]}")
            seen.add(k)
    for k, v in record.items():
        if k not in seen and usable(k, v):
            parts.append(f"{k}: {v}")
    return " | ".join(parts[:20])


def _date_of(record: dict) -> str:
    for k in ("created_date", "crash_date", "inspection_date", "inspectiondate",
              "flood_start_time", "rptdate", "approved_date"):
        if record.get(k):
            return str(record[k])[:10]
    return ""


async def _embed_batch(texts: list[str], api_key: str, input_type: str = "passage") -> list[list[float]]:
    import aiohttp
    payload = {"input": texts, "model": EMBED_MODEL, "input_type": input_type,
               "truncate": "END"}
    async with aiohttp.ClientSession() as s:
        async with s.post(EMBED_URL, json=payload,
                          headers={"Authorization": f"Bearer {api_key}"},
                          timeout=aiohttp.ClientTimeout(total=180)) as r:
            if r.status != 200:
                raise RuntimeError(f"Embedding failed [{r.status}]: {(await r.text())[:300]}")
            data = await r.json()
    # The API may return items out of order; sort by index to be safe.
    return [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]


async def ingest_dataset(client, key: str, records: list[dict], api_key: str,
                         batch_size: int = 32, recreate: bool = False) -> int:
    from opensearchpy.helpers import bulk

    index = f"{INDEX_PREFIX}{key}"
    if recreate and client.indices.exists(index=index):
        client.indices.delete(index=index)
        print(f"  dropped existing index {index}")
    if not client.indices.exists(index=index):
        client.indices.create(index=index, body=_mapping())
        print(f"  created index {index}")

    texts, metas = [], []
    for i, rec in enumerate(records):
        text = _to_text(rec)
        if len(text) < 20:
            continue
        lat, lon = _coords(rec)
        texts.append(text)
        metas.append({"_id": f"{key}_{i}", "dataset": key, "lat": lat, "lon": lon,
                      "label": text[:140], "date": _date_of(rec)})

    indexed = 0
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        vectors = await _embed_batch(chunk, api_key)
        actions = []
        for text, meta, vec in zip(chunk, metas[start:start + batch_size], vectors):
            doc = {k: v for k, v in meta.items() if k != "_id" and v is not None}
            doc["text"] = text
            doc["vector"] = vec
            actions.append({"_index": index, "_id": meta["_id"], "_source": doc})
        ok, _ = bulk(client, actions, refresh=False)
        indexed += ok
        print(f"    {indexed}/{len(texts)}", end="\r", flush=True)

    client.indices.refresh(index=index)
    print(f"  indexed {indexed} documents into {index}          ")
    return indexed


async def main():
    p = argparse.ArgumentParser(description="Index NYC Open Data into OpenSearch")
    p.add_argument("--datasets", nargs="+", default=[])
    p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=300, help="Max records per dataset")
    p.add_argument("--recreate", action="store_true", help="Drop and rebuild each index")
    args = p.parse_args()

    api_key = os.getenv("NVIDIA_API_KEY", "")
    if not api_key:
        raise SystemExit("Set NVIDIA_API_KEY — embeddings come from the NIM endpoint.")

    keys = list(LOCAL_DATASETS) if args.all else args.datasets
    if not keys:
        raise SystemExit(f"Nothing to do. Use --all or --datasets {' '.join(LOCAL_DATASETS)}")

    client = _client()
    print(f"OpenSearch: {client.info()['version']['number']}  |  embedder: {EMBED_MODEL}")

    total = 0
    for key in keys:
        path = DATA_DIR / LOCAL_DATASETS.get(key, f"{key}.json")
        if not path.exists():
            print(f"[{key}] SKIP — {path} not found")
            continue
        records = json.loads(path.read_text())[:args.limit]
        print(f"[{key}] {len(records)} records")
        total += await ingest_dataset(client, key, records, api_key, recreate=args.recreate)

    print(f"\nDone. {total} documents indexed.")


if __name__ == "__main__":
    asyncio.run(main())
