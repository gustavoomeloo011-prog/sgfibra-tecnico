from __future__ import annotations

import base64
import html
import http.cookiejar
import json
import logging
import math
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ORDER_COORDINATE_CACHE: dict[int, tuple[float, tuple[Any, Any]]] = {}
ORDER_COORDINATE_LOCK = threading.Lock()
ORDER_COORDINATE_TTL = 60 * 60
SGP_MAX_CONCURRENT_REQUESTS = max(4, int(os.getenv("SGP_MAX_CONCURRENT_REQUESTS", "24")))
SGP_REQUEST_GATE = threading.BoundedSemaphore(SGP_MAX_CONCURRENT_REQUESTS)
ADMIN_ENDPOINTS_ONLY = os.getenv("SGP_ADMIN_ENDPOINTS_ONLY", "0") != "0"

ROUTE_MONITOR_TECHNICIANS = (
    "Arthur", "Anderson Crepaldi", "Anderson Neis", "Bruno", "Clayton", "DavidR",
    "Eduardo", "Evertton", "Kaique", "Leonardo", "Paulo", "Silvando",
)

WITHDRAWAL_TECHNICIANS = {
    "Alex": 90, "Arthur": 65, "Anderson Crepaldi": 13, "Anderson Neis": 77,
    "Bruno": 11, "Clayton": 75, "Eduardo": 81, "Evertton": 73,
    "Kaique": 30, "Leonardo": 68, "Paulo": 57, "Silvando": 36,
}


def _classify_launched_modem(
    serial_suffix: str, events: list[dict[str, Any]], launched_at: datetime | None = None,
) -> dict[str, Any]:
    """Compare one stock launch with the latest ONU link state in client history."""
    suffix = re.sub(r"[^A-Z0-9]", "", str(serial_suffix or "").upper())[-6:]
    ordered = sorted(
        (event for event in events if event.get("at") and (
            launched_at is None or event["at"].date() <= launched_at.date()
        )),
        key=lambda event: event["at"], reverse=True,
    )
    target = [event for event in ordered if str(event.get("serial") or "").upper().endswith(suffix)]
    target_links = [event for event in target if event.get("action") == "link"]
    latest_link = next((event for event in ordered if event.get("action") == "link"), None)
    if launched_at is not None:
        target_links = [event for event in target_links if event["at"].date() == launched_at.date()]
    if not target_links:
        return {"status": "not_confirmed", "label": "Não confirmado", "latest": latest_link}
    latest_target = target[0]
    if latest_target.get("action") == "unlink":
        return {"status": "unlinked", "label": "Desvinculado", "latest": latest_target}
    if latest_link and not str(latest_link.get("serial") or "").upper().endswith(suffix):
        return {"status": "divergent", "label": "Divergente", "latest": latest_link}
    return {"status": "correct", "label": "Correto", "latest": target_links[0]}


def _signal_dbm(value: Any) -> float | None:
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value or ""))
    if not match:
        return None
    number = float(match.group(0).replace(",", "."))
    return number if -50 < number < 5 else None


def _optical_label_key(value: Any) -> str:
    return "".join(char for char in _normalized(value) if char.isalnum())


def _optical_street_key(value: Any) -> str:
    normalized = _normalized(value)
    if not normalized:
        return ""
    # Addresses in the report usually start with the street and then the
    # number.  Stop at the first number so different houses group together.
    street = re.split(r"\b\d+[a-z]?\b|[,;]", normalized, maxsplit=1)[0]
    words = [word for word in street.split() if len(word) > 1]
    return " ".join(words[:7])


def _optical_street_display(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "Rua não identificada"
    street = re.split(r"\b\d+[A-Za-z]?\b|[,;]", text, maxsplit=1)[0].strip(" ,-;")
    return street or "Rua não identificada"


def _optical_street_search(value: Any) -> str:
    ignored = {"rua", "avenida", "av", "travessa", "estrada", "alameda", "praca", "praça", "do", "da", "dos", "das", "de"}
    words = [word for word in _optical_street_key(value).split() if word not in ignored and len(word) >= 3]
    return words[0] if words else ""


def _last_unlinked_network(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest ONU unlink event that contains a physical slot/PON."""
    network_pattern = re.compile(r"slot\s*[:=]?\s*(\d+).*?pon\s*[:=]?\s*(\d+)", re.I)
    serial_pattern = re.compile(r"\b(?:[A-F0-9]{16}|[A-Z]{4}[A-Z0-9]{8,28})\b", re.I)
    for entry in history:
        text = str(entry.get("text") or "")
        normalized = _normalized(text)
        if "desvinculad" not in normalized or "onu" not in normalized:
            continue
        match = network_pattern.search(text)
        if not match:
            continue
        serial = serial_pattern.search(text)
        return {
            "slot": int(match.group(1)), "pon": int(match.group(2)),
            "serial": serial.group(0).upper() if serial else None,
            "date": entry.get("date"),
        }
    return None


def _parse_optical_signal_table(page: str) -> list[dict[str, Any]]:
    """Parse the SGP optical-list table without depending on column order."""
    tables = re.findall(r"<table\b[^>]*>(.*?)</table>", page, re.I | re.S)
    results: list[dict[str, Any]] = []

    def text(cell: str) -> str:
        cell = re.sub(r"<(?:script|style)\b.*?</(?:script|style)>", " ", cell, flags=re.I | re.S)
        visible = html.unescape(re.sub(r"<[^>]+>", " ", cell))
        attributes = re.findall(r"(?:title|data-[\w-]+|value)=[\"']([^\"']+)[\"']", cell, re.I)
        return " ".join(" ".join([visible, *map(html.unescape, attributes)]).split())

    def find(row: dict[str, str], *terms: str) -> str:
        for term in terms:
            for key, value in row.items():
                if term in _normalized(key):
                    return value
        return ""

    def exact(row: dict[str, str], *terms: str) -> str:
        for term in terms:
            wanted = _normalized(term)
            for key, value in row.items():
                if _normalized(key) == wanted:
                    return value
        return ""

    for table in tables:
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.I | re.S)
        if not rows:
            continue
        header_index = next((
            index for index, row in enumerate(rows)
            if re.search(r"<th\b", row, re.I)
        ), None)
        if header_index is None:
            continue
        headers = [text(cell) for cell in re.findall(r"<th\b[^>]*>(.*?)</th>", rows[header_index], re.I | re.S)]
        if not headers:
            continue
        normalized_headers = [_normalized(value) for value in headers]
        if not any("sinal" in value or re.search(r"\b(?:tx|rx)\b", value) for value in normalized_headers):
            continue
        for raw_row in rows[header_index + 1:]:
            cells = [text(cell) for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", raw_row, re.I | re.S)]
            if not cells:
                continue
            row = {headers[index]: value for index, value in enumerate(cells[:len(headers)])}
            # In the SGP report, "Rx" is what reaches the modem and "Rx Olt"
            # is the customer's return reaching the OLT.  The EBF calls the
            # latter TX in its field diagnosis.  Fall back to the literal TX
            # column for older SGP table layouts.
            rx = _signal_dbm(exact(row, "rx", "sinal rx", "sinal modem") or find(row, "rx"))
            tx = _signal_dbm(exact(row, "rx olt", "sinal rx olt", "retorno", "tx") or find(row, "tx"))
            contract_text = find(row, "contrato")
            contract_match = re.search(r"\d+", contract_text)
            if tx is None and rx is None and not contract_match and not exact(row, "id", "gpon", "serial", "phy addr", "mac"):
                continue
            network = exact(row, "slot/pon/onu", "slot / pon / onu")
            network_parts = re.findall(r"\d+", network)
            slot_value = exact(row, "slot") or (network_parts[0] if len(network_parts) >= 1 else "")
            pon_value = exact(row, "pon") or (network_parts[1] if len(network_parts) >= 2 else "")
            results.append({
                "contract_id": int(contract_match.group()) if contract_match else None,
                "client": find(row, "cliente", "assinante"),
                "serial": exact(row, "id", "gpon", "serial", "phy addr", "phy address", "mac") or find(row, "gpon", "serial"),
                "label": exact(row, "cto", "etiqueta", "identificacao", "identificação"),
                "address": exact(row, "endereco", "endereço"),
                "description": exact(row, "descricao", "descrição") or exact(row, "endereco", "endereço"),
                "vlan": exact(row, "vlan", "vlan id"),
                "slot": slot_value, "pon": pon_value,
                "tx_dbm": tx, "rx_dbm": rx,
                "collected_at": exact(row, "data coleta", "coletado em"),
                "online": "offline" not in _normalized(" ".join(cells)),
            })
    return results


def _parse_admin_stock_table(page: str, location_name: str = "") -> list[dict[str, Any]]:
    """Parse the one-page aggregated stock report using its visible headers."""
    needle = _normalized(location_name)
    results: list[dict[str, Any]] = []

    def clean(cell: str) -> str:
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())

    for table in re.findall(r"<table\b[^>]*>(.*?)</table>", page, re.I | re.S):
        raw_rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.I | re.S)
        if not raw_rows:
            continue
        headers = [clean(cell) for cell in re.findall(r"<th\b[^>]*>(.*?)</th>", raw_rows[0], re.I | re.S)]
        normalized = [_normalized(value) for value in headers]
        if not any("disponivel" in value or "estoque atual" in value for value in normalized):
            continue
        for raw_row in raw_rows[1:]:
            cells_html = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", raw_row, re.I | re.S)
            cells = [clean(cell) for cell in cells_html]
            if not cells:
                continue
            row = {headers[index]: value for index, value in enumerate(cells[:len(headers)])}

            def find(*terms: str) -> str:
                for term in terms:
                    for key, value in row.items():
                        if term in _normalized(key):
                            return value
                return ""

            location = find("estoque", "local")
            if needle and location and needle not in _normalized(location):
                continue
            # Stock quantities can be much larger than optical readings.
            quantity_match = re.search(r"-?\d+(?:[.,]\d+)?", find("disponivel", "estoque atual"))
            quantity = float(quantity_match.group(0).replace(",", ".")) if quantity_match else None
            if quantity is None or quantity <= 0:
                continue
            product = find("produto", "material", "descricao")
            product_match = re.search(r"/(?:produto|produto_estoque)/(\d+)/", raw_row, re.I)
            reference_html = cells_html[normalized.index("referencias")] if "referencias" in normalized and len(cells_html) > normalized.index("referencias") else ""
            references = [
                {"referencia": value.upper(), "id": int(reference_id)}
                for value, reference_id in re.findall(
                    r"([A-Z0-9-]{8,32})\s*&nbsp;\s*<a\b[^>]*?produto_referencia/(\d+)/detail/",
                    reference_html, re.I | re.S,
                )
            ]
            if not references:
                references = [{"referencia": value.upper()} for value in dict.fromkeys(
                    re.findall(r"\b[A-Z0-9-]{8,32}\b", find("referencia", "serial", "gpon"), re.I)
                )]
            results.append({
                "product_id": int(product_match.group(1)) if product_match else None,
                "code": find("codigo", "cod."), "description": product or "Material",
                "quantity": quantity, "reference_type": 1 if references else 0,
                "references": references, "location": location,
                "category": find("categoria"), "ncm": find("ncm"),
            })
    return results


def reconcile_stock_balances(
    location_name: str, before: list[dict[str, Any]], after: list[dict[str, Any]],
    movements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reconcile two observed balances; never infer an opening balance."""
    location = _normalized(location_name)
    buckets: dict[str, dict[str, Any]] = {}
    for source, field in ((before, "initial"), (after, "current")):
        for item in source:
            name = str(item.get("description") or "").strip()
            key = _normalized(name)
            if not key:
                continue
            bucket = buckets.setdefault(key, {"product": name, "initial": 0.0, "entries": 0.0,
                                              "returns": 0.0, "uses": 0.0, "transfers_out": 0.0,
                                              "current": 0.0})
            bucket[field] += float(item.get("quantity") or 0)
    unmatched: list[dict[str, Any]] = []
    def is_selected_stock(value: str) -> bool:
        if not location or value.startswith("cliente"):
            return False
        cleaned = re.sub(r"^(?:estoque|local)\s*:?\s*", "", value)
        return cleaned == location or _same_person(cleaned, location)

    for movement in movements:
        movement.pop("flow", None)
        key = _normalized(movement.get("product"))
        origin = _normalized(movement.get("origin"))
        destination = _normalized(movement.get("destination"))
        quantity = float(movement.get("quantity") or 0)
        from_stock = is_selected_stock(origin)
        to_stock = is_selected_stock(destination)
        if not from_stock and not to_stock:
            continue
        if key not in buckets or not location or not quantity:
            movement["flow"] = "review"
            unmatched.append(movement); continue
        if from_stock == to_stock:
            movement["flow"] = "review"
            unmatched.append(movement); continue
        movement_type = _normalized(movement.get("movement_type"))
        if to_stock:
            field = "returns" if origin.startswith("cliente") or any(
                word in movement_type for word in ("estorno", "devolu", "retorno")
            ) else "entries"
        else:
            field = "uses" if destination.startswith("cliente") else "transfers_out"
        movement["flow"] = field
        buckets[key][field] += quantity
    items = []
    for bucket in buckets.values():
        bucket["expected"] = round(bucket["initial"] + bucket["entries"] + bucket["returns"]
                                   - bucket["uses"] - bucket["transfers_out"], 2)
        bucket["difference"] = round(bucket["current"] - bucket["expected"], 2)
        items.append(bucket)
    return {"items": sorted(items, key=lambda item: (item["difference"] == 0, item["product"].casefold())),
            "unmatched_movements": unmatched, "balanced": not unmatched and all(not item["difference"] for item in items)}


def stock_day_without_opening(
    location_name: str, current_balance: list[dict[str, Any]], movements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Show real daily flows without claiming an unobserved opening was checked."""
    products = {str(item.get("description") or "") for item in current_balance}
    products.update(str(item.get("product") or "") for item in movements)
    catalog = [{"description": name, "quantity": 0} for name in products if name]
    result = reconcile_stock_balances(location_name, catalog, current_balance, movements)
    for item in result["items"]:
        item.update(initial=None, expected=None, difference=None)
    return {**result, "balanced": None, "opening_unknown": True, "movement_count": len(movements)}


def connector_classification(service_type: str) -> str:
    """One policy for usage reports and physical-return obligations; never use OS notes."""
    value = _normalized(service_type)
    if "corretiva" in value or ("mudanca de endereco" in value and "mesmo lugar" in value):
        return "return"
    if "instalacao de kit" in value or "mudanca de endereco" in value:
        return "exempt"
    return "review"


def stock_day_with_calculated_opening(
    location_name: str, current_balance: list[dict[str, Any]], movements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calculate 06:00 opening from current balance only when every movement matches."""
    report = stock_day_without_opening(location_name, current_balance, movements)
    if report["unmatched_movements"]:
        return report
    openings = [round(item["current"] - item["entries"] - item["returns"]
                      + item["uses"] + item["transfers_out"], 2) for item in report["items"]]
    if any(value < 0 for value in openings):
        return report
    for item, initial in zip(report["items"], openings):
        item["initial"] = initial
        # A balance derived from the closing balance cannot independently verify it.
        item["expected"] = None
        item["difference"] = None
    return {**report, "opening_unknown": False, "opening_calculated": True,
            "balanced": None}


def reconcile_stock_day(
    location_name: str, previous_balance: list[dict[str, Any]],
    current_balance: list[dict[str, Any]], movements: list[dict[str, Any]], day_start: datetime,
) -> dict[str, Any]:
    earlier = [item for item in movements if datetime.fromisoformat(item["at_iso"]) < day_start]
    today = [item for item in movements if datetime.fromisoformat(item["at_iso"]) >= day_start]
    known_products = [{"description": item["product"], "quantity": 0} for item in movements]
    opening = reconcile_stock_balances(location_name, previous_balance, known_products, earlier)
    if opening["unmatched_movements"]:
        partial = stock_day_without_opening(location_name, current_balance, today)
        return {**partial, "incomplete": True,
                "unmatched_movements": opening["unmatched_movements"] + partial["unmatched_movements"],
                "message": "Não foi possível calcular a abertura de hoje com todos os movimentos anteriores."}
    opening_balance = [{"description": item["product"], "quantity": item["expected"]}
                       for item in opening["items"]]
    return {**reconcile_stock_balances(location_name, opening_balance, current_balance, today),
            "movement_count": len(today)}


def _paginated_admin_rows(open_page: Any, path: str, fields: list[tuple[str, str]],
                          page_size: int = 1000) -> list[str]:
    """Collect movement rows by SGP ID and fail if pagination cannot be verified."""
    rows: dict[str, str] = {}
    for page_number in range(1, 101):
        query = urlencode([("paginate_by", str(page_size)), *fields, ("page", str(page_number))])
        page = open_page(f"{path}?{query}")
        if re.search(r'<input\b[^>]*type=["\']password["\']', page, re.I):
            raise SGPError("O SGP não liberou o relatório; consulta incompleta.", 502)
        batch: dict[str, str] = {}
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
            if not cells:
                continue
            row_id = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cells[0])).split())
            if row_id.isdigit():
                batch[row_id] = row
        fresh = set(batch) - set(rows)
        if page_number > 1 and batch and not fresh:
            raise SGPError("O SGP repetiu a página do relatório; consulta incompleta.", 502)
        rows.update(batch)
        has_next = bool(re.search(rf"[?&]page={page_number + 1}(?:[&\"'])", html.unescape(page)))
        if not batch or (len(batch) < page_size and not has_next):
            return list(rows.values())
    raise SGPError("O relatório excedeu 100 páginas; consulta incompleta.", 502)


def _business_today() -> date:
    try:
        zone = ZoneInfo("America/Sao_Paulo")
    except ZoneInfoNotFoundError:
        zone = timezone(timedelta(hours=-3))
    return datetime.now(zone).date()


def _operational_week(value: date) -> tuple[date, date]:
    """Return the EBF reporting cycle: Saturday through Friday."""
    saturday = value - timedelta(days=(value.weekday() - 5) % 7)
    return saturday, saturday + timedelta(days=6)


def _scheduled_on(value: Any, expected: date) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    for candidate in (text[:10], text.split("T", 1)[0], text.split(" ", 1)[0]):
        try:
            return date.fromisoformat(candidate) == expected
        except ValueError:
            try:
                return datetime.strptime(candidate, "%d/%m/%Y").date() == expected
            except ValueError:
                continue
    return False


def _classify_modem_return_events(events: list[tuple[datetime, int, str, str, dict[str, Any]]]) -> list[tuple[datetime, str, str, str, dict[str, Any]]]:
    """Consolidate repeated ONU audit messages into physical modem returns.

    SGP history is rendered newest-first and only records time to the minute in
    some installations. The source position therefore breaks equal-time ties
    in chronological order. Repeated unlink messages for the same serial are
    treated as one physical modem, while any later link of a different serial
    confirms an exchange.
    """
    ordered = sorted(events, key=lambda event: (event[0], -event[1]))
    returns = []
    unlinked_serials = list(dict.fromkeys(event[3] for event in ordered if event[2] == "unlink"))
    for serial in unlinked_serials:
        unlink_indexes = [index for index, event in enumerate(ordered) if event[2] == "unlink" and event[3] == serial]
        first_unlink = unlink_indexes[0]
        later_links = [(index, event) for index, event in enumerate(ordered) if index > first_unlink and event[2] == "link"]
        different_links = [(index, event) for index, event in later_links if event[3] != serial]
        if different_links:
            link_index, replacement = different_links[0]
            evidence_index = max(index for index in unlink_indexes if index < link_index)
            evidence = ordered[evidence_index]
            returns.append((evidence[0], serial, "troca", replacement[3], evidence[4]))
            continue
        last_unlink = unlink_indexes[-1]
        if not any(index > last_unlink for index, _event in later_links):
            evidence = ordered[last_unlink]
            returns.append((evidence[0], serial, "retirada", "", evidence[4]))
    # An OS can contain a chain of technical reauthorizations and temporary
    # modem swaps. The physical modem that must be returned is the latest
    # confirmed exchange before closure, not the first exchange of the day.
    return sorted(returns, key=lambda item: (item[2] != "troca", -item[0].timestamp()))


def _cached_order_coordinates(client: "SGPClient", order_id: int) -> tuple[Any, Any]:
    now = time.time()
    with ORDER_COORDINATE_LOCK:
        cached = ORDER_COORDINATE_CACHE.get(order_id)
        if cached and cached[0] > now:
            return cached[1]
        if len(ORDER_COORDINATE_CACHE) > 500:
            expired = [key for key, value in ORDER_COORDINATE_CACHE.items() if value[0] <= now]
            for key in expired:
                ORDER_COORDINATE_CACHE.pop(key, None)
        if len(ORDER_COORDINATE_CACHE) > 4000:
            oldest = sorted(ORDER_COORDINATE_CACHE, key=lambda key: ORDER_COORDINATE_CACHE[key][0])[:1000]
            for key in oldest:
                ORDER_COORDINATE_CACHE.pop(key, None)
    detail = client.order(order_id, include_onu=False)
    detail = detail or {}
    point = _coordinates(
        detail.get("endereco_ll")
        or detail.get("contrato_endereco_ll")
        or detail.get("map_ll")
        or detail.get("localizacao")
    )
    if not point:
        for latitude_key, longitude_key in (
            ("os_latitude", "os_longitude"),
            ("latitude", "longitude"),
            ("checkin_latitude", "checkin_longitude"),
        ):
            point = _coordinates(f"{detail.get(latitude_key, '')},{detail.get(longitude_key, '')}")
            if point:
                break
    point = point or (None, None)
    with ORDER_COORDINATE_LOCK:
        ORDER_COORDINATE_CACHE[order_id] = (now + ORDER_COORDINATE_TTL, point)
    return point


class SGPError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _report_select_options(page: str, name: str) -> list[dict[str, str]]:
    select = re.search(rf"<select\b[^>]*\bname=['\"]{re.escape(name)}['\"][^>]*>(.*?)</select>", page, re.I | re.S)
    if not select:
        return []
    values = []
    for match in re.finditer(r"<option\b[^>]*\bvalue=['\"]?([^'\"\s>]*)[^>]*>(.*?)</option>", select.group(1), re.I | re.S):
        label = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(2))).split())
        values.append({"id": html.unescape(match.group(1)).strip(), "name": label})
    return values


def _onu_connectivity(onu: dict[str, Any]) -> bool | None:
    """Read connectivity only from ONU connection fields, never contract status."""
    for field in ("online", "connected", "connection", "conexao"):
        value = onu.get(field)
        if isinstance(value, dict):
            value = next((value[k] for k in ("online", "connected", "status") if value.get(k) is not None), None)
        if isinstance(value, bool):
            return value
        label = str(value).strip().casefold()
        if label in ("1", "true", "online", "connected", "conectado", "ativo"):
            return True
        if label in ("0", "false", "offline", "disconnected", "desconectado", "inativo"):
            return False
    return None


def _coverage_report_markers(page: str, olt_id: str, olt_name: str, online: bool | None = None) -> list[dict[str, Any]]:
    """Parse Google/Leaflet markers from SGP's authenticated online map report."""
    marker_pattern = re.compile(
        r"(?:var\s+)?marker(?P<key>[A-Za-z0-9_]*)\s*=.*?position\s*:\s*\{\s*lat\s*:\s*(?P<lat>-?[\d.]+)\s*,\s*lng\s*:\s*(?P<lng>-?[\d.]+)",
        re.I | re.S,
    )
    matches = list(marker_pattern.finditer(page))
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(page), match.end() + 12000)
        segment = page[match.start():end]
        # Some SGP themes declare the popup before the marker.  Attach the
        # matching contentString without borrowing text from another marker.
        key = match.group("key")
        if key:
            popup = re.search(
                rf"(?:contentString|conteudo|popup){re.escape(key)}\s*=\s*(.*?);\s*(?:var\s+)?(?:infoWindow|infowindow|marker){re.escape(key)}\b",
                page, re.I | re.S,
            )
            if popup:
                segment = popup.group(1) + " " + segment
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", segment)).replace("\\n", " ").split())
        contract_match = re.search(r"/admin/cliente/(\d+)/contratos/[^>]*>\s*(\d+)(?:\s*-\s*([^<]+))?", segment, re.I)
        contract_id = int(contract_match.group(2)) if contract_match else None
        client_id = int(contract_match.group(1)) if contract_match else None
        client = " ".join(html.unescape(contract_match.group(3) or "").split()) if contract_match else ""
        if not client:
            client_match = re.search(r"(?:Cliente|Assinante)\s*:?\s*([^|;]{3,160}?)(?=\s+(?:Contrato|ONU|OLT|Slot|PON|Status)\b|$)", text, re.I)
            client = client_match.group(1).strip(" :-") if client_match else "Cliente não identificado"
        def labeled(pattern: str) -> str | None:
            found = re.search(pattern, text, re.I)
            return found.group(1).strip(" :-") if found else None
        serial = labeled(r"(?:ONU|GPON|PHY(?:\s*Address)?|Serial)\s*:?\s*([A-Za-z0-9:_-]{6,32})")
        slot = _as_int(labeled(r"Slot\s*:?\s*(\d+)"))
        pon = _as_int(labeled(r"PON\s*:?\s*(\d+)"))
        signal = labeled(r"(?:Sinal|RX)\s*:?\s*(-?[\d.,]+\s*dBm)")
        contract_id = contract_id or _as_int(labeled(r"Contrato\s*:?\s*(\d+)"))
        rows.append({
            "id": f"{olt_id}:{contract_id or serial or match.group('key') or index}",
            "client_id": client_id, "contract_id": contract_id, "client": client,
            "serial": serial, "olt_id": str(olt_id), "olt": olt_name,
            "slot": slot, "pon": pon, "signal": signal, "online": online,
            "contract_status": labeled(r"Status(?:\s+do\s+contrato)?\s*:\s*(Ativo V\. Reduzida|Ativo|Suspenso|Cancelado)"),
            "username": labeled(r"(?:Username|Login|PPPoE)\s*:\s*([\w.@-]+)"),
            "onu_id": labeled(r"(?:ID\s+ONU|ONU\s+ID)\s*:\s*(\d+)"),
            "address": labeled(r"Endere[çc]o\s*:\s*([^|;]+?)(?=\s+(?:Contrato|ONU|OLT|Slot|PON|Status|Login)\b|$)"),
            "latitude": float(match.group("lat")), "longitude": float(match.group("lng")),
        })
    if rows:
        return rows
    # Some SGP themes render Leaflet markers instead of Google Maps markers.
    for index, match in enumerate(re.finditer(r"L\.marker\s*\(\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]\s*\)(.*?)(?=L\.marker|$)", page, re.I | re.S)):
        segment = match.group(3)[:8000]
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", segment)).split())
        contract = re.search(r"Contrato\s*:?\s*(\d+)", text, re.I)
        client = re.search(r"(?:Cliente|Assinante)\s*:?\s*(.*?)(?=\s+(?:Contrato|ONU|OLT|Slot|PON|Status)\b|$)", text, re.I)
        rows.append({
            "id": f"{olt_id}:{contract.group(1) if contract else index}", "client_id": None,
            "contract_id": int(contract.group(1)) if contract else None,
            "client": client.group(1).strip() if client else "Cliente não identificado",
            "serial": None, "olt_id": str(olt_id), "olt": olt_name, "slot": None, "pon": None,
            "signal": None, "online": online, "latitude": float(match.group(1)), "longitude": float(match.group(2)),
        })
    return rows


def _coverage_report_json(payload: dict[str, Any], olt_id: str, olt_name: str) -> list[dict[str, Any]]:
    """Convert the SGP map AJAX payload into coordinate records."""
    rows: list[dict[str, Any]] = []
    for network_status in ("online", "offline"):
        for index, raw in enumerate(payload.get(network_status) or []):
            if not isinstance(raw, dict):
                continue
            latitude, longitude = _coordinates(f"{raw.get('lat')},{raw.get('lng')}") or (None, None)
            if latitude is None:
                continue
            contract = _as_int(raw.get("contract_id") or raw.get("clientecontrato_id") or raw.get("contrato"))
            client_id = _as_int(raw.get("cliente_id") or raw.get("client_id"))
            rows.append({
                "id": f"{olt_id}:{raw.get('id') or contract or raw.get('onu_id') or index}",
                "client_id": client_id, "contract_id": contract,
                "client": str(raw.get("cliente") or raw.get("clientecontrato") or raw.get("login") or "Cliente não identificado"),
                "username": raw.get("login") or raw.get("username"),
                "address": raw.get("address") or raw.get("endereco"),
                "serial": raw.get("serial") or raw.get("phy_addr") or raw.get("onu_serial"),
                "onu_id": _as_int(raw.get("onu_id")), "olt_id": str(olt_id), "olt": olt_name,
                "slot": _as_int(raw.get("slot")), "pon": _as_int(raw.get("pon")),
                "signal": raw.get("signal") or raw.get("sinal"), "online": network_status == "online",
                "network_status": network_status, "contract_status": raw.get("status"),
                "latitude": latitude, "longitude": longitude,
            })
    return rows


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


