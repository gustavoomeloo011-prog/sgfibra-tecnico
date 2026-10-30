"""Persisted PON street areas built from SGP addresses and OpenStreetMap streets.

No customer names or addresses are sent to the map provider. Only geographic
tiles are downloaded; address/house-number matching happens in this process.
No driving tour or convex hull is interpreted as network coverage.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from shapely import concave_hull
from shapely.geometry import LineString, MultiPoint, Point, Polygon, mapping
from shapely.ops import substring, transform, unary_union
from shapely.strtree import STRtree

from app import operations
from app.sgp import SGPError

RETENTION = 3650 * 86400
SNAPSHOT = "pon-street-map-v1"
TILE = .04


def normalized(value):
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"\b(?:r|rua|av|avenida|travessa|tv|estrada|estr)\b\.?", " ", value)
    return " ".join(re.findall(r"[a-z0-9]+", value))


def address_fields(raw):
    if not isinstance(raw, dict):
        return {"street": "", "number": "", "postcode": "", "text": str(raw or "")}
    return {
        "street": str(raw.get("logradouro") or raw.get("rua") or raw.get("street") or ""),
        "number": str(raw.get("numero") or raw.get("number") or ""),
        "postcode": re.sub(r"\D", "", str(raw.get("cep") or raw.get("postcode") or "")),
        "city": str(raw.get("cidade") or raw.get("municipio") or ""),
        "text": ", ".join(str(raw.get(k) or "") for k in ("logradouro", "numero", "bairro", "cidade", "cep") if raw.get(k)),
    }


def bulk_addresses(client, progress):
    """SGP limits this endpoint to 100 customers; follow its observed offset metadata.

    Persist only contract postal addresses, never the full customer response
    (which can include credentials and financial fields).
    """
    def page(offset):
        key = f"pon-address-page-v1:{offset}"
        saved = operations.cache_snapshot(key, 3600, RETENTION)
        if saved is not None:
            return saved
        for attempt in range(3):
            try:
                response = client.request("POST", "/api/ura/clientes/", form={
                    "limit": 100, "offset": offset, "omitir_titulos": "true", "omitir_contatos": "true"})
                if not isinstance(response, dict) or not isinstance(response.get("clientes"), list):
                    raise SGPError("Resposta inválida na página de endereços.", 502)
                break
            except SGPError as error:
                if error.status not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(attempt + 1)
        addresses = {}
        for customer in response["clientes"]:
            for contract in customer.get("contratos") or []:
                identifier = contract.get("contrato") or contract.get("id")
                fields = address_fields(contract.get("endereco"))
                if identifier and fields.get("street"):
                    addresses[str(identifier)] = fields
        saved = {"addresses": addresses, "total": int(response.get("paginacao", {}).get("total") or 0)}
        operations.save_cache_snapshot(key, saved, RETENTION)
        return saved
    first = page(0)
    addresses = dict(first["addresses"])
    total = first["total"]
    if total <= 0:
        raise SGPError("O SGP não informou a paginação dos endereços.", 502)
    with ThreadPoolExecutor(max_workers=3) as pool:
        for i, batch in enumerate(pool.map(page, range(100, total, 100)), start=2):
            addresses.update(batch["addresses"])
            progress("Carregando endereços dos contratos em lotes", min(i * 100, total), total)
    return addresses


def enrich_addresses(client, rows, progress):
    """Read the exact contract address, saving each lookup for interrupted builds."""
    addresses = bulk_addresses(client, progress)
    for i, row in enumerate(rows):
        fields = address_fields(row.get("address"))
        contract_id = row.get("contract_id")
        if contract_id and not all(fields.get(k) for k in ("street", "number", "postcode")):
            key = f"pon-contract-address-v1:{contract_id}"
            saved = addresses.get(str(contract_id)) or operations.cache_snapshot(key, 7 * 86400, RETENTION)
            if saved is None:
                try:
                    result = client.request("POST", "/api/ura/clientes/", form={
                        "contrato": contract_id, "limit": 5, "omitir_titulos": "true", "omitir_contatos": "true",
                    })
                except (SGPError, OSError, TimeoutError):
                    result = {}
                    row["address_lookup_failed"] = True
                saved = None
                for customer in result.get("clientes", []) if isinstance(result, dict) else []:
                    for contract in customer.get("contratos", []):
                        if str(contract.get("contrato") or contract.get("id")) != str(contract_id):
                            continue
                        # Never substitute a different contract or the billing address.
                        saved = address_fields(contract.get("endereco"))
                if saved and saved.get("street"):
                    operations.save_cache_snapshot(key, saved, RETENTION)
            if saved:
                fields = saved
        row["postal_address"] = fields
        row["address"] = fields.get("text") or str(row.get("address") or "")
        progress("Conferindo CEP e número dos contratos", i + 1, len(rows))


def street_tile(tile):
    key = f"pon-osm-streets-v1:{tile[0]}:{tile[1]}"
    saved = operations.cache_snapshot(key, 30 * 86400, RETENTION)
    if saved is not None:
        return saved["elements"]
    south, west = tile[0] * TILE - .003, tile[1] * TILE - .003
    north, east = (tile[0] + 1) * TILE + .003, (tile[1] + 1) * TILE + .003
    bbox = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    query = f'[out:json][timeout:45];(way["highway"~"^(residential|living_street|unclassified|tertiary|secondary|primary|service|pedestrian)$"]({bbox});nwr["addr:housenumber"]({bbox}););out geom;'
    # Public instance allowing project use; no customer information is sent.
    request = Request("https://overpass.private.coffee/api/interpreter", data=urlencode({"data": query}).encode(),
                      headers={"User-Agent": "EBF-Tecnico-PON/1.0", "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.loads(response.read())
            if payload.get("remark") or not isinstance(payload.get("elements"), list):
                raise ValueError("Resposta cartográfica incompleta.")
            break
        except (OSError, ValueError) as error:
            if attempt == 2:
                raise ValueError("A fonte de ruas está indisponível. Endereços e trechos já consultados foram preservados; tente novamente.") from error
            time.sleep(30 if getattr(error, "code", None) in (429, 406) else 5)
    operations.save_cache_snapshot(key, payload, RETENTION)
    return payload["elements"]


def build_geometry(rows, elements):
    """Select street blocks containing clients, then buffer their actual shape."""
    lat0 = sum(float(r["latitude"]) for r in rows) / max(1, len(rows))
    sx, sy = 111320 * math.cos(math.radians(lat0)), 111320
    project = lambda x, y, z=None: (x * sx, y * sy)
    unproject = lambda x, y, z=None: (x / sx, y / sy)
    ways = [e for e in elements if e.get("tags", {}).get("highway") and len(e.get("geometry", [])) >= 2]
    occurrences = Counter(n for way in ways for n in set(way.get("nodes", [])))
    segments, names = [], []
    for way in ways:
        coords = [(p["lon"] * sx, p["lat"] * sy) for p in way["geometry"]]
        nodes = way.get("nodes", [])
        start = 0
        for j in range(1, len(coords)):
            if j == len(coords) - 1 or (j < len(nodes) and occurrences[nodes[j]] > 1):
                line = LineString(coords[start:j + 1])
                if line.length > 1:
                    segments.append(line)
                    names.append(normalized(way.get("tags", {}).get("name")))
                start = j
    houses = defaultdict(list)
    for element in elements:
        tags = element.get("tags", {})
        if not tags.get("addr:housenumber") or not tags.get("addr:street"):
            continue
        if "lat" in element and "lon" in element:
            position = (float(element["lon"]), float(element["lat"]))
        else:
            geometry = element.get("geometry", [])
            if not geometry:
                continue
            position = (sum(p["lon"] for p in geometry) / len(geometry), sum(p["lat"] for p in geometry) / len(geometry))
        houses[(normalized(tags["addr:street"]), normalized(tags["addr:housenumber"]))].append(
            (position, re.sub(r"\D", "", tags.get("addr:postcode", ""))))
    tree = STRtree(segments) if segments else None
    selected = defaultdict(list)
    for row in rows:
        address = row.get("postal_address", {})
        name = normalized(address.get("street"))
        position = (float(row["longitude"]), float(row["latitude"]))
        candidates = houses.get((name, normalized(address.get("number"))), [])
        candidates = [p for p, cep in candidates if not cep or not address.get("postcode") or cep == address["postcode"]]
        nearby = [p for p in candidates if math.hypot((p[0] - position[0]) * sx, (p[1] - position[1]) * sy) < 2000]
        row["location_method"] = "sgp_unverified"
        if len(nearby) == 1:
            position = nearby[0]
            row["location_method"] = "osm_street_house_number"
        row["located_latitude"], row["located_longitude"] = position[1], position[0]
        row["area_status"] = "pending_address"
        if not name or not address.get("number") or not address.get("postcode") or tree is None:
            continue
        point = Point(project(*position))
        nearby_segments = tree.query(point.buffer(120))
        eligible = [(segments[int(i)].distance(point), int(i)) for i in nearby_segments if names[int(i)] == name]
        if not eligible:
            row["area_status"] = "pending_street_match"
            continue
        distance, index = min(eligible)
        if distance > 100:
            row["area_status"] = "pending_street_match"
            continue
        line = segments[index]
        # A rural/unsplit OSM way must not paint kilometres for one subscriber.
        if line.length > 450:
            at = line.project(point)
            line = substring(line, max(0, at - 80), min(line.length, at + 80))
        key = (str(row.get("olt_id")), row.get("slot"), row.get("pon"))
        if key[1] is None or key[2] is None:
            row["area_status"] = "pending_pon"
            continue
        selected[key].append(line)
        if row["location_method"] == "sgp_unverified":
            row["location_method"] = "sgp_coordinate_street_checked"
        row["area_status"] = "street_matched"
        row["street_distance_m"] = round(distance, 1)
    result = {}
    for key, lines in selected.items():
        area = unary_union([line.buffer(14, cap_style="round", join_style="round") for line in lines]).simplify(1, preserve_topology=True)
        label = area.representative_point()
        result[key] = {"area_geometry": mapping(transform(unproject, area)),
                       "label_point": [label.y / sy, label.x / sx], "street_segments": len(lines)}
    return result


def suggested_manual_areas(data):
    """Build editable PON drafts from the dominant nearby customer cluster.

    This intentionally rejects distant islands before drawing the boundary. It
    is a database-backed fallback for supervisors when the street provider is
    unavailable, not a claim of technical coverage.
    """
    rows_by_port = defaultdict(list)
    for row in data.get("clients", []):
        if row.get("slot") is None or row.get("pon") is None:
            continue
        try:
            latitude = float(row.get("located_latitude", row.get("latitude")))
            longitude = float(row.get("located_longitude", row.get("longitude")))
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        rows_by_port[(str(row.get("olt_id") or ""), str(row["slot"]), str(row["pon"]))].append((longitude, latitude, row))
    groups = {(str(item.get("olt_id") or ""), str(item.get("slot")), str(item.get("pon"))): item for item in data.get("groups", [])}
    result = []
    for key, points in rows_by_port.items():
        # A malformed coordinate must not stop all the other PONs.  The batch
        # endpoint reports any skipped item instead of presenting a 500 error.
        try:
            if not points:
                continue
            lat0 = sum(point[1] for point in points) / len(points)
            sx, sy = 111320 * math.cos(math.radians(lat0)), 111320
            projected = [(point[0] * sx, point[1] * sy) for point in points]
            nearest = []
            for index, point in enumerate(projected):
                distances = [math.dist(point, other) for j, other in enumerate(projected) if j != index]
                if distances:
                    nearest.append(min(distances))
            nearest.sort()
            median = nearest[len(nearest) // 2] if nearest else 120
            radius = max(220, min(850, median * 3.5))
            remaining, components = set(range(len(points))), []
            while remaining:
                seed = remaining.pop()
                component, queue = {seed}, [seed]
                while queue:
                    current = queue.pop()
                    neighbors = [candidate for candidate in list(remaining) if math.dist(projected[current], projected[candidate]) <= radius]
                    for candidate in neighbors:
                        remaining.remove(candidate); component.add(candidate); queue.append(candidate)
                components.append(component)
            dominant = max(components, key=lambda component: (len(component), -sum(math.dist(projected[i], projected[next(iter(component))]) for i in component)))
            core = [projected[index] for index in dominant]
            if len(core) == 1:
                area = Point(core[0]).buffer(85)
            elif len(core) == 2:
                area = LineString(core).buffer(70, cap_style="round", join_style="round")
            else:
                area = concave_hull(MultiPoint(core), ratio=.28, allow_holes=False).buffer(45, join_style="round")
            # Normalise a rare multi-geometry result to the largest usable
            # polygon: the persisted editor deliberately accepts Polygon only.
            if area.geom_type == "MultiPolygon":
                area = max(area.geoms, key=lambda geometry: geometry.area)
            if not isinstance(area, Polygon):
                continue
            area = area.simplify(5, preserve_topology=True)
            if area.is_empty or not area.is_valid:
                continue
            unproject = lambda x, y, z=None: (x / sx, y / sy)
            group = groups.get(key, {})
            result.append({
                "olt_id": key[0], "olt_name": group.get("olt") or key[0], "slot": key[1], "pon": key[2],
                "note": f"Slot {key[1]} · PON {key[2]}", "geometry": mapping(transform(unproject, area)),
                "clients_total": len(points), "clients_used": len(core), "outliers_ignored": len(points) - len(core),
            })
        except (TypeError, ValueError, ArithmeticError):
            continue
    return result


def generate(client, progress):
    progress("Consultando clientes e PONs no SGP", 0, 0)
    data = client.service_coverage_map(complete_pons=True)
    rows = data.get("clients", [])
    enrich_addresses(client, rows, progress)
    # Persist the complete SGP inventory before the optional/heavy street stage.
    # This keeps every discovered OLT/Slot/PON available for filters and optical
    # analysis even when the public cartographic provider is temporarily down.
    data["geometry_version"] = "street-blocks-pending"
    data["address_summary"] = dict(Counter(row.get("area_status", "pending_street_match") for row in rows))
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    progress("Salvando inventário de OLTs, Slots e PONs", len(rows), len(rows))
    operations.save_cache_snapshot(SNAPSHOT, data, RETENTION)
    tiles = sorted({(math.floor(float(r["latitude"]) / TILE), math.floor(float(r["longitude"]) / TILE)) for r in rows})
    elements = {}
    for i, tile in enumerate(tiles):
        progress("Lendo ruas e números de imóveis", i, len(tiles))
        for item in street_tile(tile):
            elements[(item.get("type"), item.get("id"))] = item
        progress("Lendo ruas e números de imóveis", i + 1, len(tiles))
    progress("Preenchendo trechos de rua por PON", 0, len(data.get("groups", [])))
    areas = build_geometry(rows, list(elements.values()))
    for group in data.get("groups", []):
        group.update(areas.get((str(group["olt_id"]), group["slot"], group["pon"]), {}))
    data["geometry_version"] = "street-blocks-v1"
    data["address_summary"] = dict(Counter(row["area_status"] for row in rows))
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    progress("Salvando áreas e localizações", len(rows), len(rows))
    # Saving is mandatory: a failed database write must not report success.
    operations.save_cache_snapshot(SNAPSHOT, data, RETENTION)
    return data