class SGPClient:
    def __init__(self, base_url: str, credentials: Credentials, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.timeout = timeout

    def _authorization(self) -> str:
        raw = f"{self.credentials.username}:{self.credentials.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def request(
        self,
        method: str,
        path: str,
        *,
        form: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        multipart_fields: dict[str, Any] | None = None,
        multipart_file: tuple[str, str, bytes] | None = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            clean_query = {key: value for key, value in query.items() if value not in (None, "")}
            if clean_query:
                url += "?" + urlencode(clean_query)

        body = None
        headers = {
            "Authorization": self._authorization(),
            "Accept": "application/json",
            "User-Agent": "EBF-Tecnico/0.1",
        }
        if multipart_fields is not None and multipart_file is not None:
            boundary = "----EBF" + secrets.token_hex(12)
            chunks: list[bytes] = []
            for key, value in multipart_fields.items():
                chunks.extend([
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                    str(value).encode("utf-8"), b"\r\n",
                ])
            filename, content_type, file_bytes = multipart_file
            safe_filename = filename.replace('"', "").replace("\r", "").replace("\n", "")
            chunks.extend([
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(), file_bytes, b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ])
            body = b"".join(chunks)
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = Request(url, data=body, headers=headers, method=method.upper())
        if not SGP_REQUEST_GATE.acquire(timeout=8):
            raise SGPError("O SGP está recebendo muitas consultas. Aguarde alguns segundos e tente novamente.", 503)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
                if not payload:
                    return None
                try:
                    return json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise SGPError(
                        "O SGP devolveu uma resposta inválida. Aguarde alguns segundos e tente novamente.",
                        502,
                    ) from exc
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise SGPError("Usuário ou senha inválidos, ou acesso não autorizado.", 401) from exc
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = ""
            raise SGPError(f"O SGP recusou a operação ({exc.code}). {detail[:160]}", exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise SGPError("Não foi possível conectar ao SGP. Tente novamente.", 503) from exc

        finally:
            SGP_REQUEST_GATE.release()

    def request_binary(self, path: str) -> tuple[bytes, str]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = Request(url, headers={"Authorization": self._authorization(), "User-Agent": "EBF-Tecnico/0.1"})
        if not SGP_REQUEST_GATE.acquire(timeout=8):
            raise SGPError("O SGP está recebendo muitas consultas. Aguarde alguns segundos e tente novamente.", 503)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read(), response.headers.get_content_type()
        except HTTPError as exc:
            raise SGPError("Anexo não encontrado.", exc.code) from exc
        except (URLError, TimeoutError) as exc:
            raise SGPError("Não foi possível carregar o anexo.", 503) from exc

        finally:
            SGP_REQUEST_GATE.release()

    def user_info(self) -> dict[str, Any]:
        return self.request("GET", "/api/auth/info/")

    def orders(self) -> list[dict[str, Any]]:
        result = self.request("POST", "/api/os/list/", form={"orderby": "data_agendamento"})
        rows = result if isinstance(result, list) else []
        today = _business_today()
        return [
            item for item in rows
            if _scheduled_on(item.get("os_data_agendamento") or item.get("data_agendamento"), today)
        ]

    def route_monitor_orders(self, selected_day: date | None = None) -> list[dict[str, Any]]:
        """Load today's four OS states from the SGP URA endpoint."""
        selected_day = selected_day or _business_today()
        day = selected_day.isoformat()
        def load(status: int) -> list[dict[str, Any]]:
            form: dict[str, Any] = {"status": status, "limit": 1000}
            if status == 1:
                form.update({"data_finalizacao_inicio": day, "data_finalizacao_fim": day})
            else:
                form.update({"data_agendamento_inicio": day, "data_agendamento_fim": day})
            return self._ura_order_pages(form)

        with ThreadPoolExecutor(max_workers=4) as pool:
            pages = list(pool.map(load, (0, 1, 2, 3)))

        def timestamp(row: dict[str, Any], prefix: str) -> str | None:
            raw_date, raw_time = row.get(f"data_{prefix}"), row.get(f"hora_{prefix}")
            if not raw_date:
                return None
            return f"{raw_date} {raw_time or '00:00:00'}".strip()

        normalized: dict[str, dict[str, Any]] = {}
        for row in (item for page in pages for item in page):
            order_id = row.get("id") or row.get("os_id")
            if order_id in (None, ""):
                continue
            normalized[str(order_id)] = {
                **row,
                "os_id": order_id,
                "os_status": row.get("status_id", row.get("os_status", 0)),
                "os_status_txt": row.get("status") or row.get("os_status_txt"),
                "os_data_agendamento": timestamp(row, "agendamento") or row.get("os_data_agendamento"),
                "os_data_finalizacao": timestamp(row, "finalizacao") or row.get("os_data_finalizacao"),
                "os_tecnico_responsavel": row.get("responsavel") or row.get("os_tecnico_responsavel"),
                "os_tecnicos_auxiliares": row.get("tecnicos_auxiliares") or row.get("os_tecnicos_auxiliares") or [],
                "os_motivo_descricao": row.get("motivo") or row.get("os_motivo_descricao"),
            }
        return list(normalized.values())

    def technician_itinerary(self, technician: str, selected_day: date | None = None) -> dict[str, Any]:
        day = selected_day or _business_today()
        orders = self.route_monitor_orders(day)
        row = build_route_monitor(orders, technicians=(technician,),
                                  now=datetime.combine(day, datetime.min.time()))["technicians"][0]
        services = row["services"]
        def locate(service):
            try:
                lat, lng = _cached_order_coordinates(self, int(service["id"]))
            except (SGPError, OSError, TimeoutError, ValueError):
                lat, lng = None, None
            return {**service, "latitude": lat, "longitude": lng,
                    "position_available": lat is not None and lng is not None}
        with ThreadPoolExecutor(max_workers=min(3, len(services) or 1)) as pool:
            located = list(pool.map(locate, services))
        completed = sorted([s for s in located if s["status"] == 1],
                           key=lambda s: (s["finished_at"] or "9999", int(s["id"])))
        current = sorted([s for s in located if s["status"] == 2],
                         key=lambda s: (s["started_at"] or s["scheduled_at"] or "9999", int(s["id"])))
        upcoming = sorted([s for s in located if s["status"] in (0, 3)],
                          key=lambda s: (s["scheduled_at"] or "9999", int(s["id"])))
        return {"technician": technician, "date": day.isoformat(), "completed": completed,
                "current": current, "upcoming": upcoming,
                "missing_coordinates": sum(not s["position_available"] for s in located),
                "generated_at": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat()}

    def route_monitor_start_times(
        self, orders: list[dict[str, Any]], selected_day: date | None = None,
        technicians: tuple[str, ...] = ROUTE_MONITOR_TECHNICIANS,
    ) -> dict[int, tuple[int | None, str | None]]:
        """Find the real Open -> Running transition in each customer's admin history."""
        selected_day = selected_day or _business_today()
        targets = [
            order for order in orders
            if int(order.get("os_status") or 0) in (1, 2)
            and any(_same_person(order.get("os_tecnico_responsavel"), name) for name in technicians)
        ]
        if not targets:
            return {}

        def detail(order: dict[str, Any]) -> tuple[int, int | None]:
            order_id = int(order.get("os_id") or 0)
            try:
                value = self.order(order_id, include_onu=False, include_signal_context=False) or {}
                return order_id, _as_int(value.get("cliente_id"))
            except (SGPError, OSError, TimeoutError, ValueError):
                return order_id, None

        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
            client_by_order = dict(pool.map(detail, targets))
        client_ids = sorted({value for value in client_by_order.values() if value})
        results: dict[int, tuple[int | None, str | None]] = {
            order_id: (client_id, None) for order_id, client_id in client_by_order.items()
        }
        if not client_ids:
            return results

        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")

        try:
            login_path = "/accounts/login?next=/admin/"
            login_page = open_page(login_path)
            token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not token:
                return results
            login_body = urlencode({
                "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8")
            open_page("/accounts/login/", login_body, login_path)
        except (HTTPError, URLError, OSError, TimeoutError):
            return results

        expected = selected_day.strftime("%d/%m/%Y")
        # A mesma OS pode sair de execucao, ficar pendente e ser retomada
        # horas depois. Nesse caso o monitor deve usar o inicio da sessao
        # atual, nao a primeira execucao do dia.
        transition = re.compile(
            r"status:\s*alterado de (?:aberta|pendente) para em execucao"
        )

        def history(client_id: int) -> list[tuple[int, str]]:
            found: dict[int, datetime] = {}
            try:
                page = open_page(f"/admin/cliente/{client_id}/historico/", referer="/admin/")
                for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S):
                    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
                    values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
                    if (
                        len(values) < 5
                        or not transition.search(_normalized(values[3]))
                        or not values[4].startswith(expected)
                    ):
                        continue
                    match = re.search(r"ordem de servi[cç]o\s+n[.º°]?\s*(\d+)", _normalized(values[3]), re.I)
                    parsed = _parse_datetime(values[4])
                    if match and parsed:
                        order_id = int(match.group(1))
                        if order_id not in found or parsed > found[order_id]:
                            found[order_id] = parsed
            except (HTTPError, URLError, OSError, TimeoutError, ValueError):
                pass
            return [(order_id, value.isoformat()) for order_id, value in found.items()]

        with ThreadPoolExecutor(max_workers=min(6, len(client_ids))) as pool:
            for entries in pool.map(history, client_ids):
                for order_id, started_at in entries:
                    if order_id in results:
                        results[order_id] = (results[order_id][0], started_at)
        return results

    def route_monitor_positions(
        self, orders: list[dict[str, Any]], technicians: tuple[str, ...] = ROUTE_MONITOR_TECHNICIANS,
    ) -> list[dict[str, Any]]:
        """Locate each technician at the running OS, or at the latest closed OS."""
        selected: list[tuple[str, dict[str, Any], str, dict[str, Any] | None]] = []
        without_service: list[str] = []
        for technician in technicians:
            own = [
                order for order in orders
                if any(_same_person(name, technician) for name in [
                    order.get("os_tecnico_responsavel") or order.get("responsavel"),
                    *(order.get("os_tecnicos_auxiliares") or order.get("tecnicos_auxiliares") or []),
                ] if name)
            ]
            running = [order for order in own if int(order.get("os_status") or 0) == 2]
            closed = [order for order in own if int(order.get("os_status") or 0) == 1]
            latest_closed = max(closed, key=lambda item: str(item.get("os_data_finalizacao") or ""), default=None)
            if running:
                order = max(running, key=lambda item: str(item.get("checkin_data") or item.get("os_data_agendamento") or ""))
                selected.append((technician, order, "Em execução", latest_closed))
            elif latest_closed:
                selected.append((technician, latest_closed, "Último encerrado", None))
            else:
                without_service.append(technician)

        def locate(value: tuple[str, dict[str, Any], str, dict[str, Any] | None]) -> tuple[str, dict[str, Any], str, tuple[Any, Any]]:
            technician, order, label, fallback = value
            try:
                point = _cached_order_coordinates(self, int(order.get("os_id") or 0))
            except (SGPError, OSError, TimeoutError, ValueError):
                point = (None, None)
            if point[0] is None and fallback:
                try:
                    fallback_point = _cached_order_coordinates(self, int(fallback.get("os_id") or 0))
                except (SGPError, OSError, TimeoutError, ValueError):
                    fallback_point = (None, None)
                if fallback_point[0] is not None:
                    order, label, point = fallback, "Último encerrado", fallback_point
            return technician, order, label, point

        result: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(6, len(selected) or 1)) as pool:
            for technician, order, label, point in pool.map(locate, selected):
                result.append({
                    "technician": technician,
                    "order_id": order.get("os_id"),
                    "client": order.get("cliente") or "Cliente não informado",
                    "state": label,
                    "latitude": point[0],
                    "longitude": point[1],
                    "position_available": point[0] is not None and point[1] is not None,
                })
        result.extend({
            "technician": technician, "order_id": None,
            "client": "Não realizou nenhum serviço hoje",
            "state": "Sem serviço no dia", "latitude": None, "longitude": None,
            "position_available": False, "no_service": True,
        } for technician in without_service)
        result.sort(key=lambda item: technicians.index(item["technician"]))
        return result

    def open_service_map(self, selected_day: date | None = None, cached_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Return open occurrences scheduled for the day, enriched by their OS."""
        day = selected_day or _business_today()
        cached_by_id = {
            int(item.get("order_id") or 0): item for item in (cached_items or [])
            if int(item.get("order_id") or 0) > 0
        }
        try:
            rows = self._admin_open_occurrences_for_day(day)
        except (SGPError, HTTPError, URLError, OSError, TimeoutError, ValueError):
            rows = self._ura_order_pages({
                "status": 0, "limit": 1000,
                "data_agendamento_inicio": day.isoformat(),
                "data_agendamento_fim": day.isoformat(),
            })

        def normalize(row: dict[str, Any]) -> dict[str, Any]:
            order_id = int(row.get("os_id") or row.get("id") or 0)
            previous = cached_by_id.get(order_id) or {}
            detail: dict[str, Any] = {}
            if order_id and not previous:
                try:
                    detail = self.order(order_id, include_onu=False) or {}
                except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                    detail = {}
            point = _coordinates(detail or row) or (None, None)
            if point[0] is None and previous:
                point = (previous.get("latitude"), previous.get("longitude"))
            latitude, longitude = point
            if latitude is None and order_id:
                try:
                    latitude, longitude = _cached_order_coordinates(self, order_id)
                except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                    latitude, longitude = None, None
            address = " ".join(str(value) for value in (
                detail.get("endereco_logradouro") or row.get("endereco_logradouro"),
                detail.get("endereco_numero") or row.get("endereco_numero"),
                detail.get("endereco_bairro") or row.get("endereco_bairro"),
                detail.get("endereco_cidade") or row.get("endereco_cidade"),
            ) if value not in (None, "")).strip() or str(previous.get("address") or "")
            return {
                "order_id": order_id,
                "occurrence_id": detail.get("ocorrencia_id") or row.get("ocorrencia_id") or row.get("ocorrencia"),
                "client": detail.get("cliente") or row.get("cliente") or row.get("cliente_nome") or "Cliente não informado",
                "technician": detail.get("os_tecnico_responsavel") or row.get("responsavel") or row.get("os_tecnico_responsavel") or "Sem técnico",
                "reason": detail.get("os_motivo_descricao") or row.get("motivo") or row.get("os_motivo_descricao") or "Serviço",
                "scheduled_at": detail.get("os_data_agendamento") or row.get("os_data_agendamento") or row.get("data_agendamento"),
                "address": address or detail.get("endereco") or row.get("endereco") or "Endereço não informado",
                "latitude": latitude, "longitude": longitude,
                "position_available": latitude is not None and longitude is not None,
            }

        with ThreadPoolExecutor(max_workers=min(8, len(rows) or 1)) as pool:
            items = list(pool.map(normalize, rows))
        items.sort(key=lambda item: str(item.get("scheduled_at") or ""))
        return {
            "date": day.isoformat(), "items": items,
            "located": len([item for item in items if item["position_available"]]),
            "total": len(items),
        }

    def _admin_open_occurrences_for_day(self, selected_day: date) -> list[dict[str, Any]]:
        """Use Relatório > Atendimento > Ocorrência with today's scheduling filters."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None) -> str:
            request = Request(f"{self.base_url}{path}", data=body, headers=headers)
            with opener.open(request, timeout=max(self.timeout, 60)) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        login_page = open_page(login_path)
        token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not token:
            raise SGPError("O SGP não liberou o relatório de ocorrências.", 502)
        open_page("/accounts/login/", urlencode({
            "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/",
        }).encode("utf-8"))
        start = selected_day.strftime("%d/%m/%Y 00:00:00")
        end = selected_day.strftime("%d/%m/%Y 23:59:59")
        query = urlencode({
            "paginate_by": 1000, "status": 0, "com_agendamento": "sim",
            "data_agendamento_inicial": start, "data_agendamento_final": end, "crm": "NAO",
        })
        report = open_page(f"/admin/atendimento/relatorios/ocorrencia/?{query}")
        rows: list[dict[str, Any]] = []
        for raw_row in re.findall(r"<tr[^>]*>(.*?)</tr>", report, re.I | re.S):
            occurrence = re.search(r"/admin/atendimento/ocorrencia/(\d+)/edit/", raw_row, re.I)
            if not occurrence:
                continue
            cells = re.findall(r"<td[^>]*>(.*?)</td>", raw_row, re.I | re.S)
            values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
            if len(values) < 19:
                continue
            order = re.search(r"/admin/atendimento/ocorrencia/os/(\d+)/edit/", raw_row, re.I)
            client = re.search(r"/admin/cliente/(\d+)/contratos/", raw_row, re.I)
            client_name = re.sub(r"^\d+\s*-\s*", "", values[2]).strip()
            scheduled_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})", values[10])
            rows.append({
                "id": int(order.group(1)) if order else 0,
                "os_id": int(order.group(1)) if order else 0,
                "ocorrencia_id": int(occurrence.group(1)),
                "cliente_id": int(client.group(1)) if client else None,
                "cliente": client_name or "Cliente não informado",
                "motivo": values[5] or "Ocorrência",
                "responsavel": values[12] or "Sem técnico",
                "data_agendamento": scheduled_match.group(1) if scheduled_match else values[10],
                "endereco": values[4], "endereco_bairro": values[16],
            })
        return rows

    def traffic_disconnects(self, client_id: int, contract_id: int, start_date: date, end_date: date, service_id: int | None = None) -> dict[str, Any]:
        """Read offline sessions from the customer's native traffic statement."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}
        def open_page(path: str, body: bytes | None = None) -> str:
            request = Request(f"{self.base_url}{path}", data=body, headers=headers)
            with opener.open(request, timeout=max(self.timeout, 60)) as response:
                return response.read().decode("utf-8", "replace")
        login_path = "/accounts/login?next=/admin/"
        login_page = open_page(login_path)
        token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not token:
            raise SGPError("O SGP não liberou o extrato de tráfego.", 502)
        open_page("/accounts/login/", urlencode({
            "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/",
        }).encode("utf-8"))
        path = f"/admin/cliente/{int(client_id)}/extrato_trafego/"
        form = open_page(path)
        options = re.findall(r"<option[^>]+value=['\"]([^'\"]+)['\"][^>]*>(.*?)</option>", form, re.I | re.S)
        option_values = {value for value, _label in options}
        selected_service = str(int(service_id)) if service_id and str(int(service_id)) in option_values else None
        for value, label in options:
            clean_label = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", label)).split())
            if value == selected_service or re.search(rf"Contrato:\s*{int(contract_id)}\b", clean_label, re.I):
                selected_service = value
                break
        if not selected_service:
            if "extrato_trafego" not in form or "data_inicial" not in form:
                raise SGPError("Seu usuário do SGP não possui acesso ao extrato de tráfego.", 403)
            raise SGPError("O serviço deste contrato não apareceu no extrato de tráfego.", 404)
        query = urlencode({
            "servico": selected_service, "data_inicial": start_date.strftime("%d/%m/%Y 00:00:00"),
            "data_final": end_date.strftime("%d/%m/%Y 23:59:59"),
            "starttime_only": "on", "show_offline": "on",
        })
        report = open_page(f"{path}?{query}")
        items: list[dict[str, Any]] = []
        for raw_row in re.findall(r"<tr[^>]*>(.*?)</tr>", report, re.I | re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", raw_row, re.I | re.S)
            values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
            if len(values) >= 8 and _normalized(values[1]) == "offline":
                items.append({"started_at": values[5], "ended_at": values[6], "duration": values[7]})
        return {"client_id": int(client_id), "contract_id": int(contract_id), "service_id": int(selected_service), "start": start_date.isoformat(), "end": end_date.isoformat(), "items": items, "total": len(items)}

    def radius_technical_info(self, username: str) -> dict[str, Any]:
        username = str(username or "").strip()
        if not username:
            raise SGPError("O SGP não informou o login PPPoE deste serviço.", 422)
        payload = self.request(
            "POST", "/ws/radius/radacct/list/all/",
            json_body={"username": username, "last_session": True, "limit": 1},
        )
        results = payload.get("result", []) if isinstance(payload, dict) else []
        item = results[0] if results and isinstance(results[0], dict) else {}
        sessions = item.get("radacct", []) if isinstance(item.get("radacct"), list) else []
        session = sessions[0] if sessions and isinstance(sessions[0], dict) else {}
        started, stopped = session.get("acctstarttime"), session.get("acctstoptime")
        duration_seconds = None
        try:
            start_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
            end_dt = datetime.now(start_dt.tzinfo) if not stopped else datetime.fromisoformat(str(stopped).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=start_dt.tzinfo)
            duration_seconds = max(0, int((end_dt - start_dt).total_seconds()))
        except (TypeError, ValueError):
            pass
        return {
            "found": bool(item), "login": item.get("pppoe_login") or session.get("username") or username,
            "online": bool(item.get("online")), "ip": session.get("framedipaddress") or item.get("ip"),
            "nas_ip": session.get("nasipaddress"), "nas_port": session.get("nasportid"),
            "calling_station": session.get("callingstationid"), "called_station": session.get("calledstationid"),
            "started_at": started, "stopped_at": stopped, "duration_seconds": duration_seconds,
            "terminate_cause": session.get("acctterminatecause"), "plan": item.get("plano"),
            "plan_id": item.get("plano_id"), "ipv6_prefix": session.get("framedipv6prefix"),
            "delegated_ipv6_prefix": session.get("delegatedipv6prefix"),
            "input_octets": session.get("acctinputoctets"), "output_octets": session.get("acctoutputoctets"),
        }

    def orders_by_status(self, display_name: str, status: int) -> list[dict[str, Any]]:
        if status not in (0, 1, 2, 3):
            raise SGPError("Situação inválida.", 400)
        today = _business_today()
        form: dict[str, Any] = {"status": status, "limit": 1000}
        if status == 1:
            form.update({"data_finalizacao_inicio": today.isoformat(), "data_finalizacao_fim": today.isoformat()})
        else:
            form.update({"data_agendamento_inicio": today.isoformat(), "data_agendamento_fim": today.isoformat()})
        orders = self._ura_order_pages(form)
        technician = display_name.casefold().strip()
        selected = [
            item for item in orders
            if (
                str(item.get("responsavel", "")).casefold().strip() == technician
                or technician in {str(name).casefold().strip() for name in item.get("tecnicos_auxiliares", [])}
            )
            and (status == 1 or _scheduled_on(item.get("data_agendamento"), today))
        ]
        return [
            {
                "os_id": item.get("id"), "cliente": item.get("cliente"),
                "os_motivo_descricao": item.get("motivo"), "os_conteudo": item.get("conteudo"),
                "os_status": item.get("status_id"), "os_status_txt": item.get("status"),
                "os_data_agendamento": item.get("data_agendamento"),
                "os_data_finalizacao": item.get("data_finalizacao"),
                "os_tecnico_responsavel": item.get("responsavel"),
            }
            for item in selected
        ]

    def admin_orders(self, period: str = "day") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        today = date.today()
        start = today if period == "day" else today - timedelta(days=today.weekday()) if period == "week" else today.replace(day=1)
        orders: list[dict[str, Any]] = []
        for status in (0, 2, 3):
            form = {"status": status, "limit": 1000, "data_agendamento_inicio": start.isoformat(), "data_agendamento_fim": today.isoformat()}
            for item in self._ura_order_pages(form):
                orders.append({
                    "os_id": item.get("id"), "cliente": item.get("cliente"),
                    "os_motivo_descricao": item.get("motivo"), "os_conteudo": item.get("conteudo"),
                    "os_status": item.get("status_id"), "os_status_txt": item.get("status"),
                    "os_data_cadastro": f"{item.get('data_cadastro')}T{item.get('hora_cadastro')}" if item.get("data_cadastro") and item.get("hora_cadastro") else item.get("data_cadastro"),
                    "os_data_agendamento": item.get("data_agendamento"),
                    "os_tecnico_responsavel": item.get("responsavel") or "Sem técnico",
                    "os_tecnicos_auxiliares": item.get("tecnicos_auxiliares") or [],
                })
        closed: list[dict[str, Any]] = []
        for item in self._ura_order_pages({"status": 1, "limit": 1000, "data_finalizacao_inicio": start.isoformat(), "data_finalizacao_fim": today.isoformat()}):
            closed.append({
                "os_id": item.get("id"), "cliente": item.get("cliente"), "os_motivo_descricao": item.get("motivo"),
                "os_status": item.get("status_id"), "os_status_txt": item.get("status"),
                "os_data_agendamento": item.get("data_agendamento"), "os_data_finalizacao": item.get("data_finalizacao"),
                "os_tecnico_responsavel": item.get("responsavel") or "Sem técnico",
            })
        def coordinates(order: dict[str, Any]) -> tuple[Any, Any]:
            return _cached_order_coordinates(self, int(order["os_id"]))
        mapped = orders + closed
        with ThreadPoolExecutor(max_workers=8) as pool:
            for order, point in zip(mapped, pool.map(coordinates, mapped)):
                order["latitude"], order["longitude"] = point
        return orders, closed

    def admin_statuses(self) -> list[dict[str, Any]]:
        today = date.today().isoformat(); values: list[dict[str, Any]] = []
        for status in (0, 2, 3):
            rows = self._ura_order_pages({"status": status, "limit": 1000})
            values.extend({"os_id": row.get("id"), "os_status": row.get("status_id"), "os_tecnico_responsavel": row.get("responsavel"), "cliente": row.get("cliente")} for row in rows)
        rows = self._ura_order_pages({"status": 1, "limit": 1000, "data_finalizacao_inicio": today, "data_finalizacao_fim": today})
        values.extend({"os_id": row.get("id"), "os_status": row.get("status_id"), "os_tecnico_responsavel": row.get("responsavel"), "cliente": row.get("cliente")} for row in rows)
        return values

    def latest_closed_positions(self) -> list[dict[str, Any]]:
        today = date.today()
        rows = self._ura_order_pages({
            "status": 1, "limit": 1000,
            "data_finalizacao_inicio": today.replace(day=1).isoformat(),
            "data_finalizacao_fim": today.isoformat(),
        })
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            technician = str(row.get("responsavel") or "").strip()
            if not technician:
                continue
            current = latest.get(technician)
            stamp = f"{row.get('data_finalizacao') or ''}T{row.get('hora_finalizacao') or ''}"
            current_stamp = str(current.get("_stamp") or "") if current else ""
            if current is None or stamp > current_stamp:
                latest[technician] = {
                    "os_id": row.get("id"), "cliente": row.get("cliente"),
                    "os_tecnico_responsavel": technician,
                    "os_data_finalizacao": stamp, "_stamp": stamp,
                }
        def coordinates(order: dict[str, Any]) -> tuple[Any, Any]:
            return _cached_order_coordinates(self, int(order["os_id"]))
        values = list(latest.values())
        with ThreadPoolExecutor(max_workers=6) as pool:
            for order, point in zip(values, pool.map(coordinates, values)):
                order["latitude"], order["longitude"] = point
                order.pop("_stamp", None)
        return values

    def completed_timeline(self, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
        end_date = end_date or start_date
        rows = self._ura_order_pages({"status": 1, "limit": 1000, "data_finalizacao_inicio": start_date, "data_finalizacao_fim": end_date})
        return [{
            "os_id": row.get("id"), "technician": row.get("responsavel") or "Sem técnico",
            "client": row.get("cliente"), "reason": row.get("motivo"),
            "opened_at": f"{row.get('data_cadastro')}T{row.get('hora_cadastro')}" if row.get("data_cadastro") and row.get("hora_cadastro") else None,
            "started_at": f"{row.get('data_agendamento')}T{row.get('hora_agendamento')}" if row.get("data_agendamento") and row.get("hora_agendamento") else None,
            "closed_at": f"{row.get('data_finalizacao')}T{row.get('hora_finalizacao')}" if row.get("data_finalizacao") and row.get("hora_finalizacao") else None,
            "estimated": True,
        } for row in rows]

    def scheduled_timeline(self, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
        """Return the office schedule for every technician without inferring execution time."""
        end_date = end_date or start_date
        values: list[dict[str, Any]] = []
        for status in (0, 2, 3, 1):
            rows = self._ura_order_pages({
                "status": status, "limit": 1000,
                "data_agendamento_inicio": start_date, "data_agendamento_fim": end_date,
            })
            for row in rows:
                scheduled = row.get("data_agendamento")
                hour = row.get("hora_agendamento")
                if scheduled and hour and "T" not in str(scheduled) and " " not in str(scheduled):
                    scheduled = f"{scheduled}T{hour}"
                values.append({
                    "os_id": row.get("id"), "technician": row.get("responsavel") or "Sem técnico",
                    "client": row.get("cliente"), "reason": row.get("motivo"),
                    "status": row.get("status_id", status), "status_text": row.get("status"),
                    "scheduled_at": scheduled,
                })
        return values

    def map_orders(self, display_name: str) -> list[dict[str, Any]]:
        summaries: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            for orders in pool.map(lambda status: self.orders_by_status(display_name, status), (0, 2, 3)):
                for order in orders:
                    if order.get("os_id") is not None:
                        summaries[int(order["os_id"])] = order

        def enrich(order_id: int) -> dict[str, Any] | None:
            return self.order(order_id, include_onu=False)

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            details = pool.map(enrich, summaries.keys())
            for detail in details:
                if not detail:
                    continue
                coordinates = _coordinates(detail.get("endereco_ll") or detail.get("contrato_endereco_ll"))
                if not coordinates:
                    continue
                results.append({
                    "id": detail.get("os_id"), "client": detail.get("cliente"),
                    "reason": detail.get("os_motivo_descricao") or detail.get("os_conteudo"),
                    "status": detail.get("os_status"), "status_text": detail.get("os_status_txt"),
                    "scheduled_at": detail.get("os_data_agendamento"),
                    "address": ", ".join(str(value) for value in (
                        detail.get("endereco_logradouro"), detail.get("endereco_numero"),
                        detail.get("endereco_bairro"), detail.get("endereco_cidade"),
                    ) if value not in (None, "")),
                    "latitude": coordinates[0], "longitude": coordinates[1],
                })
        results.sort(key=lambda item: str(item.get("scheduled_at") or ""))
        return results

    def order(
        self, order_id: int, include_onu: bool = True, include_signal_context: bool = True
    ) -> dict[str, Any] | None:
        result = self.request("POST", f"/api/os/list/id/{order_id}", form={})
        if isinstance(result, list):
            order = result[0] if result else None
        else:
            order = result if isinstance(result, dict) else None
        if order and include_onu:
            order["onu"] = self.onu_for_service(order.get("servico_id"), order.get("servico_login"))
            if order["onu"] and include_signal_context:
                order["onu"]["signal_context"] = self.onu_signal_context(order["onu"])
        return order

    def onu_for_service(self, service_id: Any, service_login: Any = None) -> dict[str, Any] | None:
        query = {"servico": service_id} if service_id else {"login": service_login}
        if not any(query.values()):
            return None
        result = self.request("GET", "/api/fttx/onu/list/", query=query)
        return result[0] if isinstance(result, list) and result else None

    def onu_signal_context(self, onu: dict[str, Any]) -> dict[str, Any] | None:
        """Compare RX with valid ONUs on the same OLT/slot/PON (read-only)."""
        olt = onu.get("olt") or onu.get("olt_id")
        slot, pon = onu.get("slot"), onu.get("pon")
        if olt in (None, "") or slot in (None, "") or pon in (None, ""):
            return None
        result = self.request("GET", "/api/fttx/onu/list/", query={"olt": olt, "slot": slot, "pon": pon})
        if not isinstance(result, list):
            return None
        def same_port(item: dict[str, Any]) -> bool:
            item_olt = item.get("olt") or item.get("olt_id")
            return str(item_olt) == str(olt) and str(item.get("slot")) == str(slot) and str(item.get("pon")) == str(pon)
        def rx_value(item: dict[str, Any]) -> float | None:
            raw = str(item.get("info_rx") or "").replace(",", ".").replace("dBm", "").strip()
            try: value = float(raw)
            except ValueError: return None
            return value if -40 < value < -5 else None
        values = [value for item in result if same_port(item) if (value := rx_value(item)) is not None]
        current = rx_value(onu)
        if not values or current is None:
            return None
        average = round(sum(values) / len(values), 1)
        delta = round(current - average, 1)
        return {"average": average, "current": current, "delta": delta, "sample_size": len(values), "outside_average": abs(delta) >= 2}

    def add_order_note(self, order_id: int, note: str) -> dict[str, Any]:
        return self.request("POST", f"/api/os/anotacoes/add/id/{order_id}/", form={"anotacao": note})

    def update_order_content(self, order_id: int, content: str) -> dict[str, Any]:
        content = str(content or "").strip()
        if not content:
            raise SGPError("A orientação da OS não pode ficar vazia.", 400)
        return self.request(
            "POST", f"/api/os/update/id/{int(order_id)}/",
            form={"os_conteudo": content},
        )

    def update_order_status(
        self, order_id: int, status: int, service_performed: str = "",
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = current or self.order(order_id)
        if not current:
            raise SGPError("Ordem de serviço não encontrada.", 404)
        if int(current.get("os_status", -1)) == 1:
            raise SGPError("Uma OS encerrada não pode ser reaberta ou alterada.", 409)
        if status not in (1, 2, 3):
            raise SGPError("Situação inválida.", 400)
        service_performed = service_performed.strip()
        if status == 3 and not service_performed:
            raise SGPError("Informe o serviço realizado e o motivo da pendência.", 400)
        if status == 1 and not service_performed:
            raise SGPError("Informe o serviço prestado antes de encerrar a OS.", 400)
        reason = _normalized(str(current.get("os_motivo_descricao") or current.get("os_conteudo") or ""))
        removal = any(word in reason for word in ("retirada", "remocao", "remover kit"))
        installation = "instalacao" in reason
        if status == 1 and removal and not self.order_photos(order_id):
            raise SGPError("Anexe pelo menos uma foto antes de encerrar uma OS de retirada.", 409)
        if status == 1 and removal and current.get("onu"):
            raise SGPError("Desvincule o modem/ONU do cliente antes de encerrar a retirada.", 409)
        online = current.get("servico_online") is True or str(current.get("servico_online", "")).casefold() in ("1", "true", "sim", "online")
        if status == 1 and installation and not online:
            raise SGPError("A instalação só pode ser encerrada quando o serviço estiver online.", 409)
        if status == 1 and not self.order_materials(order_id):
            raise SGPError("Registre o material utilizado ou lance 'Sem uso de material' antes de encerrar a OS.", 409)
        form: dict[str, Any] = {"os_status": status}
        if service_performed:
            form["os_servicoprestado"] = service_performed
        return self.request("POST", f"/api/os/update/id/{order_id}/", form=form)

    def start_order(self, order_id: int) -> dict[str, Any]:
        return self.request("POST", f"/api/os/update/id/{order_id}/", form={"os_status": 2})

    def order_materials(self, order_id: int) -> list[dict[str, Any]]:
        result = self.request(
            "GET", "/api/estoque/lancamentoitem/list/", query={"ordemservico_id": order_id}
        )
        if not isinstance(result, list):
            raise SGPError("Resposta inválida ao consultar materiais da OS. Tente novamente.", 502)
        return [item for item in result if item.get("aplicado", True) and not item.get("estornado")]

    def add_order_materials(
        self, order_id: int, display_name: str, username: str, raw_items: list[dict[str, Any]],
        order: dict[str, Any] | None = None, mode: str = "comodato",
    ) -> dict[str, Any]:
        order = order or self.order(order_id)
        if not order:
            raise SGPError("Ordem de serviço não encontrada.", 404)
        if int(order.get("os_status", -1)) == 1:
            raise SGPError("Não é possível lançar material em uma OS encerrada.", 409)
        stock = self.stock_for_user(display_name, username)
        location = stock.get("location")
        if not location:
            raise SGPError("O técnico não possui um estoque individual no SGP.", 409)
        available = {int(item["product_id"]): item for item in stock.get("items", [])}
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            product_id = _as_int(raw.get("product_id"))
            try:
                quantity = float(raw.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0
            product = available.get(product_id or -1)
            if not product or quantity <= 0 or quantity > float(product.get("quantity") or 0):
                raise SGPError("Um dos materiais não possui saldo suficiente.", 409)
            reference_id = _as_int(raw.get("reference_id"))
            references = {int(ref["id"]) for ref in product.get("references", []) if ref.get("id") is not None}
            if references and reference_id not in references:
                raise SGPError(f"Selecione um serial válido para {product.get('description')}.", 400)
            if references and quantity != 1:
                raise SGPError("Equipamentos serializados devem ser lançados individualmente.", 400)
            usage = str(raw.get("usage", "")).strip().casefold()
            description = _normalized(str(product.get("description") or ""))
            if any(word in description for word in ("modem", "onu", "conector")) and usage not in ("instalacao", "troca"):
                raise SGPError(f"Informe se {product.get('description')} foi usado em instalação ou troca.", 400)
            item = {"produto_id": product_id, "quantidade": quantity, "valor": "0.00"}
            if usage:
                item["anotacao"] = "Instalação" if usage == "instalacao" else "Troca"
            if reference_id is not None:
                item["referencia_id"] = reference_id
            items.append(item)
        if not items:
            raise SGPError("Selecione pelo menos um material utilizado.", 400)
        mode = str(mode or "comodato").strip().casefold()
        if mode not in ("comodato", "kit"):
            raise SGPError("Escolha Comodato ou Kit de instalação.", 400)
        payload = {
            "os_id": order_id,
            "cliente_id": order.get("cliente_id"),
            "clientecontrato_id": order.get("contrato_id"),
            "comodato": 1 if mode == "comodato" else 0,
            "origem_id": location.get("id"),
            "itens": items,
        }
        return self.request("POST", "/api/estoque/lancamentoitem/create/", json_body=payload)

    def order_photos(self, order_id: int) -> list[dict[str, Any]]:
        result = self.request("GET", f"/api/os/imagem/id/{order_id}/list/")
        return result if isinstance(result, list) else []

    def order_photo(self, attachment_id: int) -> tuple[bytes, str]:
        return self.request_binary(f"/api/os/imagem/{attachment_id}")

    def add_order_photo(
        self, order_id: int, description: str, filename: str, content_type: str, encoded: str
    ) -> Any:
        description = description.strip()
        if not description:
            raise SGPError("Informe a descrição da foto.", 400)
        if content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise SGPError("Formato de imagem não permitido. Use JPG, PNG ou WebP.", 400)
        try:
            payload = encoded.split(",", 1)[-1]
            image = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            raise SGPError("A imagem enviada é inválida.", 400)
        if not image or len(image) > 8 * 1024 * 1024:
            raise SGPError("A foto deve ter no máximo 8 MB.", 413)
        order = self.order(order_id, include_onu=False)
        if not order:
            raise SGPError("Ordem de serviço não encontrada.", 404)
        if int(order.get("os_status", -1)) == 1:
            raise SGPError("Não é possível anexar foto em uma OS encerrada.", 409)
        return self.request(
            "PUT", f"/api/os/imagem/id/{order_id}/add/",
            multipart_fields={"descricao": description},
            multipart_file=(filename or "foto.jpg", content_type, image),
        )

    def stock_locations(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/api/estoque/estoque/list/")
        return result if isinstance(result, list) else []

    def products(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/api/estoque/produto/list/")
        return result if isinstance(result, list) else []

    def stock_suppliers(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/api/estoque/fornecedor/list/")
        return result if isinstance(result, list) else []

    def stock_companies(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/api/estoque/empresa/list/")
        return result if isinstance(result, list) else []

    def available_stock_report(self, location_id: int | None = None) -> dict[str, Any]:
        """Read every page of the available-balance report shown by SGP."""
        location = next((item for item in self.stock_locations()
                         if location_id is not None and int(item.get("id") or 0) == int(location_id)), None)
        if location_id is not None and not location:
            raise SGPError("Local de estoque não encontrado.", 404)
        location_name = str(location.get("descricao") or "") if location else ""
        items: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for page_number in range(1, 101):
            query = urlencode({"paginate_by": 500, "local": location_id or "", "disponivel": "on", "page": page_number})
            page = self._admin_page(f"/admin/estoque/estoque_agregado/?{query}")
            batch = _parse_admin_stock_table(page, location_name)
            fresh = 0
            for item in batch:
                key = (item.get("location"), item.get("product_id"), item.get("description"),
                       tuple(ref.get("referencia") for ref in item.get("references", [])))
                if key not in seen:
                    seen.add(key); items.append(item); fresh += 1
            if page_number > 1 and batch and not fresh:
                raise SGPError("O SGP repetiu a página do saldo; consulta incompleta.", 502)
            if not batch:
                break
            if len(batch) < 500 and not re.search(rf"[?&]page={page_number + 1}(?:[&\"'])", html.unescape(page)):
                break
        else:
            raise SGPError("O relatório de estoque excedeu 100 páginas; consulta incompleta.", 502)
        return {"items": items, "total": len(items), "location_id": location_id, "source": "sgp-saldo-disponivel"}

    @staticmethod
    def onu_search_serial(gpon: str) -> str:
        """Convert printed ZTE/Huawei IDs to the PHY address used by the OLT."""
        value = re.sub(r"[^A-Z0-9]", "", str(gpon or "").upper())
        if value.startswith("ZTEGD"):
            return "5A544547D" + value[5:]
        if value.startswith("ZTEGC"):
            return "5A544547C" + value[5:]
        if value.startswith("HWTC"):
            return "48575443" + value[4:]
        return value

    def stock_onu_status(self, gpon: str) -> dict[str, Any]:
        printed = re.sub(r"[^A-Z0-9]", "", str(gpon or "").upper())
        serial = self.onu_search_serial(printed)
        if len(serial) < 8 or len(serial) > 32:
            raise SGPError("Informe um GPON válido.", 400)
        rows = self.request("GET", "/api/fttx/onu/list/", query={"phy_addr": serial, "address": 1})
        if not isinstance(rows, list):
            raise SGPError("O SGP não confirmou a consulta de ONU ID.", 502)
        matches = [row for row in rows if isinstance(row, dict) and self.onu_search_serial(
            row.get("phy_addr") or row.get("serial") or ""
        ) == serial]
        return {
            "gpon": printed, "searched_onu_id": serial,
            "installed": bool(matches),
            "items": [{"contract_id": row.get("service_contrato") or row.get("contrato"),
                       "client": row.get("service_cliente") or row.get("cliente"),
                       "olt": row.get("olt_name") or row.get("olt_descricao"),
                       "slot": row.get("slot"), "pon": row.get("pon")}
                      for row in matches],
        }

    def _admin_page(self, path: str) -> str:
        """Open one read-only Django admin page with the current SGP credentials."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(target: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{target}", data=body, headers=request_headers)
            with opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        try:
            login_page = open_page(login_path)
            token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not token:
                raise SGPError("O SGP não liberou a consulta administrativa.", 502)
            body = urlencode({
                "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8")
            open_page("/accounts/login/", body, login_path)
            page = open_page(path, referer="/admin/")
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível abrir a tela administrativa do SGP.", 502) from exc
        if "/accounts/login" in page or "name=\"password\"" in page:
            raise SGPError("O usuário não possui acesso à tela administrativa do estoque.", 403)
        return page

    def admin_stock_for_location(
        self, location_id: int, location: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Submit the report's own filters and parse available stock only."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}
        report_path = "/admin/estoque/estoque_agregado/"

        def open_page(target: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{target}", data=body, headers=request_headers)
            with opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")

        try:
            login_path = "/accounts/login?next=/admin/"
            login_page = open_page(login_path)
            token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not token:
                raise SGPError("O SGP não liberou a consulta administrativa.", 502)
            login_body = urlencode({
                "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8")
            open_page("/accounts/login/", login_body, login_path)
            base_page = open_page(report_path, referer="/admin/")
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível abrir o relatório administrativo de estoque.", 502) from exc

        name = str(location.get("descricao") or "")
        pages = [base_page]
        discovered = self._admin_stock_filter(base_page, int(location_id), name)
        attempts = [discovered] if discovered else []
        for href, label in re.findall(
            r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", base_page, re.I | re.S,
        ):
            if "disponivel" not in _normalized(html.unescape(re.sub(r"<[^>]+>", " ", f"{label} {href}"))):
                continue
            link_fields = dict(parse_qsl(urlsplit(html.unescape(href)).query, keep_blank_values=True))
            link_fields.update(discovered)
            attempts.append(link_fields)
        attempts.extend([
            {"estoque": str(location_id), "saldo": "disponivel"},
            {"estoque_id": str(location_id), "disponivel": "1"},
            {"local": str(location_id), "tipo": "disponivel"},
        ])
        seen: set[str] = set()
        for fields in attempts:
            encoded = urlencode(fields)
            if not encoded or encoded in seen:
                continue
            seen.add(encoded)
            try:
                pages.append(open_page(f"{report_path}?{encoded}", referer=report_path))
            except (HTTPError, URLError, OSError, TimeoutError):
                continue
        for page in pages:
            items = _parse_admin_stock_table(page, name)
            if items:
                return {"location": location, "items": items, "source": "admin-estoque-agregado"}
        return None

    @staticmethod
    def _admin_stock_filter(page: str, location_id: int, location_name: str) -> dict[str, str]:
        """Discover the same filter values a user selects in the report page."""
        forms = re.findall(r"<form\b[^>]*>(.*?)</form>", page, re.I | re.S)
        form = max(forms, key=lambda value: (
            "disponivel" in _normalized(value), "estoque" in _normalized(value), len(value)
        )) if forms else page
        fields: dict[str, str] = {}
        for tag in re.findall(r"<input\b[^>]*>", form, re.I):
            name_match = re.search(r"\bname=[\"']([^\"']+)[\"']", tag, re.I)
            if not name_match:
                continue
            field = name_match.group(1)
            value_match = re.search(r"\bvalue=[\"']([^\"']*)[\"']", tag, re.I)
            value = html.unescape(value_match.group(1)) if value_match else ""
            kind_match = re.search(r"\btype=[\"']([^\"']+)[\"']", tag, re.I)
            kind = _normalized(kind_match.group(1) if kind_match else "text")
            normalized = _normalized(f"{field} {value}")
            if kind == "hidden":
                fields[field] = value
            elif "disponivel" in normalized:
                fields[field] = value or "1"
            elif kind in {"submit", "button"} and "disponivel" in _normalized(tag):
                fields[field] = value or "Disponível"
        wanted = _normalized(location_name)
        for select_match in re.finditer(r"<select\b([^>]*)>(.*?)</select>", form, re.I | re.S):
            attributes, options_html = select_match.groups()
            name_match = re.search(r"\bname=[\"']([^\"']+)[\"']", attributes, re.I)
            if not name_match:
                continue
            field = name_match.group(1)
            options = re.findall(r"<option\b([^>]*)>(.*?)</option>", options_html, re.I | re.S)
            for option_attributes, option_html in options:
                value_match = re.search(r"\bvalue=[\"']([^\"']*)[\"']", option_attributes, re.I)
                value = html.unescape(value_match.group(1)) if value_match else ""
                label = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", option_html)).split())
                normalized_label = _normalized(label)
                if str(value) == str(location_id) or (wanted and wanted in normalized_label):
                    fields[field] = value or str(location_id)
                if "disponivel" in normalized_label:
                    fields[field] = value or "disponivel"
            normalized_field = _normalized(field)
            if field not in fields and any(term in normalized_field for term in ("estoque", "local", "tecnico")):
                fields[field] = str(location_id)
        return fields

    def probe_admin_transfer_form(self, origin_id: int, destination_id: int) -> dict[str, Any]:
        """Validate the real admin transfer form without submitting anything."""
        path = "/admin/djadmin/?" + urlencode({"extra": "estoque/transferencia/add"})
        page = self._admin_page(path)
        forms = re.findall(r"<form\b[^>]*>(.*?)</form>", page, re.I | re.S)
        field_names = sorted(set(re.findall(r"\bname=[\"']([^\"']+)[\"']", page, re.I)))
        normalized = _normalized(html.unescape(re.sub(r"<[^>]+>", " ", page)))
        normalized_fields = " ".join(_normalized(value) for value in field_names)
        checks = {
            "form": bool(forms),
            "origin": any(term in f"{normalized} {normalized_fields}" for term in ("origem", "estoque de origem")),
            "destination": any(term in f"{normalized} {normalized_fields}" for term in ("destino", "estoque de destino")),
            "items": any(term in f"{normalized} {normalized_fields}" for term in ("produto", "material", "itens")),
            "origin_id_visible": bool(re.search(rf"value=[\"']{int(origin_id)}[\"']", page)),
            "destination_id_visible": bool(re.search(rf"value=[\"']{int(destination_id)}[\"']", page)),
        }
        return {
            "ready": all(checks[key] for key in ("form", "origin", "destination", "items")),
            "checks": checks, "fields": field_names[:80], "path": path,
        }

    def purchase_modem(
        self, supplier_id: int, company_id: int, stock_id: int,
        product_id: int, gpon: str, mac: str = "", *,
        validate_catalog: bool = True, check_duplicates: bool = True,
        prefer_admin_form: bool = False,
    ) -> Any:
        return self.purchase_modems(
            supplier_id, company_id, stock_id, product_id, [gpon], mac=mac,
            validate_catalog=validate_catalog, check_duplicates=check_duplicates,
            prefer_admin_form=prefer_admin_form,
        )

    def purchase_modems(
        self, supplier_id: int, company_id: int, stock_id: int,
        product_id: int, gpons: list[str], mac: str = "", *,
        validate_catalog: bool = True, check_duplicates: bool = True,
        prefer_admin_form: bool = False,
    ) -> Any:
        gpons = [str(gpon or "").strip().upper() for gpon in gpons if str(gpon or "").strip()]
        if not gpons or len(gpons) > 50 or len(set(gpons)) != len(gpons):
            raise SGPError("Informe de 1 a 50 GPONs diferentes para a compra.", 400)
        if validate_catalog:
            locations = self.stock_locations()
            stock = next((item for item in locations if int(item.get("id") or 0) == int(stock_id)), None)
            if not stock or "empresa" not in _normalized(stock.get("descricao")):
                raise SGPError("O destino da compra deve ser o estoque Empresa.", 400)
            products = self.products()
            product = next((item for item in products if int(item.get("id") or 0) == int(product_id)), None)
            if not product or not _is_modem_product(product):
                raise SGPError("Selecione um modelo de modem válido.", 400)
            suppliers = self.stock_suppliers()
            if not any(int(item.get("id") or 0) == int(supplier_id) for item in suppliers):
                raise SGPError("Fornecedor inválido.", 400)
            companies = self.stock_companies()
            if not any(int(item.get("id") or 0) == int(company_id) for item in companies):
                raise SGPError("Empresa inválida.", 400)
        if check_duplicates:
            for gpon in gpons:
                duplicate = self.request(
                    "GET", "/api/estoque/estoque_agregado_referencias/list/",
                    query={"referencia": gpon},
                )
                if isinstance(duplicate, list) and any(float(item.get("quantidade_total") or 0) > 0 for item in duplicate):
                    raise SGPError(f"O GPON {gpon} já está cadastrado em um estoque do SGP.", 409)
        payload = {
            "fornecedor": int(supplier_id),
            "empresa": int(company_id),
            "observacao": f"Compra pelo EBF Técnico · {len(gpons)} modem(ns) · GPONs " + ", ".join(gpons) + (f" · MAC {mac}" if mac else ""),
            "itens": [{
                "produto_id": int(product_id), "estoque_id": int(stock_id),
                "quantidade": len(gpons), "valor": 0, "desconto": 0,
                "referencias": gpons,
            }],
        }
        if prefer_admin_form:
            return self._purchase_modems_via_admin(
                supplier_id, company_id, stock_id, product_id, gpons, mac,
            )
        try:
            return self.request("POST", "/api/estoque/compra/create/", json_body=payload)
        except SGPError as exc:
            if exc.status == 401:
                # Some SGP profiles have the authorized Django-admin purchase
                # screen but not the separate REST write scope.  Use the same
                # official form as a fallback instead of treating that API
                # scope denial as a lack of stock permission.
                return self._purchase_modems_via_admin(
                    supplier_id, company_id, stock_id, product_id, gpons, mac,
                )
            raise

    def _purchase_modems_via_admin(
        self, supplier_id: int, company_id: int, stock_id: int,
        product_id: int, gpons: list[str], mac: str = "",
    ) -> dict[str, Any]:
        """Submit the native SGP purchase form for authorized admin users.

        The SGP REST endpoint has a permission distinct from the web purchase
        screen.  This fallback preserves the exact fields and CSRF flow of
        ``Estoque > Compra > Adicionar`` when that endpoint answers 401.
        """
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> tuple[str, str]:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=max(self.timeout, 45)) as response:
                return response.read().decode("utf-8", "replace"), response.geturl()

        login_path = "/accounts/login?next=/admin/"
        form_path = "/admin/django/estoque/compra/add/?neo_iframe=1"
        try:
            login_page, _ = open_page(login_path)
            login_token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not login_token:
                raise SGPError("O SGP não liberou a autenticação para a compra.", 502)
            open_page("/accounts/login/", urlencode({
                "csrfmiddlewaretoken": login_token.group(1),
                "username": self.credentials.username,
                "password": self.credentials.password,
                "next": "/admin/",
            }).encode("utf-8"), login_path)
            form_page, form_url = open_page(form_path, referer="/admin/")
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível abrir a compra no SGP.", 503) from exc

        if "/accounts/login" in form_url or "name=\"password\"" in form_page:
            raise SGPError("O usuário não possui acesso à tela de compra do estoque.", 403)
        csrf = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", form_page)
        required = (
            "fornecedor", "compraitens_set-0-local_estoque",
            "compraitens_set-0-produto", "compraitens_set-0-referencia",
        )
        if not csrf or not all(f'name="{name}"' in form_page or f"name='{name}'" in form_page for name in required):
            raise SGPError("O formulário de compra do SGP mudou. Nenhuma compra foi criada.", 502)

        values = {
            "csrfmiddlewaretoken": csrf.group(1),
            "fornecedor": int(supplier_id), "empresa": int(company_id),
            "nota_fiscal": "",
            "observacao": f"Compra pelo EBF Técnico · {len(gpons)} modem(ns) · GPONs " + ", ".join(gpons) + (f" · MAC {mac}" if mac else ""),
            "compraitens_set-TOTAL_FORMS": 1,
            "compraitens_set-INITIAL_FORMS": 0,
            "compraitens_set-MIN_NUM_FORMS": 0,
            "compraitens_set-MAX_NUM_FORMS": 1000,
            "compraitens_set-0-local_estoque": int(stock_id),
            "compraitens_set-0-produto": int(product_id),
            "compraitens_set-0-quantidade": len(gpons),
            "compraitens_set-0-valor_custo": "0.00",
            "compraitens_set-0-desconto": "0.00",
            "compraitens_set-0-referencia": "\n".join(gpons),
            "compraitens_set-0-id": "",
            "compraitens_set-0-compra": "",
            "_save": "Salvar",
        }
        try:
            result_page, result_url = open_page(form_path, urlencode(values).encode("utf-8"), form_path)
        except HTTPError as exc:
            raise SGPError("O SGP recusou o lançamento da compra pelo formulário administrativo.", exc.code) from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível concluir a compra no SGP. Tente novamente.", 503) from exc

        errors = re.findall(r"<ul[^>]*class=['\"][^'\"]*errorlist[^'\"]*['\"][^>]*>(.*?)</ul>", result_page, re.I | re.S)
        if errors:
            message = " ".join(
                " ".join(html.unescape(re.sub(r"<[^>]+>", " ", error)).split()) for error in errors
            )
            raise SGPError(f"O SGP não aceitou a compra: {message[:300]}", 422)
        if "/estoque/compra/add/" in result_url:
            raise SGPError("O SGP não confirmou a compra. Nenhum lançamento foi gravado.", 422)
        purchase_match = re.search(r"/estoque/compra/(\d+)/(?:change|edit)/", result_url)
        return {"id": purchase_match.group(1) if purchase_match else "", "via": "admin-form"}

    def modem_reference_location(self, gpon: str) -> dict[str, Any]:
        """Locate a serialized modem before purchasing it again."""
        needle = "".join(character for character in str(gpon or "").upper() if character.isalnum())
        balances = self.request(
            "GET", "/api/estoque/estoque_agregado_referencias/list/",
            query={"referencia": needle},
        )
        balances = balances if isinstance(balances, list) else []
        for balance in balances:
            references = _parse_references(balance.get("referencias"))
            exact = next(
                (
                    reference for reference in references
                    if "".join(character for character in str(reference.get("referencia") or "").upper() if character.isalnum()) == needle
                ),
                None,
            )
            if exact:
                if float(balance.get("quantidade_total") or 0) > 0:
                    return {
                        "kind": "stock",
                        "stock_id": _as_int(balance.get("estoque")),
                        "stock_name": str(balance.get("estoque_descricao") or "Estoque não identificado"),
                        "product_id": _as_int(balance.get("produto_estoque")),
                        "product_name": str(balance.get("produto_estoque_descricao") or "Modem"),
                        "reference": exact or {"referencia": needle},
                    }

        # Current SGP installations accept the reference as a filter on launch
        # items. This returns the exact item ID required by the reversal API.
        try:
            launch_items = self.request(
                "GET", "/api/estoque/lancamentoitem/list/",
                query={"referencia": needle},
            )
        except SGPError as error:
            if error.status not in (400, 404):
                raise
            launch_items = []
        launch_items = launch_items if isinstance(launch_items, list) else []
        for item in launch_items:
            if item.get("estornado") or item.get("aplicado") is False:
                continue
            compact = "".join(
                character for character in " ".join(str(value or "") for value in item.values()).upper()
                if character.isalnum()
            )
            if needle not in compact:
                continue
            launch_item_id = _as_int(item.get("id") or item.get("lancamentoitem_id"))
            loan = item.get("comodato_id") or item.get("comodato") or item.get("comodato_pk")
            loan_id = _as_int(loan.get("id") if isinstance(loan, dict) else loan)
            destination = _normalized(item.get("destino"))
            client_value = item.get("cliente_id") or item.get("cliente")
            client_id = _as_int(client_value.get("id") if isinstance(client_value, dict) else client_value)
            order_id = _as_int(item.get("os_id") or item.get("ordem_servico_id") or item.get("os"))
            if not client_id and order_id:
                try:
                    order = self.order(order_id, include_onu=False)
                    client_id = _as_int((order or {}).get("cliente_id"))
                except SGPError:
                    pass
            if launch_item_id and (client_id or "cliente" in destination):
                if "comodato" in _normalized(item.get("tipo") or item.get("tipo_movimentacao")) and not loan_id:
                    continue
                return {
                    "kind": "client",
                    "launch_item_id": launch_item_id,
                    "loan_id": loan_id,
                    "movement_type": "comodato" if loan_id else "os",
                    "client": str(item.get("cliente") or item.get("cliente_nome") or item.get("destino") or "Cliente"),
                    "client_id": client_id,
                    "order_id": order_id,
                    "contract_id": _as_int(item.get("clientecontrato_id") or item.get("contrato_id")),
                    "product_id": _as_int(item.get("produto_id")),
                    "product_name": str(item.get("produto_descricao") or item.get("produto") or "Modem"),
                }

        # Some versions expose the reference directly through loan items.
        try:
            loan_items = self.request(
                "GET", "/api/estoque/comodatoitens/list/",
                query={"referencia": needle},
            )
        except SGPError as error:
            if error.status not in (400, 404):
                raise
            loan_items = []
        loan_items = loan_items if isinstance(loan_items, list) else []
        for item in loan_items:
            if item.get("estornado") or item.get("aplicado") is False:
                continue
            compact = "".join(
                character for character in " ".join(str(value or "") for value in item.values()).upper()
                if character.isalnum()
            )
            if needle not in compact:
                continue
            launch_item_id = _as_int(
                item.get("lancamentoitem_id") or item.get("lancamento_item_id")
                or item.get("lancamentoitem")
            )
            loan = item.get("comodato_id") or item.get("comodato") or item.get("comodato_pk")
            loan_id = _as_int(loan.get("id") if isinstance(loan, dict) else loan)
            if launch_item_id or loan_id:
                return {
                    "kind": "client",
                    "launch_item_id": launch_item_id,
                    "loan_id": loan_id,
                    "movement_type": "comodato",
                    "client": str(item.get("cliente") or item.get("cliente_nome") or item.get("destino") or "Cliente"),
                    "client_id": _as_int(item.get("cliente_id")),
                    "contract_id": _as_int(item.get("clientecontrato_id") or item.get("contrato_id")),
                    "product_id": _as_int(item.get("produto_id") or item.get("produto")),
                    "product_name": str(item.get("produto_descricao") or item.get("produto") or "Modem"),
                }

        # The public stock API only accepts client/order filters for loan and
        # launch items. When all we have is a serial number, consult the SGP's
        # own stock history on demand and use its latest movement. This is not
        # a background poll: it runs only after a superadmin asks to purchase
        # or recover one specific modem.
        history = self._admin_reference_history(needle)
        if history:
            latest = history[0]
            destination = str(latest.get("destination") or "")
            if _normalized(destination).startswith("cliente"):
                order_id = _as_int(latest.get("order_id"))
                try:
                    order = self.order(order_id, include_onu=False) if order_id else None
                except SGPError:
                    order = None
                return {
                    "kind": "client",
                    "launch_item_id": _as_int(latest.get("id")),
                    "client": destination.split(":", 1)[-1].strip() or "Cliente",
                    "client_id": _as_int((order or {}).get("cliente_id") or (order or {}).get("client_id")),
                    "contract_id": _as_int((order or {}).get("contrato_id") or (order or {}).get("clientecontrato_id")),
                    "order_id": order_id,
                    "movement_type": str(latest.get("movement_type") or ""),
                    "product_name": str(latest.get("product_name") or "Modem"),
                }
            if _normalized(destination).startswith("estoque"):
                stock_name = destination.split(":", 1)[-1].strip()
                locations = self.stock_locations()
                stock = next(
                    (item for item in locations if _normalized(item.get("descricao")) == _normalized(stock_name)),
                    None,
                )
                if stock:
                    return {
                        "kind": "stock",
                        "stock_id": _as_int(stock.get("id")),
                        "stock_name": stock_name,
                        "product_id": _as_int(latest.get("product_id")),
                        "product_name": str(latest.get("product_name") or "Modem"),
                    }
        if balances:
            return {"kind": "existing_unknown"}
        return {"kind": "new"}

    def client_os_launch_confirmed(self, client_id: int, launch_item_id: int, gpon: str) -> bool:
        """Cross-check an OS launch against that exact customer's SGP page."""
        page = self._admin_page(f"/admin/estoque/cliente/{int(client_id)}/lancamentoavulso/list/")
        needle = re.sub(r"[^A-Z0-9]", "", str(gpon or "").upper())
        for raw_row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, re.I | re.S):
            cells = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())
                     for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", raw_row, re.I | re.S)]
            if len(cells) >= 8 and _as_int(cells[0]) == int(launch_item_id):
                reference = re.sub(r"[^A-Z0-9]", "", cells[7].upper())
                return reference.startswith(needle)
        return False

    def locate_customer_modem(self, gpon: str, customer_name: str) -> dict[str, Any]:
        """Resolve the exact customer/item named by an SGP purchase conflict."""
        needle = re.sub(r"[^A-Z0-9]", "", str(gpon).upper())
        response = self.request("POST", "/api/ura/clientes/", form={
            "nome": customer_name, "limit": 50, "omitir_titulos": "true", "omitir_contatos": "true",
        })
        customers = response.get("clientes", []) if isinstance(response, dict) else []
        for customer in customers if isinstance(customers, list) else []:
            if not isinstance(customer, dict):
                continue
            client_id = _as_int(customer.get("id") or customer.get("cliente_id"))
            actual_name = str(customer.get("nome") or customer.get("razaosocial") or "")
            if not client_id or _normalized(actual_name) != _normalized(customer_name):
                continue
            for path, loan in (("/api/estoque/comodatoitens/list/", True),
                               ("/api/estoque/lancamentoitem/list/", False)):
                try:
                    rows = self.request("GET", path, query={"cliente": client_id, "referencia": needle})
                except SGPError as error:
                    if error.status not in (400, 404):
                        raise
                    continue
                for item in rows if isinstance(rows, list) else []:
                    if not isinstance(item, dict) or item.get("estornado") or item.get("aplicado") is False:
                        continue
                    values = [item.get("referencia"), item.get("referencias")]
                    references = re.sub(r"[^A-Z0-9]", "", " ".join(str(value or "") for value in values).upper())
                    if needle not in references:
                        continue
                    item_client = _as_int(item.get("cliente_id") or item.get("cliente"))
                    if item_client and item_client != client_id:
                        continue
                    parent = item.get("comodato_id") or item.get("comodato") or item.get("comodato_pk")
                    loan_id = _as_int(parent.get("id") if isinstance(parent, dict) else parent) if loan else None
                    launch_id = _as_int(item.get("lancamentoitem_id") or item.get("lancamento_item_id")
                                        or (item.get("id") if not loan else None))
                    if loan_id:
                        return {"kind": "client", "client": actual_name, "client_id": client_id,
                                "loan_id": loan_id, "launch_item_id": launch_id, "movement_type": "comodato"}
                    if not loan and launch_id and self.client_os_launch_confirmed(client_id, launch_id, needle):
                        return {"kind": "client", "client": actual_name, "client_id": client_id,
                                "launch_item_id": launch_id, "movement_type": "os"}
            # Some SGP versions do not expose customer items through the
            # public API. The customer's own stock page includes the native
            # return link next to the exact reference.
            for page_number in range(1, 11):
                page = self._admin_page(
                    f"/admin/estoque/cliente/{client_id}/vendas/?paginate_by=1000&page={page_number}"
                )
                rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, re.I | re.S)
                for row in rows:
                    if needle not in re.sub(r"[^A-Z0-9]", "", html.unescape(row).upper()):
                        continue
                    loan_link = re.search(r"/admin/estoque/comodato/(\d+)/estorno/", row, re.I)
                    if loan_link:
                        return {"kind": "client", "client": actual_name, "client_id": client_id,
                                "loan_id": int(loan_link.group(1)), "movement_type": "comodato"}
                    launch_link = re.search(r"/admin/estoque2/lancamentoos/(\d+)/estorno/", row, re.I)
                    if launch_link and self.client_os_launch_confirmed(client_id, int(launch_link.group(1)), needle):
                        return {"kind": "client", "client": actual_name, "client_id": client_id,
                                "launch_item_id": int(launch_link.group(1)), "movement_type": "os"}
                if not re.search(rf"[?&]page={page_number + 1}(?:[&\"'])", html.unescape(page)):
                    break
        raise SGPError(
            f"O SGP informou que {needle} está com {customer_name}, mas o lançamento exato não foi confirmado. "
            "Nenhuma devolução ou compra foi feita.", 409,
        )

    def _admin_reference_history(self, reference: str) -> list[dict[str, Any]]:
        """Search SGP stock history by serial when the public API cannot."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        try:
            login_page = open_page(login_path)
            token_match = re.search(
                r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page,
            )
            if not token_match:
                return []
            login_body = urlencode({
                "csrfmiddlewaretoken": token_match.group(1),
                "username": self.credentials.username,
                "password": self.credentials.password,
                "next": "/admin/",
            }).encode("utf-8")
            open_page("/accounts/login/", login_body, login_path)
            query = urlencode({
                "referencia": reference,
                "data_inicial": "01/01/2000",
                "data_final": _business_today().strftime("%d/%m/%Y"),
                "paginate_by": 100,
            })
            page = open_page(f"/admin/estoque/lancamento_registro/?{query}")
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível conferir o histórico de estoque no SGP. Nenhuma compra foi criada.", 503) from exc
        if re.search(r'<input\b[^>]*type=["\']password["\']', page, re.I):
            raise SGPError("O SGP não liberou a conferência do histórico de estoque. Nenhuma compra foi criada.", 503)

        movements: list[dict[str, Any]] = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S):
            if reference.casefold() not in row.casefold():
                continue
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
            values = [
                " ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split())
                for cell in cells
            ]
            if len(values) < 8:
                continue
            order_match = re.search(r"/os/(\d+)/", row)
            movements.append({
                "id": _as_int(values[0]),
                "origin": values[1],
                "destination": values[2],
                "movement_type": values[3],
                "order_id": _as_int(order_match.group(1)) if order_match else None,
                "product_name": values[6],
                "reference": reference,
            })
        return movements

    def return_modem_to_company(
        self, location: dict[str, Any], company_stock_id: int, gpon: str,
        responsible_id: int | None = None,
    ) -> Any:
        kind = str(location.get("kind") or "")
        if kind == "client":
            launch_item_id = _as_int(location.get("launch_item_id"))
            loan_id = _as_int(location.get("loan_id"))
            if loan_id:
                return self._return_loan_via_admin(loan_id, company_stock_id, gpon)
            if not launch_item_id:
                raise SGPError("O lançamento do modem no cliente não foi identificado.", 409)
            try:
                return self.request(
                    "POST", "/api/estoque/lancamentoitem/estorno/",
                    json_body={
                        "lancamentoitem_id": launch_item_id,
                        "local_id": int(company_stock_id),
                        "observacao": f"Retorno ao estoque Empresa pelo EBF Técnico · GPON {gpon}",
                    },
                )
            except SGPError as exc:
                if exc.status != 401:
                    raise
                # Some SGP profiles allow the native reversal screens but not
                # the generic REST reversal scope.
                return self._return_os_launch_via_admin(launch_item_id, company_stock_id, gpon)
        if kind == "stock":
            origin_id = _as_int(location.get("stock_id"))
            product_id = _as_int(location.get("product_id"))
            if not origin_id or not product_id:
                raise SGPError("O estoque de origem do modem não foi identificado.", 409)
            return self.transfer_stock(
                origin_id, int(company_stock_id), responsible_id,
                [{"product_id": product_id, "quantity": 1, "references": [gpon]}],
                f"Retorno de modem ao estoque Empresa · GPON {gpon}",
            )
        raise SGPError("Este modem não está vinculado a cliente ou estoque para retorno.", 409)

    def _return_loan_via_admin(
        self, loan_id: int, company_stock_id: int, gpon: str,
    ) -> dict[str, Any]:
        """Submit the native ``Comodato > Devolver para estoque`` form."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> tuple[str, str]:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=max(self.timeout, 45)) as response:
                return response.read().decode("utf-8", "replace"), response.geturl()

        login_path = "/accounts/login?next=/admin/"
        path = f"/admin/estoque/comodato/{int(loan_id)}/estorno/"
        try:
            login_page, _ = open_page(login_path)
            login_token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not login_token:
                raise SGPError("O SGP não liberou a autenticação para o estorno.", 502)
            open_page("/accounts/login/", urlencode({
                "csrfmiddlewaretoken": login_token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8"), login_path)
            form_page, form_url = open_page(path, referer="/admin/")
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível abrir o estorno de comodato no SGP.", 503) from exc
        needle = re.sub(r"[^A-Z0-9]", "", str(gpon).upper())
        if "/accounts/login" in form_url or needle not in re.sub(r"[^A-Z0-9]", "", html.unescape(form_page).upper()):
            raise SGPError("O comodato não confirmou este GPON para devolução.", 409)
        csrf = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", form_page)
        local_name = re.search(r"name=['\"]([^'\"]*(?:-|^)local)['\"]", form_page, re.I)
        matching_rows = [row for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", form_page, re.I | re.S)
                         if needle in re.sub(r"[^A-Z0-9]", "", html.unescape(row).upper())]
        if len(matching_rows) != 1:
            raise SGPError("O comodato não identificou uma linha única para este GPON. Nenhuma devolução foi feita.", 409)
        selected_name = re.search(r"name=['\"]([^'\"]*(?:-|^)sel)['\"]", matching_rows[0], re.I)
        quantity_name = re.search(r"name=['\"]([^'\"]*quantidade)['\"]", matching_rows[0], re.I)
        if not csrf or not local_name or not selected_name or not quantity_name:
            raise SGPError("O formulário de estorno de comodato mudou. Nenhuma devolução foi feita.", 502)
        values = {}
        for attrs in re.findall(r"<input\b([^>]*)>", form_page, re.I | re.S):
            name = _html_attr(attrs, "name")
            field_type = (_html_attr(attrs, "type") or "text").casefold()
            if name and field_type not in ("submit", "button", "image", "checkbox", "radio"):
                values[name] = _html_attr(attrs, "value") or ""
        for attrs, value in re.findall(r"<textarea\b([^>]*)>(.*?)</textarea>", form_page, re.I | re.S):
            if name := _html_attr(attrs, "name"):
                values[name] = html.unescape(value)
        values["csrfmiddlewaretoken"] = csrf.group(1)
        values[local_name.group(1)] = str(int(company_stock_id))
        values[selected_name.group(1)] = "on"
        values[quantity_name.group(1)] = "1"
        values["form1-observacao"] = f"Retorno ao estoque Empresa pelo EBF Técnico · GPON {gpon}"
        try:
            result_page, result_url = open_page(path, urlencode(values).encode("utf-8"), path)
        except HTTPError as exc:
            raise SGPError("O SGP recusou o estorno de comodato.", exc.code) from exc
        if "nao pode ser estornado" in _normalized(html.unescape(result_page)):
            raise SGPError("O SGP informou que este comodato não pode mais ser estornado.", 409)
        if "/comodato/" in result_url and "/estorno/" in result_url:
            raise SGPError("O SGP não confirmou o estorno de comodato.", 422)
        return {"ok": True, "via": "admin-loan-reversal"}

    def _return_os_launch_via_admin(
        self, launch_item_id: int, company_stock_id: int, gpon: str,
    ) -> dict[str, Any]:
        """Use the native ``Lançamento OS > Estorno`` form as a fallback."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> tuple[str, str]:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=max(self.timeout, 45)) as response:
                return response.read().decode("utf-8", "replace"), response.geturl()

        login_path = "/accounts/login?next=/admin/"
        path = f"/admin/estoque2/lancamentoos/{int(launch_item_id)}/estorno/"
        try:
            login_page, _ = open_page(login_path)
            login_token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not login_token:
                raise SGPError("O SGP não liberou a autenticação para o estorno.", 502)
            open_page("/accounts/login/", urlencode({
                "csrfmiddlewaretoken": login_token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8"), login_path)
            form_page, form_url = open_page(path, referer="/admin/")
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível abrir o estorno no SGP.", 503) from exc

        needle = re.sub(r"[^A-Z0-9]", "", str(gpon).upper())
        if "/accounts/login" in form_url or needle not in re.sub(r"[^A-Z0-9]", "", html.unescape(form_page).upper()):
            raise SGPError("O lançamento de OS não confirmou este GPON para estorno.", 409)
        fields: dict[str, str] = {}
        for attrs in re.findall(r"<input\b([^>]*)>", form_page, re.I | re.S):
            name = _html_attr(attrs, "name")
            field_type = (_html_attr(attrs, "type") or "text").casefold()
            if name and field_type not in ("submit", "button", "image") and (field_type not in ("checkbox", "radio") or "checked" in attrs.casefold()):
                fields[name] = _html_attr(attrs, "value") or ""
        for attrs, value in re.findall(r"<textarea\b([^>]*)>(.*?)</textarea>", form_page, re.I | re.S):
            if name := _html_attr(attrs, "name"):
                fields[name] = html.unescape(value)
        for attrs, options in re.findall(r"<select\b([^>]*)>(.*?)</select>", form_page, re.I | re.S):
            name = _html_attr(attrs, "name")
            selected = re.search(r"<option\b[^>]*selected[^>]*value=['\"]?([^'\"\s>]*)", options, re.I)
            first = re.search(r"<option\b[^>]*value=['\"]?([^'\"\s>]*)", options, re.I)
            if name:
                fields[name] = (selected or first).group(1) if (selected or first) else ""
        local_fields = [name for name in fields if name.casefold().endswith("-local") or name.casefold() == "local"]
        if not fields.get("csrfmiddlewaretoken") or not local_fields:
            raise SGPError("O formulário de estorno do SGP mudou. Nenhuma devolução foi feita.", 502)
        fields[local_fields[0]] = str(int(company_stock_id))
        for name in fields:
            if "observa" in name.casefold():
                fields[name] = f"Retorno ao estoque Empresa pelo EBF Técnico · GPON {gpon}"
        try:
            result_page, result_url = open_page(path, urlencode(fields).encode("utf-8"), path)
        except HTTPError as exc:
            raise SGPError("O SGP recusou o estorno do lançamento de OS.", exc.code) from exc
        if "nao pode ser estornado" in _normalized(html.unescape(result_page)):
            raise SGPError("O SGP informou que este item não pode mais ser estornado.", 409)
        if "/lancamentoos/" in result_url and "/estorno/" in result_url:
            raise SGPError("O SGP não confirmou o estorno. Nenhuma devolução foi feita.", 422)
        return {"ok": True, "via": "admin-os-reversal"}

    def stock_for_user(self, display_name: str, username: str) -> dict[str, Any]:
        locations = self.stock_locations()
        candidates = {display_name.casefold(), username.casefold()}
        location = next(
            (item for item in locations if str(item.get("descricao", "")).casefold() in candidates),
            None,
        )
        if location is None:
            return {"location": None, "items": []}

        return self.stock_for_location(int(location["id"]), location)

    def stock_for_location(
        self, location_id: int, known_location: dict[str, Any] | None = None, *,
        prefer_admin: bool = True,
    ) -> dict[str, Any]:
        location = known_location or next((item for item in self.stock_locations() if int(item.get("id") or 0) == location_id), None)
        if location is None:
            raise SGPError("Estoque do técnico não encontrado.", 404)

        if prefer_admin and ADMIN_ENDPOINTS_ONLY:
            try:
                admin_result = self.admin_stock_for_location(location_id, location)
                if admin_result:
                    return admin_result
            except SGPError:
                if ADMIN_ENDPOINTS_ONLY:
                    raise
                # Permissions and minor HTML changes must never break stock;
                # the documented API remains the safe compatibility fallback.
                pass
            if ADMIN_ENDPOINTS_ONLY:
                raise SGPError(
                    "O relatório administrativo não devolveu saldo disponível para este estoque.", 422,
                )

        items: list[dict[str, Any]] = []
        for product in self.products():
            balances = self.request(
                "GET",
                "/api/estoque/estoque_agregado_referencias/list/",
                query={"estoque_id": location_id, "produto_id": product["id"]},
            )
            if not isinstance(balances, list):
                continue
            for balance in balances:
                quantity = float(balance.get("quantidade_total") or 0)
                if quantity <= 0:
                    continue
                references = _parse_references(balance.get("referencias"))
                items.append(
                    {
                        "product_id": product["id"],
                        "code": product.get("codigo"),
                        "description": product.get("descricao"),
                        "quantity": quantity,
                        "reference_type": product.get("tipo_referencia"),
                        "references": references,
                    }
                )
        items.sort(key=lambda item: str(item["description"]).casefold())
        return {"location": location, "items": items, "source": "api"}

    def transfer_stock(
        self,
        origin_id: int,
        destination_id: int,
        responsible_id: int | None,
        items: list[dict[str, Any]],
        observation: str = "",
    ) -> Any:
        if origin_id == destination_id:
            raise SGPError("O estoque de origem deve ser diferente do destino.", 400)
        locations = self.stock_locations()
        known_ids = {int(item.get("id") or 0) for item in locations}
        if origin_id not in known_ids or destination_id not in known_ids:
            raise SGPError("Estoque de origem ou destino não localizado.", 404)

        # Transfers still need the API's canonical product IDs and serialized
        # references until the Django form has been verified end-to-end.
        source = self.stock_for_location(origin_id, prefer_admin=False)
        available = {int(item["product_id"]): item for item in source["items"]}
        payload_items: list[dict[str, Any]] = []
        seen_products: set[int] = set()
        for raw in items:
            try:
                product_id = int(raw.get("product_id"))
                quantity = float(raw.get("quantity"))
            except (TypeError, ValueError):
                raise SGPError("Produto ou quantidade inválida na transferência.", 400)
            if product_id in seen_products:
                raise SGPError("Cada produto deve aparecer apenas uma vez na transferência.", 400)
            seen_products.add(product_id)
            if quantity <= 0 or not quantity.is_integer():
                raise SGPError("A quantidade transferida deve ser um número inteiro maior que zero.", 400)
            source_item = available.get(product_id)
            if source_item is None or quantity > float(source_item.get("quantity") or 0):
                raise SGPError("Quantidade indisponível no estoque de origem.", 409)
            references = [str(value).strip() for value in raw.get("references", []) if str(value).strip()]
            source_references = {str(ref.get("referencia")) for ref in source_item.get("references", [])}
            if source_references:
                if len(references) != int(quantity):
                    raise SGPError(f"Selecione {int(quantity)} serial(is) para {source_item.get('description')}.", 400)
                if len(set(references)) != len(references) or any(ref not in source_references for ref in references):
                    raise SGPError("Um ou mais seriais não pertencem ao estoque de origem.", 409)
            elif references:
                raise SGPError("Este produto não utiliza serial no estoque de origem.", 400)
            payload_items.append({
                "produto": product_id,
                "quantidade": int(quantity),
                "anotacao": str(raw.get("annotation") or "Reposição de técnico")[:500],
                "referencias": references,
            })
        if not payload_items:
            raise SGPError("Selecione ao menos um material para repor.", 400)
        payload: dict[str, Any] = {
            "origem": origin_id,
            "destino": destination_id,
            "observacao": observation.strip()[:1000] or "Reposição realizada pelo EBF Técnico",
            "itens": payload_items,
        }
        if responsible_id:
            payload["responsavel_envio"] = int(responsible_id)
        return self.request("POST", "/api/estoque/transferencia/create/", json_body=payload)

    def daily_usage_for_location(self, location_id: int) -> list[dict[str, Any]]:
        location = next((item for item in self.stock_locations() if int(item.get("id") or 0) == location_id), None)
        if not location:
            raise SGPError("Estoque do técnico não encontrado.", 404)
        today = date.today().isoformat()
        launches = self.request("GET", "/api/estoque/lancamento/list/", query={"tipo": 6, "data_cadastro_ini": today, "data_cadastro_fim": today})
        launches = launches if isinstance(launches, list) else []
        def fetch(launch: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
            result = self.request("GET", "/api/estoque/lancamentoitem/list/", query={"lancamento_id": launch.get("id")})
            return launch, result if isinstance(result, list) else []
        grouped: dict[str, dict[str, Any]] = {}
        needle = _normalized(location.get("descricao"))
        with ThreadPoolExecutor(max_workers=8) as pool:
            for launch, items in pool.map(fetch, launches):
                for item in items:
                    if item.get("estornado") or not item.get("aplicado", True) or needle not in _normalized(item.get("origem")):
                        continue
                    name = str(item.get("produto") or "Material não identificado")
                    bucket = grouped.setdefault(name, {"name": name, "quantity": 0.0, "entries": []})
                    quantity = float(item.get("quantidade") or 0); bucket["quantity"] += quantity
                    bucket["entries"].append({"os_id": launch.get("ordemservico_id"), "quantity": quantity, "usage": item.get("anotacao") or "Não informado", "client": str(item.get("destino") or "").removeprefix("Cliente: ").strip()})
        return sorted(({**value, "quantity": round(value["quantity"], 2)} for value in grouped.values()), key=lambda item: (-item["quantity"], item["name"].casefold()))

    def _admin_connector_launches_for_day(self, selected_day: date, end_day: date | None = None) -> list[dict[str, Any]]:
        """Read the filtered stock report used by SGP's own admin interface."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        login_page = open_page(login_path)
        token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not token:
            raise SGPError("O SGP não liberou o relatório de estoque.", 502)
        login_body = urlencode({
            "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/",
        }).encode("utf-8")
        open_page("/accounts/login/", login_body, login_path)

        rows: dict[str, dict[str, Any]] = {}
        selected_text = selected_day.strftime("%d/%m/%Y")
        end_text = (end_day or selected_day).strftime("%d/%m/%Y")
        for product_id in (3, 9):  # Conector APC Verde / Conector UPC Azul
            report_rows = _paginated_admin_rows(open_page, "/admin/estoque/lancamento_registro/", [
                ("produto", str(product_id)),
                ("tipo", "4"), ("tipo", "6"),
                ("data_inicial", selected_text), ("data_final", end_text),
            ])
            for row in report_rows:
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
                values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
                if len(values) < 11 or not values[0].isdigit():
                    continue
                order_match = re.search(r"/os/(\d+)/", row, re.I)
                order_id = _as_int(values[4]) or (_as_int(order_match.group(1)) if order_match else None)
                quantity_match = re.search(r"-?\d+(?:[.,]\d+)?", values[8])
                if not order_id or not quantity_match:
                    continue
                source_id = values[0]
                rows[source_id] = {
                    "source_id": source_id, "os_id": order_id, "origin": values[1],
                    "client": re.sub(r"^Cliente:\s*", "", values[2], flags=re.I).strip(),
                    "movement_type": values[3], "product": values[6],
                    "quantity": float(quantity_match.group(0).replace(",", ".")),
                    "used_at": values[9], "user": values[10],
                }
        return list(rows.values())

    @staticmethod
    def _report_option_value(page: str, field: str, candidates: list[str]) -> str:
        select = re.search(
            rf"<select\b[^>]*name=[\"']{re.escape(field)}[\"'][^>]*>(.*?)</select>",
            page, re.I | re.S,
        )
        if not select:
            return ""
        needles = [_normalized(value) for value in candidates if _normalized(value)]
        exact_labels = {str(value).strip() for value in candidates if str(value).strip()}
        first_names = {value.split()[0] for value in needles if value.split()}
        ranked: list[tuple[int, str]] = []
        for attributes, label_html in re.findall(r"<option\b([^>]*)>(.*?)</option>", select.group(1), re.I | re.S):
            value_match = re.search(r"\bvalue=[\"']([^\"']*)[\"']", attributes, re.I)
            value = html.unescape(value_match.group(1)) if value_match else ""
            label = _normalized(html.unescape(re.sub(r"<[^>]+>", " ", label_html)))
            if not value or not label:
                continue
            score = 0
            raw_label = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", label_html)).split())
            if raw_label in exact_labels:
                # Prefer the exact SGP login before fuzzy/display-name aliases.
                # Some installations contain duplicated users that normalize to
                # the same text (for example LEO-LIRA and Leo-lira).
                score = 120
            elif label in needles:
                score = 100
            elif any(_same_person(label, candidate) for candidate in candidates if candidate):
                score = 90
            elif any(needle and (needle in label or label in needle) for needle in needles):
                score = 80
            elif label.split()[0] in first_names:
                score = 60
            if score:
                ranked.append((score, value))
        return max(ranked, default=(0, ""))[1]

    def technician_weekly_summary(
        self, username: str, display_name: str = "", selected_day: date | None = None,
    ) -> dict[str, Any]:
        """Build the technician's Monday-Friday summary from SGP admin reports."""
        today = selected_day or _business_today()
        week_start, friday = _operational_week(today)
        aliases = [username, display_name]
        username_base = re.sub(r"[^a-zA-ZÀ-ÿ]+", " ", username).strip()
        if username_base:
            aliases.append(username_base)

        stock_path = "/admin/estoque/lancamento_registro/"
        stock_base = self._admin_page(stock_path)
        location_id = self._report_option_value(stock_base, "local", aliases)
        if not location_id:
            raise SGPError("Não encontrei o estoque deste técnico no SGP.", 422)
        start_text, end_text = week_start.strftime("%d/%m/%Y"), friday.strftime("%d/%m/%Y")
        stock_query = urlencode([
            ("paginate_by", "10000"), ("local", location_id), ("tipo", "4"), ("tipo", "6"),
            ("data_inicial", start_text), ("data_final", end_text),
        ])
        stock_page = self._admin_page(f"{stock_path}?{stock_query}")

        service_path = "/admin/atendimento/relatorios/ocorrencia/os/"
        service_base = self._admin_page(service_path)
        finalizer_id = self._report_option_value(service_base, "usuario_finaliza", aliases)
        if not finalizer_id:
            raise SGPError("Não encontrei este técnico no filtro ‘Usuário finaliza’ do SGP.", 422)
        service_query = urlencode([
            ("paginate_by", "5000"), ("status", "1"), ("usuario_finaliza", finalizer_id),
            ("data_agendamento_inicial", f"{start_text} 00:00:00"),
            ("data_agendamento_final", f"{end_text} 23:59:59"), ("crm", "NAO"),
        ])
        service_page = self._admin_page(f"{service_path}?{service_query}")

        days: dict[str, dict[str, Any]] = {}
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            days[day.isoformat()] = {
                "date": day.isoformat(), "installations": 0, "removals": 0,
                "correctives": 0, "repairs": 0, "other_services": 0,
                "devices": {"basic": 0, "giga": 0, "wifi6": 0, "mesh": 0},
                "connectors": 0.0, "drop": 0.0,
                "service_details": [], "material_details": [],
            }

        material_rows = 0
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", stock_page, re.I | re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
            values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
            if len(values) < 11 or not values[0].isdigit():
                continue
            used_at = _parse_datetime(values[9])
            if not used_at or used_at.date().isoformat() not in days:
                continue
            quantity_match = re.search(r"-?\d+(?:[.,]\d+)?", values[8])
            if not quantity_match:
                continue
            quantity = float(quantity_match.group(0).replace(",", "."))
            product = _normalized(values[6])
            bucket = days[used_at.date().isoformat()]
            material_rows += 1
            order_match = re.search(r"/os/(\d+)/", row, re.I)
            order_id = _as_int(values[4]) or (_as_int(order_match.group(1)) if order_match else None)
            references = re.findall(r"\b[A-Z0-9-]{8,32}\b", values[7].upper())
            reference = references[0] if references else ""
            bucket["material_details"].append({
                "os_id": order_id, "product": values[6], "quantity": quantity,
                "reference": reference, "gpon": re.sub(r"[^A-Z0-9]", "", reference)[-6:] if reference else "",
                "client": re.sub(r"^Cliente:\s*", "", values[2], flags=re.I).strip(),
                "movement": values[3], "used_at": used_at.isoformat(sep=" "),
            })
            if "conector" in product:
                bucket["connectors"] += quantity
            elif "drop" in product:
                bucket["drop"] += quantity
            elif "wifi 6" in product or "wifi6" in product:
                bucket["devices"]["wifi6"] += quantity
            elif "repetidor" in product or "mesh" in product or "merch" in product:
                bucket["devices"]["mesh"] += quantity
            elif "giga" in product or "250 - 600" in product:
                bucket["devices"]["giga"] += quantity
            elif any(term in product for term in ("zte basica", "onu basica", "abaixo de 350")):
                bucket["devices"]["basic"] += quantity

        service_rows = 0
        details: list[dict[str, Any]] = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", service_page, re.I | re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
            values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
            if len(values) < 12:
                continue
            order_match = re.search(r"/os/(\d+)/", row, re.I)
            order_id = _as_int(values[1] if len(values) > 1 else "") or (_as_int(order_match.group(1)) if order_match else None)
            datetimes = [_parse_datetime(value) for value in values]
            closed_at = next((value for value in reversed(datetimes) if value), None)
            if not order_id or not closed_at or closed_at.date().isoformat() not in days:
                continue
            reason = values[6] if len(values) > 6 else "Serviço"
            normalized_reason = _normalized(reason)
            bucket = days[closed_at.date().isoformat()]
            if any(term in normalized_reason for term in ("remocao", "retirada")):
                category = "removals"
            elif "instalacao" in normalized_reason:
                category = "installations"
            elif "corretiva" in normalized_reason:
                category = "correctives"
            elif "reparo" in normalized_reason:
                category = "repairs"
            else:
                category = "other_services"
            bucket[category] += 1
            service_rows += 1
            detail = {"os_id": order_id, "date": closed_at.isoformat(sep=" "), "reason": reason, "category": category}
            details.append(detail)
            bucket["service_details"].append(detail)

        for bucket in days.values():
            bucket["connectors"] = round(bucket["connectors"], 2)
            bucket["drop"] = round(bucket["drop"], 2)
            bucket["devices"] = {key: round(value, 2) for key, value in bucket["devices"].items()}
        return {
            "technician": display_name or username, "username": username,
            "start": week_start.isoformat(), "end": friday.isoformat(),
            "expires_at": datetime.combine(friday, datetime.min.time()).replace(hour=21).isoformat(),
            "days": list(days.values()), "service_rows": service_rows,
            "material_rows": material_rows, "services": details,
        }

    def launched_modems_report(
        self, location_id: int, location_name: str, selected_day: date, end_day: date | None = None,
    ) -> dict[str, Any]:
        """Validate each modem launched from a technician stock against client history."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        try:
            login_page = open_page(login_path)
            token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not token:
                raise SGPError("O SGP não liberou o relatório de materiais.", 502)
            login_body = urlencode({
                "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8")
            open_page("/accounts/login/", login_body, login_path)
            day_text = selected_day.strftime("%d/%m/%Y")
            end_text = (end_day or selected_day).strftime("%d/%m/%Y")
            query_fields = []
            if int(location_id) > 0:
                query_fields.append(("local", str(int(location_id))))
            query_fields.extend([
                ("categoria_produto", "11"), ("categoria_produto", "2"), ("categoria_produto", "1"),
                ("tipo", "4"), ("tipo", "6"),
                ("data_inicial", day_text), ("data_final", end_text),
            ])
            report_rows = _paginated_admin_rows(
                lambda path: open_page(path, referer="/admin/"),
                "/admin/estoque/lancamento_registro/", query_fields,
            )
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível abrir o relatório diário de materiais.", 502) from exc

        launches: list[dict[str, Any]] = []
        for row in report_rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
            values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
            if len(values) < 11 or not values[0].isdigit():
                continue
            order_match = re.search(r"/os/(\d+)/", row, re.I)
            order_id = _as_int(values[4]) or (_as_int(order_match.group(1)) if order_match else None)
            references = re.findall(r"\b[A-Z0-9]{8,32}\b", values[7].upper())
            reference = references[0] if references else ""
            if not order_id or len(reference) < 8:
                continue
            launches.append({
                "source_id": values[0], "order_id": order_id, "origin": values[1],
                "client": re.sub(r"^Cliente:\s*", "", values[2], flags=re.I).strip(),
                "movement": values[3], "product": values[6], "reference": reference,
                "serial_suffix": re.sub(r"[^A-Z0-9]", "", reference)[-6:],
                "used_at": values[9], "user": values[10],
            })

        order_ids = list(dict.fromkeys(int(item["order_id"]) for item in launches))
        def load_order(order_id: int) -> tuple[int, dict[str, Any]]:
            try:
                return order_id, self.order(order_id, include_onu=False) or {}
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                return order_id, {}
        orders: dict[int, dict[str, Any]] = {}
        if order_ids:
            with ThreadPoolExecutor(max_workers=min(6, len(order_ids))) as pool:
                orders = dict(pool.map(load_order, order_ids))
        for item in launches:
            order = orders.get(int(item["order_id"])) or {}
            item["client_id"] = _as_int(order.get("cliente_id") or order.get("client_id"))
            item["client"] = order.get("cliente") or item["client"]

        client_ids = list(dict.fromkeys(int(item["client_id"]) for item in launches if item.get("client_id")))
        def load_history(client_id: int) -> tuple[int, list[dict[str, Any]]]:
            try:
                page = open_page(f"/admin/cliente/{client_id}/historico/?paginate_by=1000", referer="/admin/")
            except (HTTPError, URLError, OSError, TimeoutError):
                return client_id, []
            events: list[dict[str, Any]] = []
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
                values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
                if len(values) < 5:
                    continue
                evidence, normalized = values[3], _normalized(values[3])
                serial = re.search(r"\bONU\s+([A-Z0-9-]{8,32})\b", evidence, re.I)
                at = _parse_datetime(values[4])
                if not serial or not at or "vinculad" not in normalized:
                    continue
                events.append({
                    "action": "unlink" if "desvinculad" in normalized else "link",
                    "serial": serial.group(1).upper(), "at": at,
                    "at_text": values[4], "user": values[1], "evidence": evidence,
                })
            return client_id, events
        histories: dict[int, list[dict[str, Any]]] = {}
        if client_ids:
            with ThreadPoolExecutor(max_workers=min(4, len(client_ids))) as pool:
                histories = dict(pool.map(load_history, client_ids))

        counts = {"correct": 0, "divergent": 0, "unlinked": 0, "not_confirmed": 0}
        for item in launches:
            if not item.get("client_id"):
                result = {"status": "not_confirmed", "label": "Não confirmado", "latest": None}
            else:
                result = _classify_launched_modem(
                    item["serial_suffix"], histories.get(int(item["client_id"]), []),
                    _parse_datetime(item["used_at"]),
                )
            latest = result.pop("latest", None)
            item.update(result)
            item["history_serial"] = latest.get("serial") if latest else ""
            item["history_at"] = latest.get("at_text") if latest else ""
            item["history_user"] = latest.get("user") if latest else ""
            counts[item["status"]] += 1
        return {
            "date": selected_day.isoformat(), "end_date": (end_day or selected_day).isoformat(), "location_id": int(location_id),
            "location": location_name, "total": len(launches), "counts": counts,
            "items": launches,
        }

    def daily_material_usage_report(self, selected_day: date, end_day: date | None = None) -> dict[str, Any]:
        """Read-only connector movements, retaining installations and exchanges."""
        launches = self._admin_connector_launches_for_day(selected_day, end_day)
        order_ids = list(dict.fromkeys(int(item["os_id"]) for item in launches))

        def load_order(order_id: int) -> tuple[int, dict[str, Any]]:
            try:
                return order_id, self.order(order_id, include_onu=False) or {}
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                return order_id, {}

        orders: dict[int, dict[str, Any]] = {}
        if order_ids:
            with ThreadPoolExecutor(max_workers=min(6, len(order_ids))) as pool:
                orders = dict(pool.map(load_order, order_ids))
        counts = {"return": 0.0, "exempt": 0.0, "review": 0.0}
        technicians: dict[str, dict[str, Any]] = {}
        for item in launches:
            order = orders.get(int(item["os_id"])) or {}
            os_type = str(order.get("os_motivo_descricao") or order.get("motivo") or order.get("tipo") or "").strip()
            kind = connector_classification(os_type)
            origin = re.sub(r"^Estoque:\s*", "", str(item.get("origin") or ""), flags=re.I).strip()
            technician = origin or str(order.get("os_tecnico_responsavel") or item.get("user") or "Não identificado")
            item["technician"] = technician
            item["classification"] = kind
            item["os_type"] = os_type or "Tipo da OS não informado"
            item["service_note"] = str(order.get("os_conteudo") or order.get("os_servicoprestado") or "").strip()
            quantity = float(item.get("quantity") or 0)
            counts[kind] += quantity
            summary = technicians.setdefault(technician, {"technician": technician, "return": 0.0, "exempt": 0.0, "review": 0.0})
            summary[kind] += quantity
        return {"date": selected_day.isoformat(), "end_date": (end_day or selected_day).isoformat(),
                "connectors": launches, "connector_counts": counts,
                "incomplete": any(not orders.get(order_id) for order_id in order_ids),
                "failed_orders": [order_id for order_id in order_ids if not orders.get(order_id)],
                "technicians": sorted(technicians.values(), key=lambda item: (-item["return"], item["technician"].casefold()))}

    def stock_movements_between(self, location_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Read all SGP movements for a stock between two actual balance snapshots."""
        fields = [
            ("local", str(location_id)),
            ("data_inicial", start.strftime("%d/%m/%Y")),
            ("data_final", end.strftime("%d/%m/%Y")),
        ]
        rows = _paginated_admin_rows(self._admin_page, "/admin/estoque/lancamento_registro/", fields)
        movements: list[dict[str, Any]] = []
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
            values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
            if len(values) < 11:
                raise SGPError("Relatório de movimentos incompleto: colunas não reconhecidas.", 502)
            at = _parse_datetime(values[9])
            if not at:
                raise SGPError("Relatório de movimentos incompleto: data não reconhecida.", 502)
            if not start < at <= end:
                continue
            quantity_match = re.search(r"-?\d+(?:[.,]\d+)?", values[8])
            if not quantity_match:
                raise SGPError("Relatório de movimentos incompleto: quantidade não reconhecida.", 502)
            movements.append({
                "source_id": values[0], "origin": values[1], "destination": values[2],
                "movement_type": values[3], "product": values[6],
                "quantity": abs(float(quantity_match.group(0).replace(",", "."))),
                "at": values[9], "at_iso": at.isoformat(),
                "reference": values[7], "user": values[10],
            })
        return movements

    def connector_returns_for_day(
        self, selected_day: date, closed_orders: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Classify the day's connector report by the related OS reason."""
        launches = self._admin_connector_launches_for_day(selected_day)
        order_ids = list(dict.fromkeys(int(item["os_id"]) for item in launches))

        def load_order(order_id: int) -> tuple[int, dict[str, Any]]:
            try:
                return order_id, self.order(order_id, include_onu=False) or {}
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                return order_id, {}

        orders = {int(item.get("os_id") or 0): item for item in (closed_orders or []) if item.get("os_id")}
        if order_ids and closed_orders is None:
            with ThreadPoolExecutor(max_workers=min(4, len(order_ids))) as pool:
                orders = dict(pool.map(load_order, order_ids))

        result = []
        for item in launches:
            order = orders.get(int(item["os_id"])) or {}
            if not order and closed_orders is None:
                raise SGPError("Consulta incompleta: não foi possível ler uma OS de conectores. Tente novamente.", 502)
            if closed_orders is None and int(order.get("os_status") or -1) != 1:
                continue
            reason = str(order.get("os_motivo_descricao") or order.get("motivo") or order.get("tipo") or "")
            if connector_classification(reason) != "return":
                continue
            origin = str(item.get("origin") or "").strip()
            technician = re.sub(r"^Estoque:\s*", "", origin, flags=re.I).strip() or str(item.get("user") or order.get("os_tecnico_responsavel") or "Não identificado")
            used_at_dt = _parse_datetime(item.get("used_at"))
            result.append({
                "source_id": item["source_id"], "os_id": int(item["os_id"]), "technician": technician,
                "client": order.get("cliente") or item.get("client"), "product": item.get("product") or "Conector",
                "quantity": float(item.get("quantity") or 0),
                "used_at": used_at_dt.isoformat(sep=" ") if used_at_dt else str(item.get("used_at") or ""),
                "reason": reason, "movement_type": item.get("movement_type"),
            })
        return result

    def olts(self) -> list[dict[str, Any]]:
        result = self.request("GET", "/api/fttx/olt/list/")
        return result if isinstance(result, list) else []

    def _olt_onu_inventory(self, olt_id: str, slot=None, pon=None) -> list[dict[str, Any]]:
        """Return every ONU exposed by an OLT, not only its first API page.

        The SGP endpoint returns a plain list in some installations and a
        paginated object in others.  Asking for subsequent pages is harmless
        when pagination is ignored: the stable ONU id deduplication stops the
        loop immediately instead of repeatedly reading the same first 1,000.
        """
        rows_by_id: dict[str, dict[str, Any]] = {}
        page_size = 1000
        for page_number in range(1, 101):
            # Some SGP records make address serialization return HTML instead
            # of JSON. Geometry does not need live optical polling; obtain the
            # exact postal address separately from the contract endpoint.
            query = {"limit": page_size, "page": page_number, "slot": slot, "pon": pon}
            for attempt in range(3):
                try:
                    result = self.request("GET", f"/api/fttx/olt/{int(olt_id)}/onu/list/", query=query)
                    break
                except SGPError as exc:
                    if exc.status not in (429, 500, 502, 503, 504) or attempt == 2:
                        raise SGPError(f"Falha ao consultar OLT {olt_id}, Slot {slot}, PON {pon}, página {page_number}. {exc}", exc.status) from exc
                    time.sleep(attempt + 1)
            rows: list[Any] = []
            if isinstance(result, list):
                rows = result
            elif isinstance(result, dict):
                for key in ("onus", "results", "data", "items", "rows"):
                    candidate = result.get(key)
                    if isinstance(candidate, list):
                        rows = candidate
                        break
                    if isinstance(candidate, dict):
                        nested = candidate.get("results") or candidate.get("items") or candidate.get("onus")
                        if isinstance(nested, list):
                            rows = nested
                            break
            fresh = 0
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                key = str(row.get("id") or row.get("onu_id") or row.get("phy_addr") or json.dumps(row, sort_keys=True, default=str))
                if key not in rows_by_id:
                    fresh += 1
                    rows_by_id[key] = row
            has_next = isinstance(result, dict) and bool(result.get("next") or result.get("has_next"))
            if not rows or fresh == 0 or (len(rows) < page_size and not has_next):
                break
        return list(rows_by_id.values())

    def service_coverage_map(self, complete_pons: bool = False) -> dict[str, Any]:
        """Read SGP's own online map and enrich its coordinates with ONU inventory."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/1.0"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None, ajax: bool = False) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            if ajax:
                request_headers.update({"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=max(self.timeout, 90)) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/relatorios/mapas/online/"
        login_page = open_page(login_path)
        token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not token:
            raise SGPError("O SGP não abriu a autenticação do mapa de atendimento.", 502)
        open_page("/accounts/login/", urlencode({
            "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/relatorios/mapas/online/",
        }).encode("utf-8"), login_path)

        path = "/admin/relatorios/mapas/online/"
        page = open_page(path, referer="/admin/")
        olt_options = [item for item in _report_select_options(page, "olt") if item["id"]]
        if not olt_options:
            olt_options = [{
                "id": str(item.get("id") or item.get("olt_id") or ""),
                "name": str(item.get("nome") or item.get("name") or item.get("descricao") or f"OLT {item.get('id') or ''}"),
            } for item in self.olts() if item.get("id") or item.get("olt_id")]
        contract_status_options = _report_select_options(page, "status")
        pon_catalog = []
        if complete_pons:
            search_page = open_page("/admin/cliente/search/", referer="/admin/")
            for option in _report_select_options(search_page, "oltpon"):
                match = re.search(r"OLT:\s*(.*?)\s*-\s*Slot:\s*(\d+)\s*-\s*PON:\s*(\d+)", option["name"], re.I)
                if not match:
                    continue
                olt_name, slot, pon = match.groups()
                olt = next((o for o in olt_options if _normalized(o["name"]) == _normalized(olt_name)), None)
                if olt:
                    pon_catalog.append({"olt_id": olt["id"], "olt": olt["name"], "slot": int(slot), "pon": int(pon)})
            if not pon_catalog:
                raise SGPError("O SGP não permitiu identificar o catálogo OLT PON. O mapa salvo foi preservado.", 502)
        active_status = next((item["id"] for item in contract_status_options if _normalized(item["name"]) == "ativo"), "")
        hidden_filters = {}
        for tag in re.findall(r"<input\b[^>]*>", page, re.I):
            attrs = dict(re.findall(r"([\w-]+)\s*=\s*['\"]([^'\"]*)", tag))
            if attrs.get("type", "").casefold() == "hidden" and attrs.get("name") in ("csrfmiddlewaretoken", "dpb_token"):
                hidden_filters[attrs["name"]] = attrs.get("value", "")

        default_query = {
            "pop": "", "nas": "", "oltslot": "", "pon": "", "logradouro": "", "bairro": "",
            "complemento": "", "plano": "", "tipoconexao": "", "grupo": "", "host": "",
            "ipv6en": "", "ipv6pden": "", "com_onu": "", "username": "", "tipopessoa": "",
            "sessoes_simultaneas": "", "callingstationid": "", "calledstationid": "", "nasportid": "",
            "com_onu": "sim", "status": active_status,
        }
        discovered: dict[str, dict[str, Any]] = {}
        report_failures = 0
        for olt in olt_options:
            # The map's own AJAX endpoint returns online/offline separately.
            for contract_status in (active_status,):
                page_number = 1
                while page_number <= 100:
                    query = {**hidden_filters, **default_query, "page": page_number, "olt": olt["id"], "status": contract_status}
                    try:
                        report = open_page(f"{path}?{urlencode(query)}", referer=path, ajax=True)
                    except (SGPError, OSError, TimeoutError):
                        report_failures += 1
                        break
                    try:
                        markers = _coverage_report_json(json.loads(report), olt["id"], olt["name"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        markers = _coverage_report_markers(report, olt["id"], olt["name"])
                    fresh = 0
                    for marker in markers:
                        key = str(marker.get("username") or marker.get("contract_id") or marker.get("serial") or marker["id"])
                        if key not in discovered:
                            fresh += 1
                        discovered[key] = marker
                    if not markers or fresh == 0 or not re.search(rf"(?:page=|page%3D){page_number + 1}(?:\D|$)", report, re.I):
                        break
                    page_number += 1

        clients = list(discovered.values())
        if complete_pons and report_failures:
            raise SGPError("Uma consulta de localização falhou. Consulte novamente; o mapa salvo foi preservado.", 502)
        map_count = len(clients)
        clients = [item for item in clients if _coordinates(f"{item['latitude']},{item['longitude']}")]
        if not clients:
            raise SGPError("O relatório não retornou clientes com coordenadas válidas para o mapa.", 502)

        def inventory(olt: dict[str, str]) -> tuple[str, list[dict[str, Any]]]:
            try:
                ports = [p for p in pon_catalog if p["olt_id"] == olt["id"]]
                if ports:
                    rows = []
                    for port in ports:
                        records = self._olt_onu_inventory(olt["id"], port["slot"], port["pon"])
                        rows.extend(r for r in records if str(r.get("slot")) == str(port["slot"]) and str(r.get("pon")) == str(port["pon"]))
                    return olt["id"], rows
                return olt["id"], self._olt_onu_inventory(olt["id"])
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                if complete_pons:
                    raise
                return olt["id"], []

        with ThreadPoolExecutor(max_workers=min(6, len(olt_options) or 1)) as pool:
            inventories = dict(pool.map(inventory, olt_options))
        fields = {
            "contract_id": ("service_contrato", "contrato_id", "contract_id", "contrato"),
            "serial": ("phy_addr", "serial"),
            "onu_id": ("id", "onu_id"),
            "username": ("service_login", "username", "login"),
            "client_id": ("service_cliente_id", "cliente_id", "client_id"),
        }
        indexes = {}
        global_indexes = {field: {} for field in fields}
        for olt_id, rows in inventories.items():
            index = {field: {} for field in fields}
            for row in rows:
                row = dict(row)
                row["_coverage_olt_id"] = olt_id
                for field, aliases in fields.items():
                    value = next((str(row[a]) for a in aliases if row.get(a) is not None and str(row[a])), "")
                    if value:
                        index[field].setdefault(value, []).append(row)
                        global_indexes[field].setdefault(value, []).append(row)
            indexes[olt_id] = index
        matched = 0
        for item in clients:
            index = global_indexes
            match = None
            for field in fields:
                candidates = index.get(field, {}).get(str(item.get(field) or ""), [])
                if len(candidates) == 1:
                    match = candidates[0]
                    break
            # The SGP map feed can omit or rename the OLT id.  The contract,
            # serial or ONU id remains globally unique in normal operation, so
            # use that as a safe fallback rather than discarding its Slot/PON.
            if not match:
                for field in fields:
                    candidates = indexes.get(str(item.get("olt_id")), {}).get(field, {}).get(str(item.get(field) or ""), [])
                    if len(candidates) == 1:
                        match = candidates[0]
                        item["olt_id"] = str(match.get("olt") or match.get("olt_id") or item.get("olt_id"))
                        break
            if match:
                matched += 1
                actual_olt = str(match.get("_coverage_olt_id") or item.get("olt_id"))
                item.update({
                    "olt_id": actual_olt,
                    "olt": next((olt["name"] for olt in olt_options if str(olt["id"]) == actual_olt), item.get("olt")),
                    "contract_id": item.get("contract_id") or _as_int(match.get("service_contrato") or match.get("contrato_id")),
                    "online": _onu_connectivity(match),
                    "serial": item.get("serial") or match.get("phy_addr") or match.get("serial"),
                    "slot": item.get("slot") if item.get("slot") is not None else _as_int(match.get("slot")),
                    "pon": item.get("pon") if item.get("pon") is not None else _as_int(match.get("pon")),
                    "signal": item.get("signal") or match.get("signal") or match.get("info_rx") or match.get("rx"),
                    "onu_id": match.get("id") or match.get("onu_id"),
                    "address": item.get("address") or match.get("address") or match.get("endereco") or match.get("service_address"),
                })
            item.setdefault("contract_status", None)
            item["network_status"] = "online" if item.get("online") is True else "offline" if item.get("online") is False else "unknown"

        groups: dict[tuple[str, int, int], dict[str, Any]] = {}
        for port in pon_catalog:
            groups[(str(port["olt_id"]), port["slot"], port["pon"])] = {
                **port, "total": 0, "offline": 0, "unknown": 0, "clients": [], "points": [], "_latitudes": [], "_longitudes": [],
            }
        for item in clients:
            if item.get("slot") is None or item.get("pon") is None:
                continue
            key = (str(item.get("olt_id")), int(item["slot"]), int(item["pon"]))
            group = groups.setdefault(key, {
                "olt_id": key[0], "olt": item.get("olt"), "slot": key[1], "pon": key[2],
                "total": 0, "offline": 0, "unknown": 0, "clients": [], "points": [], "_latitudes": [], "_longitudes": [],
            })
            group["total"] += 1
            group["points"].append({"latitude": float(item["latitude"]), "longitude": float(item["longitude"]), "address": item.get("address")})
            if item["network_status"] == "unknown":
                group["unknown"] += 1
            if item["network_status"] == "offline":
                group["offline"] += 1
                group["clients"].append({"client": item.get("client"), "contract_id": item.get("contract_id")})
                group["_latitudes"].append(float(item["latitude"]))
                group["_longitudes"].append(float(item["longitude"]))
        group_rows = []
        for group in groups.values():
            group["offline_percent"] = round((group["offline"] / group["total"] * 100) if group["total"] else 0, 1)
            group["possible_outage"] = group["offline"] >= 10
            group["latitude"] = sum(group.pop("_latitudes")) / group["offline"] if group["offline"] else None
            group["longitude"] = sum(group.pop("_longitudes")) / group["offline"] if group["offline"] else None
            group_rows.append(group)
        group_rows.sort(key=lambda item: (-item["offline"], str(item.get("olt") or ""), item["slot"], item["pon"]))
        offline = sum(1 for item in clients if item["network_status"] == "offline")
        online = sum(1 for item in clients if item["network_status"] == "online")
        unknown = len(clients) - online - offline
        diagnostics = {"map_clients": map_count, "with_coordinates": len(clients), "onus": sum(map(len, inventories.values())), "matched": matched, "online": online, "offline": offline, "unknown": unknown, "olts": len(olt_options)}
        logging.getLogger(__name__).warning("Mapa Atendimento: %s", diagnostics)
        return {
            "diagnostics": diagnostics,
            "warnings": ([f"Mapa carregado. Não foi possível determinar o status de {unknown} clientes."] if unknown else []) + ([f"Consulta parcial: {report_failures} páginas de localização não puderam ser consultadas."] if report_failures else []),
            "clients": clients, "olts": olt_options,
            "contract_status_options": contract_status_options,
            "groups": group_rows, "outages": [item for item in group_rows if item["possible_outage"]],
            "total": len(clients), "online": online, "offline": offline, "unknown": len(clients) - online - offline,
            "offline_percent": round((offline / len(clients) * 100) if clients else 0, 1),
            "source": "SGP / Relatórios / Mapas / Online", "coverage_radius_m": 50,
            "generated_at": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(),
        }

    def unauthorized_onus(self, olt_id: int) -> list[dict[str, Any]]:
        result = self.request("GET", f"/api/fttx/olt/{olt_id}/unauth/")
        return result if isinstance(result, list) else []

    def find_onu_by_suffix(self, suffix: str) -> list[dict[str, Any]]:
        """Find linked ONUs by the last six characters of their PHY address."""
        suffix = re.sub(r"[^A-Za-z0-9]", "", str(suffix or "")).upper()
        if len(suffix) != 6:
            raise SGPError("Informe exatamente os 6 últimos caracteres do modem.", 400)
        result = self.request(
            "GET", "/api/fttx/onu/list/",
            query={"phy_addr": suffix, "address": 1, "connection": 1},
        )
        rows = result if isinstance(result, list) else []
        def serial_of(row: dict[str, Any]) -> str:
            return str(
                row.get("phy_addr") or row.get("serial") or row.get("gpon") or
                row.get("mac") or row.get("onu_sn") or row.get("id") or ""
            ).strip().upper()

        matches = [row for row in rows if serial_of(row).endswith(suffix)]

        def enrich(row: dict[str, Any]) -> dict[str, Any]:
            contract_id = row.get("service_contrato") or row.get("contrato") or row.get("contrato_id")
            client = row.get("service_cliente") or row.get("cliente") or "Cliente não identificado"
            cpf = row.get("cpfcnpj") or row.get("cliente_cpfcnpj") or ""
            client_id = row.get("cliente_id") or row.get("service_cliente_id")
            if contract_id and (not cpf or not client_id):
                try:
                    payload = self.request(
                        "POST", "/api/ura/clientes/",
                        form={"contrato": contract_id, "limit": 5, "exibir_conexao": "true", "omitir_titulos": "true"},
                    )
                    customers = payload.get("clientes", []) if isinstance(payload, dict) else []
                    customer = customers[0] if customers else {}
                    client = customer.get("nome") or customer.get("razaosocial") or client
                    cpf = customer.get("cpfcnpj") or customer.get("cpf_cnpj") or cpf
                    client_id = customer.get("id") or customer.get("cliente_id") or client_id
                except (SGPError, OSError, TimeoutError, ValueError):
                    pass
            return {
                "onu_id": row.get("id") or row.get("onuid"),
                "serial": serial_of(row),
                "olt_id": row.get("olt_id") or row.get("olt"),
                "olt": row.get("olt_name") or row.get("olt_descricao") or "OLT não informada",
                "slot": row.get("slot"), "pon": row.get("pon"),
                "client_id": client_id, "client": client, "cpf": cpf,
                "contract_id": contract_id,
                "status": row.get("status_txt") or row.get("status"),
                "online": row.get("online") if row.get("online") is not None else row.get("connection"),
            }

        with ThreadPoolExecutor(max_workers=min(6, len(matches) or 1)) as pool:
            return list(pool.map(enrich, matches))

    def find_onu_by_suffix_detailed(self, suffix: str) -> dict[str, Any]:
        """Search linked ONUs even though the API only accepts a full PHY address."""
        clean = re.sub(r"[^A-Za-z0-9]", "", str(suffix or "")).upper()
        if len(clean) != 6:
            raise SGPError("Informe exatamente os 6 últimos caracteres do modem.", 400)

        direct = self.find_onu_by_suffix(clean)
        if direct:
            return {"items": direct, "suffix": clean, "mode": "global-filter", "olts_scanned": 0, "onus_checked": len(direct)}

        linked, checked = self._admin_find_linked_onu_by_suffix(clean)
        if linked:
            original_request = self.request
            try:
                self.request = lambda method, path, **kwargs: linked if path == "/api/fttx/onu/list/" else original_request(method, path, **kwargs)  # type: ignore[method-assign]
                enriched = self.find_onu_by_suffix(clean)
            finally:
                self.request = original_request  # type: ignore[method-assign]
            return {
                "items": enriched, "suffix": clean, "mode": "linked-registry",
                "olts_scanned": 0, "onus_checked": checked,
            }

        olts = self.olts()

        def scan_olt(olt: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
            olt_id = olt.get("id")
            if olt_id in (None, ""):
                return olt, [], 0
            try:
                rows = self.request(
                    "GET", f"/api/fttx/olt/{int(olt_id)}/onu/list/",
                    query={"address": 1, "connection": 1},
                )
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                return olt, [], 0
            values = [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []
            found = [item for item in values if str(
                item.get("phy_addr") or item.get("serial") or item.get("gpon") or
                item.get("mac") or item.get("onu_sn") or item.get("id") or ""
            ).strip().upper().endswith(clean)]
            for item in found:
                item.setdefault("olt_id", olt_id)
                item.setdefault("olt_name", olt.get("name") or olt.get("nome") or olt.get("descricao"))
            return olt, found, len(values)

        scanned: list[tuple[dict[str, Any], list[dict[str, Any]], int]] = []
        with ThreadPoolExecutor(max_workers=min(4, len(olts) or 1)) as pool:
            scanned.extend(pool.map(scan_olt, olts))
        matches = [item for _olt, found, _count in scanned for item in found]

        # Reuse the normal enrichment path without issuing another 9k-device scan.
        original_request = self.request
        try:
            self.request = lambda method, path, **kwargs: matches if path == "/api/fttx/onu/list/" else original_request(method, path, **kwargs)  # type: ignore[method-assign]
            enriched = self.find_onu_by_suffix(clean)
        finally:
            self.request = original_request  # type: ignore[method-assign]
        return {
            "items": enriched, "suffix": clean, "mode": "all-olts",
            "olts_scanned": len(olts), "onus_checked": sum(count for _olt, _found, count in scanned),
        }

    def _admin_find_linked_onu_by_suffix(self, suffix: str) -> tuple[list[dict[str, Any]], int]:
        """Read SGP's linked-ONU registry, which includes offline equipment.

        The public ONU API treats ``phy_addr`` as an exact value.  The native
        report is therefore paged in bounded batches and stopped as soon as a
        matching suffix is found.  No command is sent to any OLT.
        """
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=max(self.timeout, 45)) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        login_page = open_page(login_path)
        token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not token:
            raise SGPError("O SGP não liberou a consulta de modens vinculados.", 502)
        login_body = urlencode({
            "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/",
        }).encode("utf-8")
        open_page("/accounts/login/", login_body, login_path)

        checked = 0
        page_number = 1
        last_page = 1
        matches: list[dict[str, Any]] = []
        while page_number <= last_page and page_number <= 20:
            query = urlencode({"paginate_by": 1000, "page": page_number})
            page = open_page(f"/admin/network/onu/list/?{query}")
            page_match = re.search(r"P[aá]gina\s+\d+\s+de\s+(\d+)", page, re.I)
            if page_match:
                last_page = min(20, max(1, int(page_match.group(1))))
            for raw_row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", raw_row, re.I | re.S)
                values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
                if len(values) < 10:
                    continue
                serial_match = re.search(r"\b[A-Z0-9]{12,32}\b", values[8], re.I)
                if not serial_match:
                    continue
                checked += 1
                serial = serial_match.group(0).upper()
                if not serial.endswith(suffix):
                    continue
                service_match = re.search(r"/admin/servicos/internet/(\d+)/", raw_row, re.I)
                onu_match = re.search(r"(?:doc|/onu/)(\d+)", raw_row, re.I)
                contract_match = re.search(r"\b(\d+)\s*-", values[9])
                matches.append({
                    "id": int(onu_match.group(1)) if onu_match else None,
                    "phy_addr": serial,
                    "olt_name": values[1], "slot": _as_int(values[2]), "pon": _as_int(values[3]),
                    "onu": _as_int(values[4]),
                    "service_contrato": int(contract_match.group(1)) if contract_match else None,
                    "service_id": int(service_match.group(1)) if service_match else None,
                    "description": values[10] if len(values) > 10 else "",
                    "online": False,
                })
            if matches:
                break
            page_number += 1
        return matches, checked

    def _admin_suspended_financial_contracts(self) -> list[dict[str, Any]]:
        """Read current financially-suspended contracts from SGP's status map."""
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=max(self.timeout, 60)) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        login_page = open_page(login_path)
        login_token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not login_token:
            raise SGPError("O SGP não liberou o relatório de contratos suspensos.", 502)
        open_page("/accounts/login/", urlencode({
            "csrfmiddlewaretoken": login_token.group(1), "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/",
        }).encode("utf-8"), login_path)
        path = "/admin/relatorios/mapas/contrato/status/"
        page = open_page(path)
        csrf = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", page)
        dpb = re.search(r"name=['\"]dpb_token['\"][^>]+value=['\"]([^'\"]*)", page, re.I)
        if not csrf:
            raise SGPError("O SGP não abriu os filtros de suspensão.", 502)
        report = open_page(path, urlencode({
            "csrfmiddlewaretoken": csrf.group(1), "dpb_token": dpb.group(1) if dpb else "",
            "tipo_pesquisa": "0", "status": "4", "status_motivo": "2", "crm": "NAO",
        }).encode("utf-8"), path)
        results: dict[int, dict[str, Any]] = {}
        pattern = re.compile(
            r"var\s+marker(?P<contract>\d+)\s*=.*?position:\s*\{lat:\s*(?P<lat>-?[\d.]+),\s*lng:\s*(?P<lng>-?[\d.]+).*?"
            r"contentString(?P=contract)\s*=.*?href=[\"']?/admin/cliente/(?P<client>\d+)/contratos/[\"']?[^>]*>\s*"
            r"(?P=contract)\s*-\s*(?P<name>.*?)\s*</a>.*?Data:\s*(?P<status_date>\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}).*?Status:\s*Suspenso",
            re.I | re.S,
        )
        for match in pattern.finditer(report):
            contract_id = int(match.group("contract"))
            results[contract_id] = {
                "contract_id": contract_id, "client_id": int(match.group("client")),
                "client": " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group("name"))).split()),
                "latitude": float(match.group("lat")), "longitude": float(match.group("lng")),
                "suspended_at": match.group("status_date"),
            }
        return list(results.values())

    def _withdrawal_contract_detail(self, contract_id: int, minimum_days: int = 40) -> dict[str, Any] | None:
        payload = self.request("POST", "/api/ura/clientes/", form={
            "contrato": int(contract_id), "limit": 5, "exibir_conexao": "true",
        })
        customers = payload.get("clientes", []) if isinstance(payload, dict) else []
        customer = customers[0] if customers else {}
        contracts = customer.get("contratos", []) if isinstance(customer, dict) else []
        contract = next(
            (item for item in contracts if int(item.get("id") or item.get("contrato") or 0) == int(contract_id)),
            contracts[0] if contracts else {},
        )
        contract_status = _normalized(contract.get("status"))
        if contract_status not in ("4", "suspenso", "suspensa"):
            return None
        titles = customer.get("titulos", []) if isinstance(customer, dict) else []
        overdue = [
            title for title in titles if int(title.get("clientecontrato_id") or 0) == int(contract_id)
            and _normalized(title.get("status")) in ("1", "aberto", "aberta", "pendente")
            and int(title.get("diasAtraso") or 0) > int(minimum_days)
        ]
        if not overdue:
            return None
        services = contract.get("servicos", []) if isinstance(contract, dict) else []
        service = next(
            (item for item in services if isinstance(item.get("onu"), dict) and item.get("onu")),
            None,
        )
        if not service:
            return None
        onu = service.get("onu") or {}
        address = contract.get("endereco") or customer.get("endereco") or {}
        oldest = max(overdue, key=lambda item: int(item.get("diasAtraso") or 0))
        latitude = address.get("latitude")
        longitude = address.get("longitude")
        return {
            "contract_id": int(contract_id), "client_id": int(customer.get("id") or 0),
            "client": customer.get("nome") or "Cliente não informado", "cpf": customer.get("cpfcnpj") or "",
            "service_id": service.get("id"), "service_login": service.get("login"),
            "serial": onu.get("serial") or onu.get("phy_addr") or service.get("serial"),
            "olt": onu.get("olt_nome") or onu.get("olt_name"), "slot": onu.get("slot"), "pon": onu.get("pon"),
            "days_overdue": int(oldest.get("diasAtraso") or 0), "oldest_due_date": oldest.get("dataVencimento"),
            "address": _address_text(address), "latitude": latitude, "longitude": longitude,
        }

    @staticmethod
    def _nearest_withdrawal_technician(item: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, Any] | None:
        try:
            lat1, lon1 = math.radians(float(item["latitude"])), math.radians(float(item["longitude"]))
        except (KeyError, TypeError, ValueError):
            return None
        best = None
        for point in positions:
            try:
                lat2, lon2 = math.radians(float(point["latitude"])), math.radians(float(point["longitude"]))
            except (KeyError, TypeError, ValueError):
                continue
            value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
            distance = 6371 * 2 * math.asin(math.sqrt(value))
            if best is None or distance < best[0]:
                best = (distance, point)
        if not best:
            return None
        return {"technician": best[1].get("technician"), "distance_km": round(best[0], 1)}

    def withdrawal_candidates(self, minimum_days: int = 40) -> dict[str, Any]:
        bases = self._admin_suspended_financial_contracts()
        open_contracts: set[int] = set()
        route_orders: list[dict[str, Any]] = []
        for status in (0, 2, 3):
            rows = self._ura_order_pages({"status": status, "limit": 1000})
            for row in rows:
                contract_id = _as_int(row.get("contrato"))
                if contract_id:
                    open_contracts.add(contract_id)
                route_orders.append({
                    "os_id": row.get("id"), "os_status": row.get("status_id"),
                    "os_tecnico_responsavel": row.get("responsavel"), "cliente": row.get("cliente"),
                    "os_data_agendamento": f"{row.get('data_agendamento') or ''} {row.get('hora_agendamento') or ''}".strip(),
                })
        pending = [item for item in bases if int(item["contract_id"]) not in open_contracts]

        def load(base: dict[str, Any]) -> dict[str, Any] | None:
            try:
                detail = self._withdrawal_contract_detail(int(base["contract_id"]), minimum_days)
                return {**base, **detail} if detail else None
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                return None

        with ThreadPoolExecutor(max_workers=min(16, len(pending) or 1)) as pool:
            items = [item for item in pool.map(load, pending) if item]
        positions = self.route_monitor_positions(route_orders)
        for item in items:
            item["suggestion"] = self._nearest_withdrawal_technician(item, positions)
        items.sort(key=lambda item: (-int(item.get("days_overdue") or 0), str(item.get("client") or "").casefold()))
        return {
            "items": items, "total": len(items), "minimum_days": minimum_days,
            "suspended_checked": len(bases), "excluded_open_os": len([item for item in bases if int(item["contract_id"]) in open_contracts]),
            "technicians": [{"name": name, "id": value} for name, value in WITHDRAWAL_TECHNICIANS.items()],
            "generated_at": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(),
        }

    def _order_assignment_session(self):
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        def open_page(path, body=None):
            request = Request(self.base_url + path, data=body, headers={
                "User-Agent": "EBF-Tecnico/0.1", "Referer": self.base_url + path,
            })
            with opener.open(request, timeout=min(self.timeout, 30)) as response:
                page = response.read().decode("utf-8", "replace")
                if body is None and "/accounts/login" in response.geturl() and not path.startswith("/accounts/login"):
                    raise SGPError("Entre no SGP com um usuário autorizado a editar OS.", 403)
                return page
        login = open_page("/accounts/login?next=/admin/")
        from .order_assignment import Forms
        tokens = [dict(f["fields"]).get("csrfmiddlewaretoken") for f in Forms(login).forms]
        token = next((value for value in tokens if value), None)
        if not token:
            raise SGPError("Não foi possível autenticar a edição da OS no SGP.", 502)
        open_page("/accounts/login/", urlencode({
            "csrfmiddlewaretoken": token, "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/",
        }).encode())
        return open_page

    def order_assignment(self, order_id: int, technician_id=None, expected_revision=None):
        from .order_assignment import order_form, revision
        if int(order_id) <= 0:
            raise SGPError("OS inválida.", 400)
        path = f"/admin/atendimento/ocorrencia/os/{int(order_id)}/edit/"
        submitted = False
        try:
            open_page = self._order_assignment_session()
            form = order_form(open_page(path))
            fields = dict(form["fields"])
            if form["action"] not in ("", path):
                raise SGPError("O formulário da OS mudou. Operação interrompida para conferência.", 502)
            if fields.get("status") not in ("0", "2", "3"):
                raise SGPError("Somente OS abertas, em execução ou pendentes podem ser direcionadas.", 409)
            options = [{"id": o["value"], "name": o["label"]}
                       for o in form["options"]["responsavel"] if o["value"].isdigit() and not o["disabled"]]
            before = fields.get("responsavel", "")
            result = {"order_id": int(order_id), "technicians": options, "technician_id": before,
                      "revision": revision(form), "scheduled_at": fields.get("data_agendamento", "")}
            if technician_id is None:
                return result
            destination = str(technician_id)
            technician = next((o for o in options if o["id"] == destination), None)
            if not technician:
                raise SGPError("Selecione um técnico disponível nesta OS no SGP.", 400)
            if not expected_revision or expected_revision != result["revision"]:
                raise SGPError("A OS foi alterada no SGP. Feche e abra novamente a seleção de técnico.", 409)
            if destination != before:
                payload = [(k, destination if k == "responsavel" else v) for k, v in form["fields"]]
                submitted = True
                response = open_page(path, urlencode(payload).encode())
                if re.search(r'class=[\"\'][^\"\']*errorlist', response, re.I):
                    raise SGPError("O SGP recusou a alteração da OS. Confira o formulário no SGP antes de repetir.", 422)
                confirmed = order_form(open_page(path))
                if dict(confirmed["fields"]).get("responsavel") != destination:
                    raise SGPError("A atribuição não foi confirmada. Confira a OS no SGP antes de repetir.", 502)
            return {"order_id": int(order_id), "previous_technician_id": before,
                    "technician_id": destination, "technician": technician["name"], "confirmed": True}
        except ValueError as exc:
            raise SGPError("Não foi possível confirmar a atribuição. Confira a OS no SGP antes de repetir." if submitted else str(exc), 502) from exc
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Sem confirmação do SGP. Confira a OS antes de repetir a atribuição." if submitted
                           else "Não foi possível carregar os técnicos da OS no SGP.", 502) from exc

    def create_withdrawal_order(self, contract_id: int, technician_id: int, opened_by: str = "Gustavo") -> dict[str, Any]:
        technician_name = next((name for name, value in WITHDRAWAL_TECHNICIANS.items() if value == int(technician_id)), None)
        if not technician_name:
            raise SGPError("Selecione um técnico válido.", 400)
        for status in (0, 2, 3):
            if any(_as_int(row.get("contrato")) == int(contract_id) for row in self._ura_order_pages({"status": status, "limit": 1000})):
                raise SGPError("Este cliente já possui uma OS aberta, em execução ou pendente.", 409)
        detail = self._withdrawal_contract_detail(int(contract_id), 40)
        if not detail:
            raise SGPError("O contrato não atende mais aos critérios da retirada.", 409)

        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}
        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> tuple[str, str]:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=max(self.timeout, 45)) as response:
                return response.read().decode("utf-8", "replace"), response.geturl()
        login_path = "/accounts/login?next=/admin/"
        login_page, _ = open_page(login_path)
        login_token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not login_token:
            raise SGPError("O SGP não liberou a abertura da retirada.", 502)
        open_page("/accounts/login/", urlencode({
            "csrfmiddlewaretoken": login_token.group(1), "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/",
        }).encode("utf-8"), login_path)
        client_id = int(detail["client_id"])
        occurrence_path = f"/admin/atendimento/cliente/{client_id}/ocorrencia/add/"
        occurrence_page, _ = open_page(occurrence_path)
        csrf = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", occurrence_page)
        dpb = re.search(r"name=['\"]dpb_token['\"][^>]+value=['\"]([^'\"]*)", occurrence_page, re.I)
        number_match = re.search(r"name=['\"]numero['\"][^>]+value=['\"]([^'\"]+)", occurrence_page, re.I)
        if not csrf:
            raise SGPError("O formulário de ocorrência não foi carregado.", 502)
        now_text = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y %H:%M:%S")
        occurrence_body = urlencode({
            "csrfmiddlewaretoken": csrf.group(1), "dpb_token": dpb.group(1) if dpb else "",
            "numero": number_match.group(1) if number_match else "", "clientecontrato": int(contract_id),
            "servico": int(detail.get("service_id") or 0), "setor": 2, "responsavel": 40,
            "tipo": 9, "metodo": 3, "status": 0, "conteudo": "RETIRADA DE APARELHO",
            "observacoes": f"Aberta pelo app EBF Técnico por {opened_by}.", "data_agendamento": now_text,
        }).encode("utf-8")
        occurrence_result, occurrence_url = open_page(occurrence_path, occurrence_body, occurrence_path)
        occurrence_match = re.search(r"/ocorrencia/(\d+)/edit/", occurrence_url)
        if not occurrence_match:
            errors = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split() for value in re.findall(r"<ul class=['\"]errorlist['\"]>(.*?)</ul>", occurrence_result, re.I | re.S))
            raise SGPError(f"O SGP não criou a ocorrência. {errors[:300]}", 422)
        occurrence_id = int(occurrence_match.group(1))
        os_path = f"/admin/atendimento/ocorrencia/{occurrence_id}/os/add/"
        os_page, _ = open_page(os_path)
        os_csrf = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", os_page)
        os_dpb = re.search(r"name=['\"]dpb_token['\"][^>]+value=['\"]([^'\"]*)", os_page, re.I)
        if not os_csrf:
            raise SGPError(f"A ocorrência #{occurrence_id} foi criada, mas o formulário da OS não abriu.", 502)
        os_body = urlencode({
            "csrfmiddlewaretoken": os_csrf.group(1), "dpb_token": os_dpb.group(1) if os_dpb else "",
            "tipoos": 1, "motivoos": 2, "prioridade": 2, "data_agendamento": now_text,
            "setor": 2, "responsavel": int(technician_id), "conteudo": "RETIRADA DE APARELHO",
            "servicoprestado": "", "observacao": f"Aberta pelo app EBF Técnico por {opened_by}.",
        }).encode("utf-8")
        os_result, os_url = open_page(os_path, os_body, os_path)
        os_match = re.search(r"/ocorrencia/os/(\d+)/edit/", os_url)
        if not os_match:
            errors = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split() for value in re.findall(r"<ul class=['\"]errorlist['\"]>(.*?)</ul>", os_result, re.I | re.S))
            raise SGPError(f"A ocorrência #{occurrence_id} foi criada, mas a OS falhou. {errors[:250]}", 422)
        order_id = int(os_match.group(1))
        return {
            "occurrence_id": occurrence_id, "order_id": order_id, "contract_id": int(contract_id),
            "client": detail.get("client"), "technician": technician_name,
            "notice": f"Ocorrência #{occurrence_id} e OS #{order_id} abertas para {technician_name} pelo app.",
        }

    def onu_link_client(self, client_id: int, contract_id: int | None = None) -> dict[str, Any]:
        """Load ONU auth fields from either a customer ID or a contract ID."""
        identifier = int(contract_id if contract_id is not None else client_id)

        def find_customer(field: str) -> tuple[dict[str, Any] | None, int | None]:
            result = self.request(
                "POST", "/api/ura/clientes/",
                form={
                    field: identifier, "limit": 20, "exibir_conexao": "true",
                    "omitir_titulos": "true", "omitir_contatos": "true",
                },
            )
            customers = result.get("clientes", []) if isinstance(result, dict) else []
            if field == "cliente":
                match = next(
                    (item for item in customers if int(item.get("id") or item.get("cliente_id") or 0) == identifier),
                    customers[0] if len(customers) == 1 else None,
                )
                return (match if isinstance(match, dict) else None), None
            for item in customers:
                if not isinstance(item, dict):
                    continue
                contracts = item.get("contratos") if isinstance(item.get("contratos"), list) else []
                if any(int(contract.get("contrato") or contract.get("id") or contract.get("clientecontrato_id") or 0) == identifier for contract in contracts):
                    return item, identifier
            return None, None

        if contract_id is not None:
            customer, requested_contract = find_customer("contrato")
        else:
            customer, requested_contract = find_customer("cliente")
            if customer is None:
                customer, requested_contract = find_customer("contrato")
        if not isinstance(customer, dict):
            raise SGPError("Cliente ou contrato não localizado no SGP.", 404)
        contracts = customer.get("contratos") if isinstance(customer.get("contratos"), list) else []
        if requested_contract is not None:
            contracts = [
                contract for contract in contracts
                if int(contract.get("contrato") or contract.get("id") or contract.get("clientecontrato_id") or 0) == requested_contract
            ]
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for contract in contracts:
            services = contract.get("servicos") if isinstance(contract.get("servicos"), list) else []
            for service in services:
                if isinstance(service, dict):
                    candidates.append((contract, service))
        if not candidates:
            raise SGPError("O cliente não possui contrato e serviço disponíveis para vincular a ONU.", 422)
        contract, service = next(
            ((contract, service) for contract, service in candidates if not service.get("onu")),
            candidates[0],
        )
        address = contract.get("endereco") or customer.get("endereco") or {}
        login = service.get("login") or service.get("servico_login") or service.get("username") or ""
        linked_onu = service.get("onu")
        linked_onu_id = (
            linked_onu.get("phy_addr") or linked_onu.get("serial") or linked_onu.get("id")
            if isinstance(linked_onu, dict) else linked_onu
        )
        contract_id = contract.get("contrato") or contract.get("id") or contract.get("clientecontrato_id")
        if not contract_id or not login:
            raise SGPError("O SGP não devolveu o contrato ou login do serviço deste cliente.", 422)
        first_name = str(customer.get("nome") or "Cliente").strip().split()[0]
        address_text = _address_text(address)
        return {
            "client_id": int(customer.get("id") or customer.get("cliente_id") or 0), "client": customer.get("nome") or "Cliente",
            "contract_id": contract_id, "service": login,
            "description": " · ".join(value for value in (first_name, address_text) if value),
            "address": address_text,
            "already_linked": bool(linked_onu),
            "linked_onu_id": linked_onu_id,
        }

    def verify_cto_contracts(self, contract_ids: list[int], *, include_history: bool = True) -> dict[str, Any]:
        """Find each contract's authorized ONU and compare its physical PON placement."""
        ids = list(dict.fromkeys(int(value) for value in contract_ids if int(value) > 0))
        if not ids or len(ids) > 40:
            raise SGPError("Informe entre 1 e 40 contratos.", 400)
        # The customer endpoint normally returns the current ONU placement.  An
        # unavailable OLT catalogue must not make the whole CTO consultation
        # fail; it is only a fallback for older/incomplete customer responses.
        olts = None
        olt_lock = threading.Lock()

        def fallback_olts():
            nonlocal olts
            with olt_lock:
                if olts is None:
                    try:
                        olts = self.olts()
                    except (SGPError, OSError, TimeoutError, ValueError, TypeError):
                        olts = []
                return olts

        def lookup(contract_id: int) -> dict[str, Any]:
            customer_record: dict[str, Any] = {}
            try:
                result = self.request(
                    "POST", "/api/ura/clientes/",
                    form={
                        "contrato": contract_id, "limit": 5, "exibir_conexao": "true",
                        "omitir_titulos": "true", "omitir_contatos": "true",
                    },
                )
                customers = result.get("clientes", []) if isinstance(result, dict) else []
                if customers:
                    customer = customers[0]
                    contracts = customer.get("contratos", []) if isinstance(customer, dict) else []
                    contract = next(
                        (item for item in contracts if int(item.get("contrato") or 0) == contract_id),
                        contracts[0] if contracts else {},
                    )
                    services = contract.get("servicos", []) if isinstance(contract, dict) else []
                    service = services[0] if services else {}
                    onu = service.get("onu") if isinstance(service.get("onu"), dict) else {}
                    olt = onu.get("olt") if isinstance(onu.get("olt"), dict) else {}
                    address = contract.get("endereco") or customer.get("endereco") or {}
                    raw_status = contract.get("status")
                    status_codes = {1: "Ativo", 2: "Inativo", 3: "Cancelado", 4: "Suspenso", 5: "Inviabilidade técnica", 6: "Novo", 7: "Ativo com velocidade reduzida"}
                    try:
                        status_label = status_codes.get(int(raw_status), str(raw_status or "Não informado"))
                    except (TypeError, ValueError):
                        status_label = str(raw_status or "Não informado").strip()
                    slot, pon = onu.get("slot"), onu.get("pon")
                    olt_value = onu.get("olt") if not isinstance(onu.get("olt"), dict) else None
                    customer_record = {
                        "contract_id": contract_id, "found": True,
                        # The customer endpoint already returns the authorized ONU on
                        # current SGP versions. Avoid scanning every OLT when slot and
                        # PON are available here; that scan can take minutes if one OLT
                        # is slow or offline.
                        "network_found": slot is not None and pon is not None,
                        "client_id": customer.get("id") or customer.get("cliente_id"),
                        "client": customer.get("nome"), "contract_status": status_label,
                        "contract_restricted": _normalized(status_label) in ("cancelado", "inviabilidade tecnica"),
                        "address": _address_text(address),
                        "olt_id": onu.get("olt_id") or olt.get("id") or olt_value,
                        "olt_name": onu.get("olt_name") or olt.get("nome") or olt.get("name"),
                        "onu": onu.get("onu") or onu.get("onuid") or onu.get("id"),
                        "slot": slot, "pon": pon,
                        "label": (
                            onu.get("ident") or
                            (onu.get("splitter") or {}).get("nome")
                            if isinstance(onu.get("splitter"), dict) else onu.get("ident")
                        ),
                        "online": str((onu.get("conexao") or {}).get("status", "")).casefold() == "online"
                        if isinstance(onu.get("conexao"), dict) else False,
                        "signal": onu.get("rx"), "serial": onu.get("phy_addr"),
                    }
            except SGPError:
                customer_record = {}
            if customer_record.get("network_found"):
                return customer_record
            if customer_record and not onu:
                # There is no currently linked ONU. Its last physical slot/PON
                # belongs to the customer history, not to the authorized-ONU
                # lists, so defer this record to the history restoration pass.
                return customer_record
            matches: list[dict[str, Any]] = []
            def search_olt(olt: dict[str, Any]) -> list[dict[str, Any]]:
                try:
                    result = self.request(
                        "GET", f"/api/fttx/olt/{int(olt['id'])}/onu/list/",
                        query={"contrato": contract_id, "signal": 1, "address": 1, "connection": 1},
                    )
                except (KeyError, TypeError, ValueError, SGPError):
                    # One unavailable OLT must not hold or fail the complete
                    # contract batch. Other OLTs may still contain the ONU.
                    return []
                if isinstance(result, list):
                    return [item for item in result if isinstance(item, dict)]
                return []

            # Searching OLTs sequentially made a single contract take N times
            # the upstream timeout. Four bounded workers keep the SGP load
            # controlled while making the total wait close to one/two calls.
            available_olts = fallback_olts()
            with ThreadPoolExecutor(max_workers=min(2, max(1, len(available_olts)))) as olt_pool:
                olt_results = list(olt_pool.map(search_olt, available_olts))
            for olt_matches in olt_results:
                exact_matches = [
                    item for item in olt_matches
                    if int(item.get("service_contrato") or 0) == contract_id
                ]
                if exact_matches:
                    matches = exact_matches
                    break
                matches.extend(olt_matches)
            if not matches:
                if customer_record:
                    return customer_record
                return {"contract_id": contract_id, "found": False, "network_found": False}
            onu = next(
                (item for item in matches if int(item.get("service_contrato") or 0) == contract_id),
                matches[0],
            )
            return {
                **customer_record, "contract_id": contract_id, "found": True, "network_found": True,
                "client": onu.get("service_cliente") or onu.get("cliente") or customer_record.get("client"),
                "olt_id": onu.get("olt_id"), "olt_name": onu.get("olt_name"),
                "slot": onu.get("slot"), "pon": onu.get("pon"),
                "onu": onu.get("onu") or onu.get("onuid"),
                "serial": onu.get("phy_addr"), "label": onu.get("ident"),
                "address": _address_text(onu.get("address") or onu.get("endereco") or onu.get("service_address")) or customer_record.get("address"),
                "online": bool(onu.get("online") or onu.get("connection") or onu.get("connected")),
                "signal": onu.get("signal") or onu.get("info_rx"),
            }

        def safe_lookup(contract_id: int) -> dict[str, Any]:
            try:
                return lookup(contract_id)
            except (SGPError, OSError, TimeoutError, ValueError, TypeError, KeyError) as exc:
                # A malformed or temporarily unavailable customer must be
                # reported as a partial failure, never turn the other 39
                # contracts into an HTTP 500 response.
                return {
                    "contract_id": contract_id,
                    "found": False,
                    "network_found": False,
                    "lookup_error": str(exc) or "Falha temporária na consulta deste contrato.",
                }

        with ThreadPoolExecutor(max_workers=min(4, len(ids))) as pool:
            contracts = list(pool.map(safe_lookup, ids))
        self._restore_unlinked_cto_positions(contracts)
        groups: dict[tuple[str, str, str], int] = {}
        for item in contracts:
            if not item.get("network_found") or item.get("slot") is None or item.get("pon") is None:
                continue
            key = (str(item.get("olt_id") or ""), str(item.get("slot") or ""), str(item.get("pon") or ""))
            groups[key] = groups.get(key, 0) + 1
        reference = max(groups, key=groups.get) if groups else None
        for item in contracts:
            if not item.get("found"):
                detail = str(item.get("lookup_error") or "").strip()
                item.update({
                    "belongs": None,
                    "reason": detail or "Contrato não localizado no cadastro de clientes nem nas ONUs autorizadas.",
                })
                continue
            if not item.get("network_found"):
                item.update({"belongs": None, "reason": "Contrato localizado, mas o SGP não devolveu ONU, OLT, slot e PON para comparação."})
                continue
            key = (str(item.get("olt_id") or ""), str(item.get("slot") or ""), str(item.get("pon") or ""))
            same_historical_branch = bool(
                item.get("network_source") == "history" and reference
                and str(item.get("slot")) == reference[1]
                and str(item.get("pon")) == reference[2]
                and not item.get("olt_id")
            )
            item["belongs"] = key == reference or same_historical_branch
            item["reason"] = (
                "Mesmo slot e PON do grupo, recuperados da última ONU desvinculada no histórico."
                if same_historical_branch else "Mesmo OLT, slot e PON do grupo."
                if item["belongs"] else "OLT, slot ou PON diferente do grupo principal."
            )
        self._enrich_cto_disconnect_analysis(contracts, reference, include_history=include_history)
        labels = [str(item.get("label") or "").strip() for item in contracts if item.get("belongs") and item.get("label")]
        reference_item = next((item for item in contracts if item.get("belongs")), {})
        return {
            "contracts": contracts,
            "reference": {
                "olt_id": reference[0], "slot": reference[1], "pon": reference[2],
                "olt_name": reference_item.get("olt_name"),
                "count": sum(item.get("belongs") is True for item in contracts),
                "label": max(labels, key=labels.count) if labels else "",
            } if reference else None,
            "all_same_cto": bool(reference and all(item.get("belongs") is not False for item in contracts)),
        }

    def _restore_unlinked_cto_positions(self, contracts: list[dict[str, Any]]) -> None:
        """Restore the last slot/PON from an ONU unlink event in client history."""
        targets = [
            item for item in contracts
            if item.get("found") and not item.get("network_found") and _as_int(item.get("client_id"))
        ]
        if not targets:
            return
        try:
            cookies = http.cookiejar.CookieJar()
            opener = build_opener(HTTPCookieProcessor(cookies))
            headers = {"User-Agent": "EBF-Tecnico/0.1"}

            def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
                request_headers = dict(headers)
                if referer:
                    request_headers["Referer"] = f"{self.base_url}{referer}"
                request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
                with opener.open(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8", "replace")

            login_path = "/accounts/login?next=/admin/"
            login_page = open_page(login_path)
            token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not token:
                return
            login_body = urlencode({
                "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8")
            open_page("/accounts/login/", login_body, login_path)

            def load(item: dict[str, Any]) -> tuple[int, list[dict[str, str]]]:
                client_id = int(item["client_id"])
                page = open_page(f"/admin/cliente/{client_id}/historico/")
                history = []
                for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S):
                    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
                    values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
                    if len(values) >= 5:
                        history.append({"text": values[3], "date": values[4]})
                return client_id, history

            with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
                histories = dict(pool.map(load, targets))
        except (SGPError, OSError, TimeoutError, ValueError, KeyError):
            return

        for item in targets:
            restored = _last_unlinked_network(histories.get(int(item["client_id"]), []))
            if restored:
                item.update({
                    "network_found": True, "network_source": "history",
                    "slot": restored["slot"], "pon": restored["pon"],
                    "serial": restored["serial"] or item.get("serial"),
                    "online": False, "history_date": restored["date"],
                })

    def _enrich_cto_disconnect_analysis(
        self, contracts: list[dict[str, Any]], reference: tuple[str, str, str] | None, *, include_history: bool = True,
    ) -> None:
        """Deep-check only problematic contracts, keeping the normal CTO scan light."""
        target_statuses = {"cancelado", "inativo", "inviabilidade tecnica", "suspenso"}
        disconnect_statuses = {"cancelado", "inativo", "inviabilidade tecnica"}
        targets = [
            item for item in contracts
            if item.get("belongs") is False or _normalized(item.get("contract_status")) in target_statuses
        ]
        if not targets:
            return
        ref_slot = str(reference[1]) if reference else ""
        ref_pon = str(reference[2]) if reference else ""
        for item in targets:
            status = _normalized(item.get("contract_status"))
            evidence = []
            if status in target_statuses:
                evidence.append({"kind": "status", "date": "", "text": f"Contrato com status {item.get('contract_status')}."})
            if item.get("slot") is not None and item.get("pon") is not None:
                evidence.append({
                    "kind": "network", "date": "",
                    "text": (
                        f"Modem {item.get('serial') or 'identificado'} está vinculado no slot {item.get('slot')}, PON {item.get('pon')}"
                        + (f", diferente do grupo {ref_slot}/{ref_pon}." if item.get("belongs") is False else ".")
                    ),
                })
            item["disconnect_analysis"] = {
                "decision": "disconnect" if status in disconnect_statuses else "review",
                "title": "Pode desconectar" if status in disconnect_statuses else "Conferir antes de desconectar",
                "summary": "Análise inicial pelo status e posição atual; o histórico detalhado não estava disponível.",
                "evidence": evidence,
            }

        # Cancelled, inactive and technically-unfeasible contracts are already
        # conclusive from their current status. Reading their full admin
        # history and every OS added up to several minutes without changing the
        # decision. Keep the expensive deep inspection only for inconclusive
        # network divergences.
        client_targets = [
            item for item in targets
            if _as_int(item.get("client_id"))
            and item.get("belongs") is False
            and _normalized(item.get("contract_status")) not in disconnect_statuses
        ]
        if not client_targets or not include_history:
            return
        try:
            cookies = http.cookiejar.CookieJar()
            opener = build_opener(HTTPCookieProcessor(cookies))
            headers = {"User-Agent": "EBF-Tecnico/0.1"}

            def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
                request_headers = dict(headers)
                if referer:
                    request_headers["Referer"] = f"{self.base_url}{referer}"
                request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
                with opener.open(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8", "replace")

            login_path = "/accounts/login?next=/admin/"
            login_page = open_page(login_path)
            token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not token:
                return
            login_body = urlencode({
                "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8")
            open_page("/accounts/login/", login_body, login_path)

            def inspect(item: dict[str, Any]) -> tuple[int, list[dict[str, str]], list[dict[str, Any]]]:
                client_id = int(item["client_id"])
                history = []
                try:
                    page = open_page(f"/admin/cliente/{client_id}/historico/")
                    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S):
                        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
                        values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
                        if len(values) >= 5:
                            history.append({"user": values[1], "text": values[3], "date": values[4]})
                except (SGPError, OSError, TimeoutError, ValueError, KeyError):
                    pass
                try:
                    orders = self._ura_order_pages({"cliente": client_id, "limit": 1000})
                except (SGPError, OSError, TimeoutError, ValueError):
                    orders = []
                return client_id, history, orders

            with ThreadPoolExecutor(max_workers=min(4, len(client_targets))) as pool:
                details = {client_id: (history, orders) for client_id, history, orders in pool.map(inspect, client_targets)}
        except (SGPError, OSError, TimeoutError, ValueError, KeyError):
            return

        network_pattern = re.compile(r"slot\s*[:=]?\s*(\d+).*?pon\s*[:=]?\s*(\d+)", re.I)
        for item in client_targets:
            history, orders = details.get(int(item["client_id"]), ([], []))
            proof = list(item["disconnect_analysis"]["evidence"])
            was_on_reference = False
            moved_network = False
            for entry in history:
                normalized = _normalized(entry["text"])
                match = network_pattern.search(entry["text"])
                if not match:
                    continue
                slot, pon = match.groups()
                if slot == ref_slot and pon == ref_pon and "desvinculad" in normalized:
                    was_on_reference = True
                    proof.append({"kind": "unlink", "date": entry["date"], "text": f"ONU desvinculada do slot {slot}, PON {pon}."})
                elif (slot != ref_slot or pon != ref_pon) and "vinculad" in normalized and "desvinculad" not in normalized:
                    moved_network = True
                    proof.append({"kind": "link", "date": entry["date"], "text": f"ONU vinculada depois no slot {slot}, PON {pon}."})
            address_changes = []
            for order in orders:
                reason = str(order.get("motivo") or order.get("os_motivo_descricao") or "")
                if "mudanca de endereco" in _normalized(reason):
                    when = order.get("data_finalizacao") or order.get("data_agendamento") or order.get("data_cadastro") or ""
                    address_changes.append({"kind": "address", "date": str(when), "text": f"OS de mudança de endereço #{order.get('id') or order.get('os_id')}."})
            proof.extend(address_changes)
            status = _normalized(item.get("contract_status"))
            conclusive_move = was_on_reference and (moved_network or bool(address_changes)) and item.get("belongs") is False
            can_disconnect = conclusive_move or status in disconnect_statuses
            item["disconnect_analysis"] = {
                "decision": "disconnect" if can_disconnect else "keep" if item.get("belongs") is True else "review",
                "title": "Pode desconectar" if can_disconnect else "Não desconectar" if item.get("belongs") is True else "Conferir antes de desconectar",
                "summary": (
                    "Há histórico de saída desta PON e mudança para outra rede/endereço."
                    if conclusive_move else (
                        f"Cliente no slot/PON {item.get('slot')}/{item.get('pon')}, com modem vinculado, porém o contrato está {str(item.get('contract_status') or '').casefold()}; pode ser retirado."
                        if item.get("slot") is not None and item.get("pon") is not None
                        else f"Contrato {str(item.get('contract_status') or '').casefold()}; o cabo antigo pode ser retirado."
                    )
                    if can_disconnect else "Não foram encontradas provas suficientes de que este cabo ficou abandonado."
                ),
                "evidence": proof,
            }

    def pon_connection_status(self, olt_id: int, slot: int, pon: int) -> dict[str, Any]:
        result: Any = None
        queries = (
            {
                "slot": int(slot), "pon": int(pon), "signal": 1,
                "address": 1, "connection": 1, "limit": 1000,
            },
            {"slot": int(slot), "pon": int(pon)},
        )
        for attempt, query in enumerate(queries):
            try:
                result = self.request(
                    "GET", f"/api/fttx/olt/{int(olt_id)}/onu/list/",
                    query=query,
                )
                break
            except SGPError as exc:
                if attempt == 0 and exc.status in (400, 422, 500, 502, 503, 504):
                    time.sleep(.5)
                    continue
                raise
        rows: list[Any] = []
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            for key in ("onus", "results", "data", "items", "rows"):
                candidate = result.get(key)
                if isinstance(candidate, list):
                    rows = candidate
                    break
                if isinstance(candidate, dict):
                    nested = candidate.get("results") or candidate.get("items") or candidate.get("onus")
                    if isinstance(nested, list):
                        rows = nested
                        break
            if not rows:
                upstream_error = result.get("error") or result.get("detail")
                if upstream_error or result.get("success") is False:
                    message = str(upstream_error or result.get("message") or "erro não detalhado")
                    raise SGPError(f"O SGP não conseguiu consultar a PON: {message[:180]}", 502)
        onus = []
        for onu in rows:
            if not isinstance(onu, dict):
                continue
            online = _onu_connectivity(onu) is True
            connection_data = onu.get("connection") if isinstance(onu.get("connection"), dict) else {}
            status_changed_at = (
                onu.get("status_changed_at") or onu.get("connection_date") or
                onu.get("last_change") or onu.get("last_down") or onu.get("offline_at") or
                connection_data.get("date") or connection_data.get("updated_at") or
                connection_data.get("last_change")
            )
            onus.append({
                "id": onu.get("id"), "serial": onu.get("phy_addr"),
                "contract_id": onu.get("service_contrato"),
                "client": onu.get("service_cliente") or onu.get("cliente"),
                "label": onu.get("ident"),
                "address": onu.get("address") or onu.get("endereco"),
                "description": onu.get("description") or onu.get("descricao"),
                "slot": onu.get("slot") or slot, "pon": onu.get("pon") or pon,
                "vlan": onu.get("vlan") or onu.get("info_vlan"), "online": online,
                "signal": onu.get("signal") or onu.get("info_rx"),
                "status_changed_at": status_changed_at,
            })
        return {
            "olt_id": int(olt_id), "slot": int(slot), "pon": int(pon),
            "checked_at": datetime.now().isoformat(), "onus": onus,
            "online": sum(1 for onu in onus if onu["online"]),
        }

    def optical_signals(self, olt_id: int, slot: int, pon: int, admin_report: bool = False) -> dict[str, Any]:
        """Read TX/RX from SGP's administrative optical-level report."""
        if not ADMIN_ENDPOINTS_ONLY and not admin_report:
            status = self.pon_connection_status(olt_id, slot, pon)
            return {
                **status,
                "onus": [{
                    **row, "tx_dbm": None,
                    "rx_dbm": _signal_dbm(row.get("signal")),
                } for row in status.get("onus") or []],
                "source": "api-pon",
            }
        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        try:
            login_page = open_page(login_path)
            token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
            if not token:
                raise SGPError("O SGP não liberou a consulta óptica.", 502)
            login_body = urlencode({
                "csrfmiddlewaretoken": token.group(1), "username": self.credentials.username,
                "password": self.credentials.password, "next": "/admin/",
            }).encode("utf-8")
            open_page("/accounts/login/", login_body, login_path)
            # Ask the report itself for this branch. Loading only the OLT can
            # return a paginated first slice and hide other clients on the CTO.
            query = urlencode({
                "olt": int(olt_id), "slot": int(slot), "pon": int(pon),
                "paginate_by": 1000, "limit": 1000, "length": 1000,
            })
            page = open_page(f"/admin/network/onu/optical/list/?{query}", referer="/admin/")
            pages = [page]
            visited = {1}
            numbers = set()
            while True:
                for href in re.findall(r'href=[\"\']([^\"\']+)', pages[-1], re.I):
                    fields = dict(parse_qsl(urlsplit(html.unescape(href)).query))
                    if str(fields.get("page", "")).isdigit():
                        numbers.add(int(fields["page"]))
                pending = sorted(numbers - visited)
                if not pending:
                    break
                if len(visited) >= 100:
                    raise SGPError("O relatório óptico excedeu 100 páginas; consulta incompleta.", 502)
                page_number = pending[0]
                visited.add(page_number)
                pages.append(open_page(f"/admin/network/onu/optical/list/?{query}&page={page_number}", referer="/admin/"))
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise SGPError("Não foi possível abrir a listagem óptica do SGP.", 502) from exc
        rows = [row for report_page in pages for row in _parse_optical_signal_table(report_page)]
        rows = list({(_identity_value(row.get("serial")) or str(row.get("contract_id")) + ':' + str(row.get("client")), str(row.get("slot")), str(row.get("pon"))): row for row in rows}.values())
        filtered = [
            row for row in rows
            if (row.get("slot") in (None, "") or str(row["slot"]) == str(slot))
            and (row.get("pon") in (None, "") or str(row["pon"]) == str(pon))
        ]
        if not filtered:
            # Preserve the working signal check if this SGP build renders the
            # admin table with JavaScript instead of returning rows in HTML.
            fallback = self.pon_connection_status(olt_id, slot, pon)
            fallback_rows = [{
                **row, "tx_dbm": None, "rx_dbm": _signal_dbm(row.get("signal")),
            } for row in fallback.get("onus") or []]
            if not fallback_rows:
                raise SGPError("O SGP não retornou sinais para este slot e PON.", 422)
            return {**fallback, "onus": fallback_rows, "source": "api-pon-fallback"}
        try:
            api_rows = self.pon_connection_status(olt_id, slot, pon).get("onus") or []
        except SGPError:
            api_rows = []
        by_serial = {_identity_value(item.get("serial")): item for item in api_rows if _identity_value(item.get("serial"))}
        by_contract = {str(item.get("contract_id")): item for item in api_rows if item.get("contract_id") not in (None, "")}
        for row in filtered:
            extra = by_serial.get(_identity_value(row.get("serial"))) or by_contract.get(str(row.get("contract_id"))) or {}
            for field in ("vlan", "address", "description", "label", "slot", "pon"):
                if row.get(field) in (None, "") and extra.get(field) not in (None, ""):
                    row[field] = extra[field]
        seen_serials = {_identity_value(row.get("serial")) for row in filtered if row.get("serial")}
        seen_contracts = {str(row.get("contract_id")) for row in filtered if row.get("contract_id")}
        for row in api_rows:
            if str(row.get("slot")) != str(slot) or str(row.get("pon")) != str(pon):
                continue
            serial = _identity_value(row.get("serial"))
            contract = str(row.get("contract_id") or '')
            if (serial and serial in seen_serials) or (contract and contract in seen_contracts):
                continue
            filtered.append({**row, "tx_dbm": None, "rx_dbm": _signal_dbm(row.get("signal"))})
            if serial: seen_serials.add(serial)
            if contract: seen_contracts.add(contract)
        return {
            "olt_id": int(olt_id), "slot": int(slot), "pon": int(pon),
            "checked_at": datetime.now().isoformat(), "onus": filtered,
            "source": "sgp-optical-list",
        }

    def cto_attenuation(self, contract_id: int) -> dict[str, Any]:
        verification = self.verify_cto_contracts([int(contract_id)])
        contract = (verification.get("contracts") or [{}])[0]
        reference = verification.get("reference")
        if not contract.get("found"):
            raise SGPError("Contrato não localizado no SGP.", 404)
        if not reference:
            raise SGPError(
                "O contrato foi localizado, mas não possui OLT, slot e PON para análise.",
                422,
            )
        status = self.optical_signals(
            int(reference["olt_id"]), int(reference["slot"]), int(reference["pon"]),
        )
        expected_label = _optical_label_key(contract.get("label") or reference.get("label"))
        same_cto = []
        for onu in status.get("onus") or []:
            current_label = _optical_label_key(onu.get("label"))
            same_label = bool(
                expected_label and current_label and (
                    expected_label == current_label or
                    (len(expected_label) >= 5 and expected_label in current_label) or
                    (len(current_label) >= 5 and current_label in expected_label)
                )
            )
            same_contract = str(onu.get("contract_id") or "") == str(contract_id)
            same_serial = bool(
                _identity_value(onu.get("serial"))
                and _identity_value(onu.get("serial")) == _identity_value(contract.get("serial"))
            )
            if same_label or same_contract or same_serial:
                same_cto.append({
                    **onu,
                    "tx_dbm": _signal_dbm(onu.get("tx_dbm") or onu.get("tx")),
                    "rx_dbm": _signal_dbm(onu.get("rx_dbm") or onu.get("rx") or onu.get("signal")),
                    "is_target": same_contract or same_serial,
                })

        if not same_cto:
            raise SGPError(
                "A CTO foi identificada, mas nenhuma ONU com a mesma etiqueta apareceu na listagem óptica.",
                422,
            )

        def metrics(field: str) -> dict[str, Any]:
            values = [item[field] for item in same_cto if item.get(field) is not None]
            average = round(sum(values) / len(values), 2) if values else None
            return {
                "count": len(values), "average": average,
                "best": max(values) if values else None,
                "worst": min(values) if values else None,
                "weak_count": sum(1 for value in values if value <= -27),
            }

        tx_metrics, rx_metrics = metrics("tx_dbm"), metrics("rx_dbm")
        cto_attenuated = any(
            data["count"] and (
                data["average"] <= -27
                or data["weak_count"] >= max(1, round(data["count"] * .6))
            )
            for data in (tx_metrics, rx_metrics)
        )
        target = next((item for item in same_cto if item.get("is_target")), None)
        target_weak = bool(target and any(
            value is not None and value <= -27
            for value in (target.get("tx_dbm"), target.get("rx_dbm"))
        ))
        client_attenuated = bool(target_weak and not cto_attenuated)
        attenuated = cto_attenuated or client_attenuated
        attention = bool(not attenuated and any(
            data["average"] is not None and data["average"] <= -25
            for data in (tx_metrics, rx_metrics)
        ))
        diagnosis = (
            "CTO inteira com sinal ruim" if cto_attenuated else
            "Somente este cliente está atenuado" if client_attenuated else
            "Sinal em atenção" if attention else
            "TX e RX dentro do esperado" if tx_metrics["count"] or rx_metrics["count"] else
            "Sem leituras de sinal"
        )
        return {
            "contract": contract,
            "reference": reference,
            "clients": same_cto,
            "signals": {"tx": tx_metrics, "rx": rx_metrics},
            "attenuated": attenuated,
            "cto_attenuated": cto_attenuated,
            "client_attenuated": client_attenuated,
            "attention": attention,
            "diagnosis": diagnosis,
            "checked_at": status.get("checked_at"),
            "source": status.get("source"),
        }

    def pon_attenuation(self, contract_id: int) -> dict[str, Any]:
        """Diagnose the target ONU against its whole PON and local street/CTO."""
        verification = self.verify_cto_contracts([int(contract_id)])
        contract = (verification.get("contracts") or [{}])[0]
        reference = verification.get("reference")
        if not contract.get("found"):
            raise SGPError("Contrato não localizado no SGP.", 404)
        if not reference:
            raise SGPError("O contrato não possui OLT, slot e PON para análise.", 422)
        status = self.optical_signals(
            int(reference["olt_id"]), int(reference["slot"]), int(reference["pon"]), admin_report=True,
        )
        clients = []
        target_serial = _identity_value(contract.get("serial"))
        for raw in status.get("onus") or []:
            tx = _signal_dbm(raw.get("tx_dbm") or raw.get("tx"))
            rx = _signal_dbm(raw.get("rx_dbm") or raw.get("rx") or raw.get("signal"))
            same_contract = str(raw.get("contract_id") or "") == str(contract_id)
            same_serial = bool(target_serial and _identity_value(raw.get("serial")) == target_serial)
            clients.append({
                **raw,
                "slot": raw.get("slot") or reference.get("slot"),
                "pon": raw.get("pon") or reference.get("pon"),
                "tx_dbm": tx,
                "rx_dbm": rx,
                "is_target": same_contract or same_serial,
            })
        if not clients:
            raise SGPError("O SGP não retornou leituras para esta PON.", 422)

        target = next((item for item in clients if item["is_target"]), None)
        target_label = _optical_label_key((target or {}).get("label") or contract.get("label"))
        street_search = _optical_street_search((target or {}).get("description") or contract.get("address"))
        report_has_label = bool(target_label and any(_optical_label_key(item.get("label")) for item in clients))
        report_has_street = bool(street_search and any(
            re.search(rf"\b{re.escape(street_search)}\b", _normalized(item.get("description"))) for item in clients
        ))
        area_clients = [item for item in clients if (
            report_has_street and bool(re.search(
                rf"\b{re.escape(street_search)}\b", _normalized(item.get("description")), re.I,
            ))
        ) or (
            not report_has_street and report_has_label and _optical_label_key(item.get("label")) == target_label
        )]
        if target and target not in area_clients:
            area_clients.append(target)

        def metrics(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
            values = sorted(item[field] for item in rows if item.get(field) is not None)
            middle = len(values) // 2
            median = None if not values else values[middle] if len(values) % 2 else round((values[middle - 1] + values[middle]) / 2, 2)
            return {
                "count": len(values), "median": median,
                "average": round(sum(values) / len(values), 2) if values else None,
                "best": max(values) if values else None, "worst": min(values) if values else None,
                "bad_count": sum(1 for value in values if value <= -25.25),
                "limit_count": sum(1 for value in values if -25.25 < value <= -25.0),
            }

        pon = {field: metrics(clients, f"{field}_dbm") for field in ("tx", "rx")}
        area = {field: metrics(area_clients, f"{field}_dbm") for field in ("tx", "rx")}

        def row_level(item: dict[str, Any]) -> str:
            values = [value for value in (item.get("tx_dbm"), item.get("rx_dbm")) if value is not None]
            if not values:
                return "no_signal"
            if any(value <= -25.25 for value in values):
                return "bad"
            if any(value <= -25.0 for value in values):
                return "limit"
            return "good"

        def group_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
            counts = Counter(row_level(item) for item in rows)
            measured = counts["good"] + counts["limit"] + counts["bad"]
            prevalent = max(("bad", "limit", "good"), key=lambda key: counts[key]) if measured else "no_signal"
            return {
                "total": len(rows), "measured": measured,
                "good": counts["good"], "limit": counts["limit"],
                "bad": counts["bad"], "no_signal": counts["no_signal"],
                "bad_percent": round(counts["bad"] * 100 / measured, 1) if measured else 0,
                "prevalent": prevalent,
            }

        pon_summary, area_summary = group_summary(clients), group_summary(area_clients)
        for item in clients:
            item["signal_level"] = row_level(item)
        def collective(group: dict[str, dict[str, Any]], minimum: int) -> bool:
            return any(data["count"] >= minimum and (
                (data["median"] is not None and data["median"] <= -25.25)
                or data["bad_count"] >= max(minimum, math.ceil(data["count"] * .30))
            ) for data in group.values())
        pon_bad = bool(pon_summary["measured"] and pon_summary["bad_percent"] >= 50)
        pon_attention = bool(pon_summary["measured"] and 15 <= pon_summary["bad_percent"] < 50)
        area_bad = len(area_clients) >= 2 and collective(area, 2)
        target_values = [(target or {}).get("tx_dbm"), (target or {}).get("rx_dbm")]
        target_bad = any(value is not None and value <= -25.25 for value in target_values)
        target_limit = any(value is not None and -25.25 < value <= -25.0 for value in target_values)
        diagnosis = (
            "Problema na PON" if pon_bad else
            "PON precisa de análise" if pon_attention else
            "CTO ou rua com sinal ruim" if area_bad else
            "Não é a PON · provável problema individual" if target_bad else
            "Cliente no limite" if target_limit else
            "Não é problema da PON"
        )
        return {
            "contract": contract, "reference": reference, "target": target,
            "clients": clients, "area_clients": area_clients,
            "signals": pon, "area_signals": area,
            "pon_summary": pon_summary, "area_summary": area_summary,
            "area_name": _optical_street_display((target or {}).get("description") or contract.get("address")),
            "street_search": street_search,
            "pon_attenuated": pon_bad, "area_attenuated": area_bad,
            "pon_attention": pon_attention,
            "pon_status": "problem" if pon_bad else "review" if pon_attention else "not_pon",
            "target_attenuated": target_bad, "target_limit": target_limit,
            "diagnosis": diagnosis, "checked_at": status.get("checked_at"),
            "source": status.get("source"), "threshold": {"limit": -25.0, "bad": -25.25},
        }

    def pon_port_analysis(self, olt_id: int, slot: int, pon: int) -> dict[str, Any]:
        """Apply the existing OS optical rules to an explicitly selected PON."""
        status = self.optical_signals(int(olt_id), int(slot), int(pon), admin_report=True)
        clients = []
        for raw in status.get("onus") or []:
            tx = _signal_dbm(raw.get("tx_dbm") or raw.get("tx"))
            rx = _signal_dbm(raw.get("rx_dbm") or raw.get("rx") or raw.get("signal"))
            values = [value for value in (tx, rx) if value is not None]
            level = "no_signal" if not values else "bad" if any(value <= -25.25 for value in values) else "limit" if any(value <= -25.0 for value in values) else "good"
            clients.append({**raw, "slot": raw.get("slot") or int(slot), "pon": raw.get("pon") or int(pon),
                            "tx_dbm": tx, "rx_dbm": rx, "signal_level": level, "is_target": False})
        if not clients:
            raise SGPError("O SGP não retornou leituras para esta PON.", 422)

        def metrics(field: str) -> dict[str, Any]:
            values = sorted(item[field] for item in clients if item.get(field) is not None)
            middle = len(values) // 2
            median = None if not values else values[middle] if len(values) % 2 else round((values[middle - 1] + values[middle]) / 2, 2)
            return {"count": len(values), "median": median,
                    "average": round(sum(values) / len(values), 2) if values else None,
                    "best": max(values) if values else None, "worst": min(values) if values else None,
                    "bad_count": sum(1 for value in values if value <= -25.25),
                    "limit_count": sum(1 for value in values if -25.25 < value <= -25.0)}

        counts = Counter(item["signal_level"] for item in clients)
        measured = counts["good"] + counts["limit"] + counts["bad"]
        summary = {"total": len(clients), "measured": measured, "good": counts["good"],
                   "limit": counts["limit"], "bad": counts["bad"], "no_signal": counts["no_signal"],
                   "bad_percent": round(counts["bad"] * 100 / measured, 1) if measured else 0,
                   "prevalent": max(("bad", "limit", "good"), key=lambda key: counts[key]) if measured else "no_signal"}
        bad = bool(measured and summary["bad_percent"] >= 50)
        attention = bool(measured and 15 <= summary["bad_percent"] < 50)
        empty = {"total": 0, "measured": 0, "good": 0, "limit": 0, "bad": 0, "no_signal": 0, "bad_percent": 0, "prevalent": "no_signal"}
        return {"reference": {"olt_id": int(olt_id), "olt_name": next((item.get("olt_name") for item in clients if item.get("olt_name")), str(olt_id)),
                              "slot": int(slot), "pon": int(pon)},
                "target": {}, "clients": clients, "area_clients": [],
                "signals": {field: metrics(f"{field}_dbm") for field in ("tx", "rx")},
                "pon_summary": summary, "area_summary": empty, "area_name": "Análise direta", "street_search": "",
                "pon_attenuated": bad, "area_attenuated": False, "pon_attention": attention,
                "pon_status": "problem" if bad else "review" if attention else "not_pon",
                "target_attenuated": False, "target_limit": False,
                "diagnosis": "Problema na PON" if bad else "PON precisa de análise" if attention else "PON dentro do esperado",
                "checked_at": status.get("checked_at"), "source": status.get("source"), "threshold": {"limit": -25.0, "bad": -25.25}}

    def onu_options(self) -> dict[str, list[dict[str, Any]]]:
        endpoints = {
            "types": "/api/fttx/onutype/list/",
            "modes": "/api/fttx/onumode/list/",
            "templates": "/api/fttx/onutemplate/list/",
        }
        return {
            key: result if isinstance((result := self.request("GET", path)), list) else []
            for key, path in endpoints.items()
        }

    def splitters(self, olt_id: int, slot: str, pon: str) -> list[dict[str, Any]]:
        result = self.request(
            "GET",
            f"/api/fttx/olt/pon/{olt_id}/splitter/list/",
            query={"slot": slot, "pon": pon},
        )
        return result if isinstance(result, list) else []

    def authorize_onu(self, olt_id: int, data: dict[str, Any]) -> dict[str, Any]:
        required = ("id", "slot", "pon", "onutype", "mode", "ident")
        missing = [field for field in required if str(data.get(field, "")).strip() == ""]
        if missing:
            raise SGPError("Preencha todos os campos obrigatórios da ONU.", 400)
        if not any(str(data.get(key, "")).strip() for key in ("contrato", "service", "description")):
            raise SGPError("Informe o contrato, o login do serviço ou uma descrição.", 400)
        if not str(data.get("onutemplate", "")).strip() and not str(data.get("onutemplate_plain", "")).strip():
            raise SGPError("Selecione o template da ONU.", 400)
        allowed = {
            "id", "slot", "pon", "onutype", "mode", "contrato", "service", "description",
            "onutemplate", "onutemplate_plain", "splitter", "splitter_port", "vlan", "ident",
            "pppoe_login", "pppoe_password", "wifi_ssid", "wifi_password", "wifi_channel",
            "wifi_ssid5", "wifi_password5", "wifi_channel5", "wifi_authmode", "wifi_encrypttype",
            "wifi_central", "onu_web", "onu_web_port", "onu_telnet", "onu_login", "onu_password",
        }
        form = {key: str(value).strip() for key, value in data.items() if key in allowed and str(value).strip()}
        return self.request("POST", f"/api/fttx/olt/{olt_id}/auth/", form=form)

    def deauthorize_onu(self, identifier: Any) -> dict[str, Any]:
        """Remove the ONU from SGP and deauthorizes it on the OLT."""
        value = str(identifier or "").strip()
        if not value:
            raise SGPError("ONU inválida para desvinculação.", 400)
        # Omitting no_commit is intentional: SGP also commits removal to the OLT.
        return self.request("POST", f"/api/fttx/onu/{quote(value, safe='')}/deauth/", form={})

    def update_onu_label(self, onu_id: Any, label: str, vlan: Any = None) -> dict[str, Any]:
        """Change label and, when supplied, VLAN through SGP's native ONU edit form."""
        try:
            numeric_id = int(str(onu_id).strip())
        except (TypeError, ValueError) as exc:
            raise SGPError("O SGP não informou o código interno do modem.", 422) from exc
        clean_label = " ".join(str(label or "").split())
        if not clean_label:
            raise SGPError("Informe a nova etiqueta do modem.", 400)
        if len(clean_label) > 120:
            raise SGPError("A etiqueta deve ter no máximo 120 caracteres.", 400)
        clean_vlan: str | None = None
        if vlan not in (None, ""):
            try:
                vlan_number = int(str(vlan).strip())
            except (TypeError, ValueError) as exc:
                raise SGPError("Informe uma VLAN válida.", 400) from exc
            if not 1 <= vlan_number <= 4094:
                raise SGPError("A VLAN deve estar entre 1 e 4094.", 400)
            clean_vlan = str(vlan_number)

        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=max(self.timeout, 45)) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        login_page = open_page(login_path)
        login_token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not login_token:
            raise SGPError("O SGP não liberou a alteração da etiqueta.", 502)
        open_page("/accounts/login/", urlencode({
            "csrfmiddlewaretoken": login_token.group(1), "username": self.credentials.username,
            "password": self.credentials.password, "next": "/admin/",
        }).encode("utf-8"), login_path)

        path = f"/admin/network/onu/{numeric_id}/edit/"
        page = open_page(path)
        form_match = re.search(r"<form\b[^>]*method=['\"]?post['\"]?[^>]*>(.*?)</form>", page, re.I | re.S)
        if not form_match:
            raise SGPError("O formulário de edição do modem não foi encontrado no SGP.", 404)
        form_html = form_match.group(1)
        form: dict[str, str] = {}
        for attrs in re.findall(r"<input\b([^>]*)>", form_html, re.I | re.S):
            name = _html_attr(attrs, "name")
            input_type = _html_attr(attrs, "type").casefold() or "text"
            if not name or input_type in ("submit", "button", "file"):
                continue
            if input_type in ("checkbox", "radio") and not re.search(r"\bchecked\b", attrs, re.I):
                continue
            form[name] = _html_attr(attrs, "value")
        for attrs, options in re.findall(r"<select\b([^>]*)>(.*?)</select>", form_html, re.I | re.S):
            name = _html_attr(attrs, "name")
            if not name:
                continue
            choices = re.findall(r"<option\b([^>]*)>", options, re.I | re.S)
            selected = next((item for item in choices if re.search(r"\bselected\b", item, re.I)), choices[0] if choices else "")
            form[name] = _html_attr(selected, "value")
        for attrs, value in re.findall(r"<textarea\b([^>]*)>(.*?)</textarea>", form_html, re.I | re.S):
            name = _html_attr(attrs, "name")
            if name:
                form[name] = html.unescape(value)
        if not form.get("csrfmiddlewaretoken"):
            raise SGPError("O SGP não forneceu a validação do formulário do modem.", 502)
        previous = form.get("ident", "")
        previous_vlan = form.get("vlan", "")
        form["ident"] = clean_label
        if clean_vlan is not None:
            form["vlan"] = clean_vlan
        result = open_page(path, urlencode(form).encode("utf-8"), path)
        error = re.search(r"<ul\s+class=['\"]errorlist['\"]>(.*?)</ul>", result, re.I | re.S)
        if error:
            message = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", error.group(1))).split())
            raise SGPError(message or "O SGP recusou a alteração da etiqueta.", 422)
        verify = open_page(path)
        ident = re.search(r"<input\b(?=[^>]*\bname=['\"]ident['\"])[^>]*>", verify, re.I | re.S)
        saved = _html_attr(ident.group(0), "value") if ident else ""
        if saved != clean_label:
            raise SGPError("O SGP não confirmou a nova etiqueta do modem.", 502)
        vlan_input = re.search(r"<input\b(?=[^>]*\bname=['\"]vlan['\"])[^>]*>", verify, re.I | re.S)
        saved_vlan = _html_attr(vlan_input.group(0), "value") if vlan_input else ""
        if clean_vlan is not None and saved_vlan != clean_vlan:
            raise SGPError("O SGP não confirmou a nova VLAN do modem.", 502)
        return {"ok": True, "onu_id": numeric_id, "previous_label": previous, "label": saved, "previous_vlan": previous_vlan, "vlan": saved_vlan}

    def productivity_report(self, display_name: str, user_id: int, period: str = "week", refunds: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        today = date.today()
        month_start = today.replace(day=1)
        week_start = today - timedelta(days=today.weekday())
        selected_start = month_start if period == "month" else week_start if period == "week" else today
        month_orders = self._enrich_repeat_candidates(self._closed_orders_between(month_start, today))
        repeat_data = _apply_repeat_refunds(_repeat_adjustments(month_orders), refunds or [])

        technician = display_name.casefold().strip()
        all_orders = [
            order for order in month_orders
            if str(order.get("os_tecnico_responsavel", "")).casefold().strip() == technician
            or technician in {str(name).casefold().strip() for name in order.get("os_tecnicos_auxiliares", [])}
        ]
        all_orders.sort(key=lambda item: str(item.get("os_data_finalizacao", "")), reverse=True)
        selected_orders = [order for order in all_orders if _order_date(order) >= selected_start]
        technician_repeats = repeat_data.get(_normalized(display_name), {"count": 0, "lost_points": 0.0, "details": []})
        month_points = round(max(0, sum(_score_order(order)[1] for order in all_orders) - technician_repeats["lost_points"]), 2)

        materials = self._materials_for_user(selected_start, today, user_id)
        type_counts: Counter[str] = Counter()
        selected_points = 0.0
        history = []
        for order in selected_orders:
            category, points = _score_order(order)
            type_counts[category] += 1
            selected_points += points
            history.append({
                "id": order.get("os_id"),
                "client": order.get("cliente"),
                "type": category,
                "reason": order.get("os_motivo_descricao") or order.get("os_conteudo"),
                "closed_at": order.get("os_data_finalizacao"),
                "points": points,
            })

        selected_repeat_details = [item for item in technician_repeats["details"] if item["repeat_date"] >= selected_start.isoformat()]
        selected_lost_points = round(sum(float(item.get("points_deducted", item["points_lost"])) for item in selected_repeat_details), 2)
        selected_refunded_points = round(sum(float(item["points_lost"]) for item in selected_repeat_details if item.get("refunded")), 2)
        return {
            "period": period,
            "start": selected_start.isoformat(),
            "end": today.isoformat(),
            "closed_orders": len(selected_orders),
            "points": round(max(0, selected_points - selected_lost_points), 2),
            "month_points": month_points,
            "repeated": {
                "count": len(selected_repeat_details),
                "lost_points": selected_lost_points,
                "refunded_points": selected_refunded_points,
                "month_count": int(technician_repeats["count"]),
                "month_lost_points": round(float(technician_repeats["lost_points"]), 2),
                "month_refunded_points": round(float(technician_repeats.get("refunded_points", 0)), 2),
                "details": selected_repeat_details,
            },
            "types": [{"name": name, "count": count} for name, count in type_counts.most_common()],
            "materials": materials,
            "material_units": round(sum(float(item["quantity"]) for item in materials), 2),
            "history": history,
        }

    def team_productivity_report(
        self,
        refunds: list[dict[str, Any]] | None = None,
        technician_filter: str = "",
        selected_month: str = "",
    ) -> dict[str, Any]:
        today = _business_today()
        if selected_month:
            try:
                year, month = (int(value) for value in selected_month.split("-", 1))
                month_start = date(year, month, 1)
            except (TypeError, ValueError):
                raise SGPError("Mês inválido. Use o formato AAAA-MM.", 400)
            if month_start > today.replace(day=1):
                raise SGPError("Não é possível consultar um mês futuro.", 400)
        else:
            month_start = today.replace(day=1)
        next_month = date(month_start.year + (month_start.month == 12), 1 if month_start.month == 12 else month_start.month + 1, 1)
        month_end = min(today, next_month - timedelta(days=1))
        orders = self._closed_orders_between(month_start, month_end)
        selected_name = str(technician_filter or "").strip()
        if selected_name:
            selected_orders = [
                order for order in orders
                if _same_person(order.get("os_tecnico_responsavel"), selected_name)
                or any(_same_person(aux, selected_name) for aux in order.get("os_tecnicos_auxiliares", []))
            ]
            selected_clients = {_normalized(order.get("cliente")) for order in selected_orders if _normalized(order.get("cliente"))}
            orders = [order for order in orders if _normalized(order.get("cliente")) in selected_clients]
        orders = self._enrich_repeat_candidates(orders)
        repeats = _apply_repeat_refunds(_repeat_adjustments(orders), refunds or [])
        raw_names = sorted(
            {
                name
                for order in orders
                for name in (
                    [str(order.get("os_tecnico_responsavel") or "").strip()]
                    + [str(aux).strip() for aux in order.get("os_tecnicos_auxiliares", [])]
                )
                if name and (not selected_name or _same_person(name, selected_name))
            }
        )
        name_groups: list[list[str]] = []
        for name in raw_names:
            group = next((item for item in name_groups if any(_same_person(name, alias) for alias in item)), None)
            if group is None:
                name_groups.append([name])
            else:
                group.append(name)
        technicians = []
        for aliases in name_groups:
            name = max(aliases, key=lambda value: (len(_normalized(value).split()), len(_normalized(value))))
            own_orders = [
                order for order in orders
                if any(_same_person(str(order.get("os_tecnico_responsavel") or ""), alias) for alias in aliases)
                or any(_same_person(str(aux), alias) for aux in order.get("os_tecnicos_auxiliares", []) for alias in aliases)
            ]
            type_summary: dict[str, dict[str, Any]] = {}
            order_history = []
            for order in sorted(own_orders, key=lambda item: str(item.get("os_data_finalizacao") or ""), reverse=True):
                category, order_points = _score_order(order)
                bucket = type_summary.setdefault(category, {"name": category, "count": 0, "unit_points": order_points, "points": 0.0})
                bucket["count"] += 1
                bucket["points"] = round(float(bucket["points"]) + order_points, 2)
                order_history.append({
                    "id": order.get("os_id"), "client_id": order.get("cliente_id"),
                    "client": order.get("cliente"), "type": category,
                    "closed_at": order.get("os_data_finalizacao"), "points": order_points,
                })
            repeat_buckets = [repeats[_normalized(alias)] for alias in aliases if _normalized(alias) in repeats]
            repeated = {
                "count": sum(int(item.get("count") or 0) for item in repeat_buckets),
                "lost_points": round(sum(float(item.get("lost_points") or 0) for item in repeat_buckets), 2),
                "refunded_points": round(sum(float(item.get("refunded_points") or 0) for item in repeat_buckets), 2),
                "details": [detail for item in repeat_buckets for detail in item.get("details", [])],
            }
            gross = round(sum(_score_order(order)[1] for order in own_orders), 2)
            technicians.append({
                "name": name,
                "closed_orders": len(own_orders),
                "gross_points": gross,
                "points": round(max(0, gross - float(repeated["lost_points"])), 2),
                "types": sorted(type_summary.values(), key=lambda item: (-int(item["count"]), item["name"])),
                "history": order_history,
                "repeated": repeated,
            })
        technicians.sort(key=lambda item: (-float(item["points"]), item["name"].casefold()))
        return {"start": month_start.isoformat(), "end": month_end.isoformat(), "month": month_start.strftime("%Y-%m"), "technicians": technicians}

    def _closed_orders_between(self, start: date, end: date) -> list[dict[str, Any]]:
        form = {
            "status": 1,
            "data_finalizacao_inicio": start.isoformat(),
            "data_finalizacao_fim": end.isoformat(),
            "limit": 1000,
        }
        orders = self._ura_order_pages(form)
        return [
            {
                "os_id": item.get("id"),
                "cliente_id": item.get("cliente_id"),
                "contrato_id": item.get("contrato_id") or item.get("contrato"),
                "cliente_cpf": item.get("cliente_cpf") or item.get("cpfcnpj") or item.get("cpf"),
                "servico_login": item.get("servico_login") or item.get("login"),
                "endereco": item.get("endereco") or item.get("contrato_endereco"),
                "cliente": item.get("cliente"),
                "os_motivo_descricao": item.get("motivo"),
                "os_conteudo": item.get("conteudo"),
                "os_servicoprestado": item.get("servico_prestado") or item.get("servicoprestado"),
                "os_data_finalizacao": item.get("data_finalizacao"),
                "os_tecnico_responsavel": item.get("responsavel"),
                "os_tecnicos_auxiliares": item.get("tecnicos_auxiliares") or [],
                "usuario_finalizacao": item.get("usuario_finalizacao"),
            }
            for item in orders
        ]

    def modem_exchanges_report(self, selected_day: date) -> dict[str, Any]:
        """Audit closed OSs and distinguish modem exchanges from removals."""
        rows = self._ura_order_pages({
            "status": 1,
            "data_agendamento_inicio": selected_day.isoformat(),
            "data_agendamento_fim": selected_day.isoformat(),
            "limit": 1000,
        })
        orders = [
            {
                "os_id": item.get("id"),
                "cliente_id": item.get("cliente_id"),
                "contrato_id": item.get("contrato_id") or item.get("contrato"),
                "cliente_cpf": item.get("cliente_cpf") or item.get("cpfcnpj") or item.get("cpf"),
                "cliente": item.get("cliente"),
                "os_motivo_descricao": item.get("motivo"),
                "os_conteudo": item.get("conteudo"),
                "os_data_agendamento": item.get("data_agendamento"),
                "os_data_finalizacao": item.get("data_finalizacao"),
                "os_tecnico_responsavel": item.get("responsavel"),
                "os_tecnicos_auxiliares": item.get("tecnicos_auxiliares") or [],
                "usuario_finalizacao": item.get("usuario_finalizacao"),
            }
            for item in rows if item.get("id")
        ]
        evidence = self.confirmed_removals_for_day(selected_day, orders)
        modem_events = {int(item["os_id"]): item for item in evidence if item.get("os_id")}
        exchanges = {order_id: item for order_id, item in modem_events.items()
                     if item.get("return_type") == "troca"}
        removals = {order_id: item for order_id, item in modem_events.items()
                    if item.get("return_type") == "retirada"}
        items = []
        for order in orders:
            order_id = int(order.get("os_id") or 0)
            modem_event = modem_events.get(order_id)
            return_type = str(modem_event.get("return_type") or "") if modem_event else ""
            items.append({
                "os_id": order_id,
                "client_id": order.get("cliente_id"),
                "client": order.get("cliente") or "Cliente não informado",
                "technician": _closing_technician(order) or order.get("os_tecnico_responsavel") or "Não identificado",
                "reason": order.get("os_motivo_descricao") or "Serviço",
                "scheduled_at": order.get("os_data_agendamento"),
                "closed_at": order.get("os_data_finalizacao"),
                "return_type": return_type,
                "old_gpon": str(modem_event.get("serial") or "")[-6:] if modem_event else "",
                "new_gpon": str(modem_event.get("replacement_serial") or "")[-6:] if modem_event else "",
                "evidence_at": modem_event.get("evidence_at") if modem_event else "",
            })
        items.sort(key=lambda item: (not bool(item["return_type"]), str(item.get("scheduled_at") or ""), item["os_id"]))
        return {
            "date": selected_day.isoformat(), "total": len(items),
            "exchanges": len(exchanges), "removals": len(removals),
            "without_modem_event": len(items) - len(modem_events),
            "items": items,
        }

    def confirmed_removals_for_day(
        self, selected_day: date, closed_orders: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Classify returned modems from closed OS audit histories.

        Unlink without a later link is a withdrawal. Unlink followed by a
        different ONU is an exchange. Re-linking the same ONU is ignored.
        """
        candidates = closed_orders if closed_orders is not None else self._closed_orders_between(selected_day, selected_day)
        if not candidates:
            return []

        # Some list responses omit cliente_id. Open only those candidate OSs;
        # this scan is explicitly requested by the supervisor and never polls.
        for item in candidates:
            if item.get("cliente_id"):
                continue
            try:
                detail = self.order(int(item.get("os_id") or 0), include_onu=False) or {}
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                detail = {}
            item["cliente_id"] = detail.get("cliente_id") or item.get("cliente_id")
            item["usuario_finalizacao"] = detail.get("usuario_finalizacao") or item.get("usuario_finalizacao")
            item["os_tecnico_responsavel"] = detail.get("os_tecnico_responsavel") or item.get("os_tecnico_responsavel")

        cookies = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookies))
        headers = {"User-Agent": "EBF-Tecnico/0.1"}

        def open_page(path: str, body: bytes | None = None, referer: str | None = None) -> str:
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = f"{self.base_url}{referer}"
            request = Request(f"{self.base_url}{path}", data=body, headers=request_headers)
            with opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")

        login_path = "/accounts/login?next=/admin/"
        login_page = open_page(login_path)
        token = re.search(r"csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", login_page)
        if not token:
            raise SGPError("O SGP não liberou a consulta do histórico.", 502)
        login_body = urlencode({
            "csrfmiddlewaretoken": token.group(1),
            "username": self.credentials.username,
            "password": self.credentials.password,
            "next": "/admin/",
        }).encode("utf-8")
        open_page("/accounts/login/", login_body, login_path)

        confirmed: list[dict[str, Any]] = []

        def load_history(client_id: int) -> tuple[int, list[dict[str, str]]]:
            try:
                page = open_page(f"/admin/cliente/{client_id}/historico/")
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                # Um histórico indisponível não pode derrubar toda a consulta.
                return client_id, []
            entries: list[dict[str, str]] = []
            for position, row in enumerate(re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.I | re.S)):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
                values = [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in cells]
                if len(values) >= 5:
                    entries.append({"user": values[1], "evidence": values[3], "at": values[4], "position": position})
            return client_id, entries

        client_ids = list(dict.fromkeys(
            client_id for client_id in (_as_int(item.get("cliente_id")) for item in candidates) if client_id
        ))
        # Four short-lived reads keep the explicit scan responsive without
        # turning it into continuous polling or flooding the SGP.
        history_cache = {}
        if client_ids:
            with ThreadPoolExecutor(max_workers=min(4, len(client_ids))) as pool:
                history_cache = dict(pool.map(load_history, client_ids))

        for order in candidates:
            client_id = _as_int(order.get("cliente_id"))
            if not client_id:
                continue

            closed_text = str(order.get("os_data_finalizacao") or "")
            closed_at = _parse_datetime(closed_text)
            order_id = _as_int(order.get("os_id"))
            # The list may expose only YYYY-MM-DD. Recover the exact closing
            # time from the same audit table to avoid matching an unrelated
            # unlink performed earlier that day.
            if not closed_at or (closed_at.hour == 0 and closed_at.minute == 0):
                close_marker = f"ordem de servico n. {order_id}"
                audit_closures = [
                    _parse_datetime(entry["at"]) for entry in history_cache[client_id]
                    if close_marker in _normalized(entry["evidence"])
                    and "encerrada" in _normalized(entry["evidence"])
                ]
                closed_at = max((value for value in audit_closures if value), default=closed_at)
            history_events = []
            reason = _normalized(order.get("os_motivo_descricao"))
            is_removal_order = any(word in reason for word in ("remocao", "retirada", "remover kit"))
            for entry in history_cache[client_id]:
                normalized_evidence = _normalized(entry["evidence"])
                if "onu" not in normalized_evidence or not any(word in normalized_evidence for word in ("vinculada", "vinculado", "desvinculada", "desvinculado")):
                    continue
                evidence_at = _parse_datetime(entry["at"])
                if evidence_at and evidence_at.date() == selected_day and (
                    is_removal_order or not closed_at or -900 <= (closed_at - evidence_at).total_seconds() <= 21600
                ):
                    serial_match = re.search(r"\bONU\s+([A-Z0-9-]{8,32})\b", entry["evidence"], re.I)
                    if serial_match:
                        action = "unlink" if "desvinculad" in normalized_evidence else "link"
                        history_events.append((evidence_at, int(entry.get("position") or 0), action, serial_match.group(1).upper(), entry))
            modem_returns = _classify_modem_return_events(history_events)
            if not modem_returns:
                continue
            # The ledger currently stores one physical return per OS. Prefer
            # an exchange when repeated audit messages produced more than one
            # candidate, then use the most recent evidence within that class.
            evidence_at, old_serial, return_type, replacement_serial, evidence = modem_returns[0]
            login_match = re.search(r"\bLogin\s+(\d{11})\b", evidence.get("evidence", ""), re.I)
            technician = (
                _closing_technician(order) or order.get("usuario_finalizacao")
                or order.get("os_tecnico_responsavel") or evidence["user"] or "Não identificado"
            )
            confirmed.append({
                "os_id": order_id, "technician": str(technician),
                "client_id": client_id, "client": order.get("cliente"),
                "cpf": order.get("cliente_cpf") or (login_match.group(1) if login_match else ""),
                "serial": old_serial, "replacement_serial": replacement_serial or "", "return_type": return_type,
                "closed_at": closed_text, "evidence_at": evidence_at.isoformat(sep=" "),
                "history_user": evidence["user"],
                "evidence": (
                    f"{evidence['evidence']} | Troca: substituída pela ONU {replacement_serial}"
                    if return_type == "troca" else f"{evidence['evidence']} | Retirada: nenhuma outra ONU vinculada"
                ),
            })
        return confirmed

    def installations_without_materials_for_day(
        self, selected_day: date, closed_orders: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return closed installation OSs whose material ledger is empty."""
        orders = closed_orders if closed_orders is not None else self._closed_orders_between(selected_day, selected_day)
        installations = [
            item for item in orders
            if "instalacao" in _normalized(item.get("os_motivo_descricao")) and item.get("os_id")
        ]

        def inspect(item: dict[str, Any]) -> dict[str, Any] | None:
            try:
                materials = self.order_materials(int(item["os_id"]))
            except (SGPError, OSError, TimeoutError, TypeError, ValueError) as error:
                raise SGPError(f"Consulta incompleta: materiais da OS {item['os_id']} não puderam ser conferidos.", 502) from error
            names = [str(material.get("produto") or material.get("produto_descricao") or "")
                     for material in materials if float(material.get("quantidade") or 0) > 0]
            missing = []
            if "instalacao de kit" in _normalized(item.get("os_motivo_descricao")):
                if not any(_is_modem_product({"descricao": name}) for name in names):
                    missing.append("modem")
                if not any("conector" in _normalized(name) for name in names):
                    missing.append("conector")
            elif not materials:
                missing.append("material")
            if not missing:
                return None
            return {
                "os_id": int(item["os_id"]), "technician": _closing_technician(item) or item.get("os_tecnico_responsavel"),
                "client": item.get("cliente"), "contract_id": item.get("contrato_id"),
                "closed_at": item.get("os_data_finalizacao"),
                "reason": f"{item.get('os_motivo_descricao') or 'Instalação'} · Conferir lançamento de {' e '.join(missing)} (validar exceções; não é cobrança automática)",
            }

        if not installations:
            return []
        with ThreadPoolExecutor(max_workers=min(6, len(installations))) as pool:
            return [item for item in pool.map(inspect, installations) if item]

    def general_daily_material_audit(self, selected_day: date) -> dict[str, Any]:
        """Fetch the day's closed OS list once and reuse it in all audits."""
        orders = self._closed_orders_between(selected_day, selected_day)
        material_rules = self.material_rules_for_day(selected_day, orders)
        installations = [
            {
                "os_id": item["os_id"], "technician": item["technician"], "client": item["client"],
                "contract_id": item.get("contract_id"), "closed_at": item.get("closed_at"),
                "reason": item["reason"],
            }
            for item in material_rules
            if item.get("service") == "Instalação de kit" and item.get("status") != "correct"
        ]
        return {
            "closed_orders": len(orders),
            "removals": self.confirmed_removals_for_day(selected_day, orders),
            "connectors": self.connector_returns_for_day(selected_day, orders),
            "installations": installations,
            "material_rules": material_rules,
        }

    def material_rules_for_day(
        self, selected_day: date, closed_orders: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Check material launches against the operational rules for each closed OS.

        This is deliberately deterministic: a failed or incomplete rule is a
        review item, never an automatic stock correction or charge.
        """
        orders = closed_orders if closed_orders is not None else self._closed_orders_between(selected_day, selected_day)
        candidates = [item for item in orders if item.get("os_id") and _material_rule_names(item)]
        if not candidates:
            return []

        def inspect(item: dict[str, Any]) -> dict[str, Any]:
            order_id = int(item["os_id"])
            try:
                materials = self.order_materials(order_id)
            except (SGPError, OSError, TimeoutError, TypeError, ValueError) as error:
                raise SGPError(f"Consulta incompleta: materiais da OS {order_id} não puderam ser conferidos.", 502) from error
            # Plan information is required only for the Wi-Fi 6 restriction.
            detail: dict[str, Any] = {}
            if "troca de modem" in _normalized(_material_rule_service_text(item)):
                try:
                    detail = self.order(order_id, include_onu=False) or {}
                except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                    # Keep the launch auditable and mark the plan validation as
                    # pending rather than silently approving it.
                    detail = {}
            return _evaluate_material_rules(item, materials, detail)

        with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as pool:
            return sorted(pool.map(inspect, candidates), key=lambda item: (item["status"] == "correct", item["os_id"]))

    def material_analysis_for_day(self, selected_day: date) -> dict[str, Any]:
        """One auditable row per closed OS, including services without a rule yet."""
        orders = self._closed_orders_between(selected_day, selected_day)
        if not orders:
            return {"date": selected_day.isoformat(), "closed_orders": 0, "items": []}

        def inspect(order: dict[str, Any]) -> dict[str, Any]:
            order_id = int(order.get("os_id") or 0)
            try:
                materials = self.order_materials(order_id)
            except (SGPError, OSError, TimeoutError, TypeError, ValueError) as error:
                return {
                    "os_id": order_id, "technician": _closing_technician(order) or order.get("os_tecnico_responsavel") or "Não identificado",
                    "client": order.get("cliente") or "Cliente não informado", "service": _material_rule_service_text(order) or "Serviço não informado",
                    "status": "needs_review", "reason": f"Materiais não puderam ser consultados: {error}", "launched": [], "expected": [],
                }
            if _material_rule_names(order):
                detail = {}
                if "troca de modem" in _normalized(_material_rule_service_text(order)):
                    try: detail = self.order(order_id, include_onu=False) or {}
                    except (SGPError, OSError, TimeoutError, TypeError, ValueError): pass
                return _evaluate_material_rules(order, materials, detail)
            launched = [
                {"name": name or "Material sem nome", "quantity": quantity, "reference": _material_reference(material)}
                for material in materials
                for name, quantity in [_material_name_and_quantity(material)]
                if quantity > 0
            ]
            return {
                "os_id": order_id, "technician": _closing_technician(order) or order.get("os_tecnico_responsavel") or "Não identificado",
                "client": order.get("cliente") or "Cliente não informado", "contract_id": order.get("contrato_id"),
                "closed_at": order.get("os_data_finalizacao"), "service": _material_rule_service_text(order) or "Serviço não informado",
                "status": "needs_review", "reason": "Não há regra automática cadastrada para este tipo de OS.",
                "launched": launched, "expected": [], "actual": {}, "plan_mbps": None,
            }
        with ThreadPoolExecutor(max_workers=min(6, len(orders))) as pool:
            items = list(pool.map(inspect, orders))
        # The history is the source of truth for a physical modem exchange:
        # a material launch alone does not prove that the old ONU was removed
        # or that the new one was linked to this customer.
        modem_order_ids = {int(item.get("os_id") or 0) for item in items if (
            any(_is_modem_product({"descricao": material.get("name")}) for material in item.get("launched", []))
            or any(term in _normalized(item.get("service")) for term in ("modem", "onu", "retirada", "remocao", "corretiva"))
        )}
        modem_orders = [order for order in orders if int(order.get("os_id") or 0) in modem_order_ids]
        history_available = True
        try:
            modem_events = {
                int(event.get("os_id") or 0): event
                for event in (self.confirmed_removals_for_day(selected_day, modem_orders) if modem_orders else [])
            }
        except (SGPError, OSError, TimeoutError, TypeError, ValueError):
            history_available = False
            modem_events = {}
        order_by_id = {int(order.get("os_id") or 0): order for order in orders}
        for item in items:
            order_id = int(item.get("os_id") or 0)
            launched_modems = [material for material in item.get("launched", []) if _is_modem_product({"descricao": material.get("name")})]
            launched_modem = bool(launched_modems)
            modem_event = modem_events.get(order_id)
            if launched_modem:
                replacement = str((modem_event or {}).get("replacement_serial") or "")
                references = [str(material.get("reference") or "") for material in launched_modems if material.get("reference")]
                reference_matches = bool(replacement and any(_same_onu_serial(reference, replacement) for reference in references))
                if not history_available:
                    modem_status, modem_message = "unavailable", "Histórico do cliente indisponível; não foi possível confirmar o modem."
                elif not modem_event or modem_event.get("return_type") != "troca":
                    modem_status, modem_message = "needs_review", "Modem lançado sem troca confirmada no histórico do cliente."
                elif not references:
                    modem_status, modem_message = "needs_review", "Troca confirmada, mas o lançamento não trouxe referência para comparar o GPON."
                elif reference_matches:
                    modem_status, modem_message = "correct", "A referência lançada confere com o novo GPON vinculado no histórico."
                else:
                    modem_status, modem_message = "divergence", "O modem lançado não confere com o novo GPON vinculado no histórico."
                item["modem_history"] = {
                    "checked": history_available, "status": modem_status, "message": modem_message,
                    "launched_references": references,
                    "return_required": bool(modem_event), "return_type": str((modem_event or {}).get("return_type") or ""),
                    "old_gpon": str((modem_event or {}).get("serial") or ""),
                    "new_gpon": replacement,
                }
                if modem_status == "divergence":
                    item["status"] = "divergence"
                    item["reason"] = modem_message
                elif modem_status in ("needs_review", "unavailable") and item.get("status") == "correct":
                    item["status"] = "needs_review"
                    item["reason"] = modem_message
            elif modem_event:
                return_type = str(modem_event.get("return_type") or "")
                item["modem_history"] = {
                    "checked": True, "status": "return", "return_required": True, "return_type": return_type,
                    "message": "Modem antigo deve ser devolvido ao estoque." if return_type == "troca" else "Retirada confirmada: modem deve ser devolvido ao estoque.",
                    "old_gpon": str(modem_event.get("serial") or ""), "new_gpon": str(modem_event.get("replacement_serial") or ""),
                    "launched_references": [],
                }
            else:
                item["modem_history"] = {"checked": history_available, "status": "not_applicable", "return_required": False, "message": "Nenhum modem movimentado nesta OS."}
            service = _material_rule_service_text(order_by_id.get(order_id) or {})
            connector_total = sum(float(material.get("quantity") or 0) for material in item.get("launched", []) if "conector" in _normalized(material.get("name")))
            item["connector_return"] = {
                "required": connector_classification(service) == "return" and connector_total > 0,
                "quantity": connector_total,
                "message": "Conector(es) de corretiva: devem ser devolvidos ao estoque." if connector_classification(service) == "return" and connector_total > 0 else "Sem devolução automática de conector para este tipo de OS.",
            }
        return {"date": selected_day.isoformat(), "closed_orders": len(orders), "items": sorted(items, key=lambda item: item["os_id"])}

    def _enrich_repeat_candidates(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Load full OS data only for client names that occur more than once."""
        counts = Counter(_normalized(item.get("cliente")) for item in orders)
        candidates = [item for item in orders if counts[_normalized(item.get("cliente"))] > 1 and item.get("os_id")]

        def detail(item: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
            try:
                return item.get("os_id"), self.order(int(item["os_id"]), include_onu=False) or {}
            except (SGPError, OSError, TimeoutError, TypeError, ValueError):
                # The list response already contains enough data for repeat
                # scoring. A transient failure while opening one OS must not
                # discard the whole monthly report or force a stale cache.
                return item.get("os_id"), {}

        if candidates:
            with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
                details = dict(pool.map(detail, candidates))
            for item in candidates:
                value = details.get(item.get("os_id"), {})
                services = value.get("servicos") or []
                service = services[0] if services and isinstance(services[0], dict) else {}
                item.update({
                    "cliente_id": value.get("cliente_id") or item.get("cliente_id"),
                    "contrato_id": value.get("contrato_id") or item.get("contrato_id"),
                    "cliente_cpf": value.get("cliente_cpf") or value.get("cliente_cpfcnpj") or value.get("cpfcnpj") or item.get("cliente_cpf"),
                    "servico_login": value.get("servico_login") or service.get("servico_login") or item.get("servico_login"),
                    "endereco": value.get("endereco") or value.get("contrato_endereco") or " ".join(str(part) for part in (
                        value.get("endereco_logradouro"), value.get("endereco_numero"), value.get("endereco_bairro"), value.get("endereco_cidade")
                    ) if part not in (None, "")) or item.get("endereco"),
                    "usuario_finalizacao": value.get("usuario_finalizacao") or item.get("usuario_finalizacao"),
                    "os_servicoprestado": value.get("os_servicoprestado") or value.get("servico_prestado") or item.get("os_servicoprestado"),
                })
        return orders

    def _ura_order_pages(self, form: dict[str, Any]) -> list[dict[str, Any]]:
        result = self.request("POST", "/api/ura/ordemservico/list/", form=form)
        if not isinstance(result, dict):
            return []
        orders = list(result.get("ordens_servicos") or [])
        total = int((result.get("paginacao") or {}).get("total") or len(orders))
        offsets = range(1000, total, 1000)
        if offsets:
            with ThreadPoolExecutor(max_workers=4) as pool:
                pages = pool.map(
                    lambda offset: self.request(
                        "POST", "/api/ura/ordemservico/list/", form={**form, "offset": offset}
                    ),
                    offsets,
                )
                for page in pages:
                    if isinstance(page, dict):
                        orders.extend(page.get("ordens_servicos") or [])
        return orders

    def _materials_for_user(self, start: date, end: date, user_id: int) -> list[dict[str, Any]]:
        launches = self.request(
            "GET",
            "/api/estoque/lancamento/list/",
            query={"tipo": 6, "data_cadastro_ini": start.isoformat(), "data_cadastro_fim": end.isoformat()},
        )
        selected = [
            launch for launch in launches if _as_int(launch.get("usuario_id")) == _as_int(user_id)
        ] if isinstance(launches, list) else []
        totals: defaultdict[str, float] = defaultdict(float)
        references: defaultdict[str, int] = defaultdict(int)

        def fetch(launch: dict[str, Any]) -> list[dict[str, Any]]:
            result = self.request(
                "GET", "/api/estoque/lancamentoitem/list/", query={"lancamento_id": launch.get("id")}
            )
            return result if isinstance(result, list) else []

        with ThreadPoolExecutor(max_workers=8) as pool:
            for items in pool.map(fetch, selected):
                for item in items:
                    if item.get("estornado") or not item.get("aplicado", True):
                        continue
                    name = str(item.get("produto") or "Material não identificado")
                    totals[name] += float(item.get("quantidade") or 0)
                    if item.get("referencia"):
                        references[name] += 1
        return [
            {"name": name, "quantity": round(quantity, 2), "serialized": references[name]}
            for name, quantity in sorted(totals.items(), key=lambda entry: (-entry[1], entry[0].casefold()))
        ]


def _parse_references(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        while isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
            value = value[0]
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


SCORE_RULES = (
    ("Reativação de cliente cancelado", 2.0, ("reativacao de cliente cancelado", "reativacao cliente cancelado")),
    ("Instalação de TV", 1.0, ("instalacao de tv", "instalacao tv")),
    ("Mudança de endereço", 1.5, ("mudanca de endereco",)),
    ("Remoção de kit", 0.5, ("remocao de kit", "retirada de kit")),
    ("Corretiva", 1.0, ("corretiva", "manutencao corretiva")),
    ("Mudança de plano", 0.5, ("mudanca de plano",)),
    ("Rede mesh (repetidor)", 1.0, ("rede mesh", "repetidor", "mesh")),
    ("Instalação", 2.0, ("instalacao",)),
)


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.casefold().split())


def _address_text(value: Any) -> str:
    """Turn nested SGP address values into readable street text."""
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        preferred = (
            "logradouro", "rua", "endereco", "numero", "complemento", "bairro",
            "cidade", "municipio", "uf", "estado", "cep",
        )
        parts = []
        for key in preferred:
            if key in value:
                text = _address_text(value.get(key))
                if text and text not in parts:
                    parts.append(text)
        if not parts:
            parts = [_address_text(item) for item in value.values()]
        return ", ".join(item for item in parts if item)
    if isinstance(value, (list, tuple)):
        return ", ".join(item for item in (_address_text(part) for part in value) if item)
    return str(value).strip()


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    candidates = [text]
    candidates.extend(re.findall(
        r"(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\s+\d{2}:\d{2}(?::\d{2})?",
        text,
    ))
    for candidate in candidates:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
            try:
                return datetime.strptime(candidate[:19], pattern)
            except ValueError:
                continue
    return None


def _route_order_start(order: dict[str, Any]) -> datetime | None:
    details = order.get("os_djson") if isinstance(order.get("os_djson"), dict) else {}
    for key in (
        "checkin_data", "os_checkin_data", "os_data_checkin", "data_checkin",
        "inicio_execucao", "os_data_inicio", "data_inicio", "os_data_alteracao",
    ):
        parsed = _parse_datetime(order.get(key) or details.get(key))
        if parsed:
            return parsed
    return None


def build_route_monitor(
    orders: list[dict[str, Any]],
    technicians: tuple[str, ...] = ROUTE_MONITOR_TECHNICIANS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize the daily OS list into a small supervisor route monitor."""
    now = (now or datetime.now(ZoneInfo("America/Sao_Paulo"))).replace(tzinfo=None)
    rows: list[dict[str, Any]] = []
    for technician in technicians:
        services: list[dict[str, Any]] = []
        for order in orders:
            names = [
                order.get("os_tecnico_responsavel") or order.get("responsavel"),
                *(order.get("os_tecnicos_auxiliares") or order.get("tecnicos_auxiliares") or []),
            ]
            if not any(_same_person(name, technician) for name in names if name):
                continue
            try:
                status = int(order.get("os_status", order.get("status_id", 0)))
            except (TypeError, ValueError):
                status = 0
            started = _route_order_start(order)
            finished = _parse_datetime(order.get("os_data_finalizacao") or order.get("data_finalizacao"))
            scheduled = _parse_datetime(order.get("os_data_agendamento") or order.get("data_agendamento"))
            elapsed: int | None = None
            if started and status == 2:
                elapsed = max(0, int((now - started).total_seconds()))
            elif started and finished:
                elapsed = max(0, int((finished - started).total_seconds()))
            level = "unknown" if status == 2 and elapsed is None else "normal"
            if status == 2 and elapsed is not None:
                level = "urgent" if elapsed >= 7200 else "attention" if elapsed >= 3600 else "normal"
            duration_level = "unknown" if elapsed is None else (
                "urgent" if elapsed >= 7200 else "attention" if elapsed >= 3600 else "normal"
            )
            services.append({
                "id": order.get("os_id") or order.get("id"),
                "client": order.get("cliente") or "Cliente não informado",
                "reason": order.get("os_motivo_descricao") or order.get("motivo") or "Serviço",
                "status": status,
                "status_text": order.get("os_status_txt") or order.get("status") or {0: "Aberta", 1: "Encerrada", 2: "Em execução", 3: "Pendente"}.get(status, "Desconhecida"),
                "scheduled_at": scheduled.isoformat() if scheduled else None,
                "started_at": started.isoformat() if started else None,
                "finished_at": finished.isoformat() if finished else None,
                "elapsed_seconds": elapsed,
                "level": level,
                "duration_level": duration_level,
            })
        services.sort(key=lambda item: (item["scheduled_at"] or "", int(item["id"] or 0)))
        active = [item for item in services if item["status"] == 2]
        current = max(active, key=lambda item: int(item["elapsed_seconds"] or -1), default=None)
        rows.append({
            "technician": technician,
            "current": current,
            "services": services,
            "counts": {
                "total": len(services),
                "open": sum(item["status"] == 0 for item in services),
                "closed": sum(item["status"] == 1 for item in services),
                "running": len(active),
                "pending": sum(item["status"] == 3 for item in services),
            },
            "level": current["level"] if current else "idle",
        })
    alerts = sorted(
        (
            {
                "technician": row["technician"],
                **service,
            }
            for row in rows
            for service in row["services"]
            if service["duration_level"] in ("attention", "urgent")
        ),
        key=lambda item: (item["duration_level"] != "urgent", -(item["elapsed_seconds"] or 0)),
    )
    return {
        "date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "technicians": rows,
        "alerts": alerts,
        "total_orders": len({str(order.get("os_id") or order.get("id")) for order in orders if order.get("os_id") or order.get("id")}),
    }


def _is_modem_product(product: dict[str, Any]) -> bool:
    description = _normalized(product.get("descricao"))
    return any(
        term in description
        for term in (
            "modem", "onu", "ont", "wifi", "wi-fi", "roteador",
            "4 antenas", "quatro antenas", "zte basica", "zte basico",
            "huawei basica", "huawei basico",
        )
    )


def _material_rule_service_text(order: dict[str, Any]) -> str:
    """Use only the OS service fields; free-text technician notes are evidence, not type."""
    return " ".join(str(order.get(key) or "") for key in (
        "os_motivo_descricao", "motivo", "tipo", "os_servicoprestado", "servico_prestado",
    ))


def _material_rule_names(order: dict[str, Any]) -> list[str]:
    text = _normalized(_material_rule_service_text(order))
    rules: list[str] = []
    # Specific services take precedence over incidental wording in another
    # field. The two connector spellings remain separate by business rule.
    if "instalacao de kit" in text:
        rules.append("Instalação de kit")
    elif "mudanca de endereco" in text:
        rules.append("Mudança de endereço")
    else:
        if "troca de modem" in text:
            rules.append("Troca de modem")
        if "troca de conectores" in text:
            rules.append("Troca de conectores")
        elif "troca de conector" in text:
            rules.append("Troca de conector")
        if "troca de drop" in text:
            rules.append("Troca de drop")
    return rules


def _material_name_and_quantity(item: dict[str, Any]) -> tuple[str, float]:
    raw_name = item.get("produto") or item.get("produto_descricao") or item.get("descricao") or ""
    if isinstance(raw_name, dict):
        raw_name = raw_name.get("descricao") or raw_name.get("nome") or ""
    try:
        quantity = float(item.get("quantidade", item.get("quantity", 0)) or 0)
    except (TypeError, ValueError):
        quantity = 0.0
    return str(raw_name), max(0.0, quantity)


def _material_reference(item: dict[str, Any]) -> str:
    """Return the first serialized stock reference in a stable text form."""
    raw = item.get("referencia") or item.get("reference") or item.get("serial") or ""
    if isinstance(raw, dict):
        raw = raw.get("referencia") or raw.get("reference") or raw.get("serial") or ""
    if isinstance(raw, list):
        flat = _parse_references(raw)
        raw = (flat[0].get("referencia") if flat else "")
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def _same_onu_serial(left: Any, right: Any) -> bool:
    """Compare printed ZTE/Huawei labels with the OLT hexadecimal serial."""
    a = SGPClient.onu_search_serial(str(left or ""))
    b = SGPClient.onu_search_serial(str(right or ""))
    return bool(a and b and (a == b or (len(a) >= 6 and len(b) >= 6 and a[-6:] == b[-6:])))


def _material_plan_mbps(order: dict[str, Any]) -> int | None:
    """Recover a plan speed from the OS detail without trusting customer notes."""
    values: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if any(term in _normalized(key) for term in ("plano", "velocidade", "banda", "servico")):
                    walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
        elif value is not None:
            values.append(str(value))

    walk(order)
    for value in values:
        match = re.search(r"\b(\d{2,4})\s*(?:mega|mbps|mb)\b", _normalized(value))
        if match:
            return int(match.group(1))
    return None


def _evaluate_material_rules(
    order: dict[str, Any], materials: list[dict[str, Any]], detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an explainable material audit for one OS.

    Green and blue connectors intentionally count together. The result is
    suitable for a supervisor review and must never trigger a stock movement.
    """
    counts = {"conectores": 0.0, "drop": 0.0, "cunha": 0.0, "modem": 0.0, "wifi_6": 0.0}
    launched: list[dict[str, Any]] = []
    for material in materials:
        name, quantity = _material_name_and_quantity(material)
        if quantity <= 0:
            continue
        normalized = _normalized(name)
        launched.append({"name": name or "Material sem nome", "quantity": quantity, "reference": _material_reference(material)})
        if "conector" in normalized:
            counts["conectores"] += quantity
        if "drop" in normalized:
            counts["drop"] += quantity
        if "cunha" in normalized:
            counts["cunha"] += quantity
        if _is_modem_product({"descricao": name}):
            counts["modem"] += quantity
            if "wifi 6" in normalized or "wifi6" in normalized:
                counts["wifi_6"] += quantity

    requirements = {
        "Troca de drop": {"drop": 1, "cunha": 1},
        "Troca de conector": {"conectores": 1},
        "Troca de conectores": {"conectores": 2},
        "Troca de modem": {"modem": 1},
        "Mudança de endereço": {"conectores": 2, "drop": 1, "cunha": 1},
        "Instalação de kit": {"conectores": 2, "modem": 1, "drop": 1, "cunha": 1},
    }
    labels = {"conectores": "conector(es)", "drop": "drop", "cunha": "cunha", "modem": "modem"}
    rule_names = _material_rule_names(order)
    expected: dict[str, int] = {}
    for rule_name in rule_names:
        for material, minimum in requirements[rule_name].items():
            expected[material] = max(expected.get(material, 0), minimum)
    missing = [labels[key] for key, minimum in expected.items() if counts[key] < minimum]
    review: list[str] = []
    plan_mbps = _material_plan_mbps(detail or {}) if counts["wifi_6"] else None
    if counts["wifi_6"]:
        if plan_mbps is None:
            review.append("Modem Wi‑Fi 6 lançado: plano não localizado para validar o mínimo de 500 Mega.")
        elif plan_mbps < 500:
            missing.append(f"plano de pelo menos 500 Mega para modem Wi‑Fi 6 (plano localizado: {plan_mbps} Mega)")

    status = "divergence" if missing else "needs_review" if review else "correct"
    reason = "Conferido pelas regras de material"
    if missing:
        reason = "Material esperado não lançado: " + ", ".join(missing)
    elif review:
        reason = " ".join(review)
    return {
        "os_id": int(order.get("os_id") or order.get("id") or 0),
        "technician": _closing_technician(order) or order.get("os_tecnico_responsavel") or "Não identificado",
        "client": order.get("cliente") or "Cliente não informado",
        "contract_id": order.get("contrato_id") or order.get("contrato"),
        "closed_at": order.get("os_data_finalizacao"),
        "service": " + ".join(rule_names),
        "expected": [{"material": labels[key], "minimum": minimum} for key, minimum in expected.items()],
        "actual": counts,
        "launched": launched,
        "plan_mbps": plan_mbps,
        "status": status,
        "reason": reason,
    }


def _score_order(order: dict[str, Any]) -> tuple[str, float]:
    reason = _normalized(" ".join(filter(None, [order.get("os_motivo_descricao"), order.get("os_conteudo")])))
    for label, points, terms in SCORE_RULES:
        if any(term in reason for term in terms):
            return label, points
    return "Outros", 0.0


def _same_person(left: Any, right: Any) -> bool:
    # SGP may return the same user as "Bruno  Bezerra", "Bruno_Bezerra" or
    # an abbreviated login. Compare only letters and digits before applying
    # the existing exact/prefix rule.
    a, b = _identity_value(left), _identity_value(right)
    if a and b and (a == b or (min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a)))):
        return True
    left_parts = re.findall(r"[a-z0-9]+", _normalized(left))
    right_parts = re.findall(r"[a-z0-9]+", _normalized(right))
    if left_parts and right_parts and (len(left_parts) == 1 or len(right_parts) == 1):
        # Stock/location records may expose only "Leonardo", while closed OS
        # records use "Leo Lira." / "LEO-LIRA".
        left_first, right_first = left_parts[0], right_parts[0]
        if min(len(left_first), len(right_first)) >= 3 and (
            left_first.startswith(right_first) or right_first.startswith(left_first)
        ):
            return True
    return bool(
        len(left_parts) >= 2 and len(right_parts) >= 2
        and left_parts[-1] == right_parts[-1]
        and min(len(left_parts[0]), len(right_parts[0])) >= 3
        and (left_parts[0].startswith(right_parts[0]) or right_parts[0].startswith(left_parts[0]))
    )


def _closing_technician(order: dict[str, Any]) -> str | None:
    """Return the technical profile that actually closed the OS, if identifiable."""
    closer = order.get("usuario_finalizacao")
    candidates = [order.get("os_tecnico_responsavel"), *(order.get("os_tecnicos_auxiliares") or [])]
    for candidate in candidates:
        if candidate and _same_person(closer, candidate):
            return str(candidate)
    return None


def _html_attr(attrs: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*(['\"])(.*?)\1", str(attrs or ""), re.I | re.S)
    if not match:
        match = re.search(rf"\b{re.escape(name)}\s*=\s*([^\s>]+)", str(attrs or ""), re.I)
    return html.unescape(match.group(2) if match and match.lastindex == 2 else match.group(1) if match else "")


def _identity_value(value: Any) -> str:
    return "".join(character for character in _normalized(value) if character.isalnum())


def _same_customer(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Confirm a customer using stable IDs plus independent human-readable fields."""
    strong_fields = ("cliente_id", "contrato_id", "cliente_cpf", "servico_login")
    for field in strong_fields:
        a, b = _identity_value(left.get(field)), _identity_value(right.get(field))
        if a and b and a == b:
            return True
    name_equal = _identity_value(left.get("cliente")) == _identity_value(right.get("cliente"))
    address_equal = bool(_identity_value(left.get("endereco"))) and _identity_value(left.get("endereco")) == _identity_value(right.get("endereco"))
    return bool(name_equal and address_equal)


def _repeat_adjustments(orders: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Calculate calendar-month repeat penalties without penalizing unconfirmed visits."""
    eligible = []
    for order in orders:
        repeat_reason = _normalized(
            order.get("os_motivo_descricao") or order.get("os_conteudo") or ""
        )
        if "entrega de boleto" in repeat_reason:
            continue
        closer = _closing_technician(order)
        has_identity = any(order.get(field) not in (None, "") for field in ("cliente_id", "contrato_id", "cliente_cpf", "servico_login")) or bool(order.get("cliente") and order.get("endereco"))
        if closer and has_identity and _order_date(order) != date.min:
            eligible.append((order, closer))
    eligible.sort(key=lambda row: (str(row[0].get("os_data_finalizacao") or ""), int(row[0].get("os_id") or 0)))

    first_visits: list[tuple[dict[str, Any], str]] = []
    technician_first_visits: list[tuple[dict[str, Any], str]] = []
    first_penalized: set[Any] = set()
    result: dict[str, dict[str, Any]] = {}

    def bucket(name: str) -> dict[str, Any]:
        return result.setdefault(_normalized(name), {"count": 0, "lost_points": 0.0, "details": []})

    for order, closer in eligible:
        first_visit = next(((first, technician) for first, technician in first_visits if _same_customer(first, order)), None)
        if first_visit is None:
            first_visits.append((order, closer))
            technician_first_visits.append((order, closer))
            continue
        technician_visit = next((
            (first, technician)
            for first, technician in technician_first_visits
            if _same_customer(first, order) and _same_person(technician, closer)
        ), None)
        if technician_visit:
            first_order, first_technician = technician_visit
        else:
            first_order, first_technician = first_visit
            technician_first_visits.append((order, closer))
        first_key = first_order.get("os_id")
        same_technician = _same_person(closer, first_technician)
        original_suspended = False
        if same_technician:
            lost = _score_order(order)[1]
            if first_key not in first_penalized:
                lost += _score_order(first_order)[1]
                first_penalized.add(first_key)
                original_suspended = True
        elif first_key not in first_penalized:
            lost = _score_order(first_order)[1]
            first_penalized.add(first_key)
            original_suspended = True
        else:
            lost = 0.0
        target = bucket(first_technician)
        target["count"] += 1
        target["lost_points"] += lost
        target["details"].append({
            "client": order.get("cliente") or first_order.get("cliente"),
            "client_id": order.get("cliente_id") or first_order.get("cliente_id"),
            "original_order_id": first_order.get("os_id"),
            "original_type": _score_order(first_order)[0],
            "original_reason": first_order.get("os_motivo_descricao") or first_order.get("os_conteudo"),
            "original_service_performed": first_order.get("os_servicoprestado"),
            "original_date": str(first_order.get("os_data_finalizacao") or "")[:10],
            "original_technician": first_technician,
            "repeat_order_id": order.get("os_id"),
            "repeat_type": _score_order(order)[0],
            "repeat_reason": order.get("os_motivo_descricao") or order.get("os_conteudo"),
            "repeat_service_performed": order.get("os_servicoprestado"),
            "repeat_date": str(order.get("os_data_finalizacao") or "")[:10],
            "repeat_technician": closer,
            "same_technician": same_technician,
            "original_suspended": original_suspended,
            "original_points": _score_order(first_order)[1],
            "repeat_points": _score_order(order)[1],
            "points_lost": lost,
        })
    return result


def _apply_repeat_refunds(
    repeat_data: dict[str, dict[str, Any]], refunds: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    indexed = {
        (
            int(item.get("original_order_id") or 0),
            int(item.get("repeat_order_id") or 0),
            _normalized(item.get("technician")),
        ): item
        for item in refunds
    }
    for technician_key, bucket in repeat_data.items():
        effective_loss = 0.0
        refunded_points = 0.0
        for detail in bucket.get("details", []):
            refund = indexed.get((
                int(detail.get("original_order_id") or 0),
                int(detail.get("repeat_order_id") or 0),
                technician_key,
            ))
            detail["refunded"] = bool(refund)
            detail["refunded_by"] = refund.get("refunded_by") if refund else None
            detail["refunded_at"] = refund.get("refunded_at") if refund else None
            detail["restored_order_id"] = (
                refund.get("restored_order_id")
                or (detail.get("repeat_order_id") if detail.get("same_technician") else detail.get("original_order_id"))
            ) if refund else None
            detail["restored_order_ids"] = (
                [detail.get("original_order_id"), detail.get("repeat_order_id")]
                if detail.get("restored_order_id") == -1
                else ([detail.get("restored_order_id")] if detail.get("restored_order_id") else [])
            )
            restored = float(refund.get("points") or 0) if refund else 0.0
            detail["points_deducted"] = max(0.0, float(detail.get("points_lost") or 0) - restored)
            if refund:
                refunded_points += restored
                effective_loss += detail["points_deducted"]
            else:
                effective_loss += float(detail.get("points_lost") or 0)
        bucket["lost_points"] = round(effective_loss, 2)
        bucket["refunded_points"] = round(refunded_points, 2)
    return repeat_data


def _order_date(order: dict[str, Any]) -> date:
    value = str(order.get("os_data_finalizacao") or "")[:10]
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return date.min


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coordinates(raw: Any) -> tuple[float, float] | None:
    try:
        latitude, longitude = (float(part.strip()) for part in str(raw).split(",")[:2])
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None
        return latitude, longitude
    except (TypeError, ValueError):
        return None
