from __future__ import annotations

import json
import mimetypes
import math
import os
import secrets
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from app.sgp import ADMIN_ENDPOINTS_ONLY, Credentials, SGPClient, SGPError, _business_today, _scheduled_on
from app.operations import assign, assignments_for, completed_between, confirm_covered_street, covered_street, delete_onu_relink_draft, event, onu_relink_drafts, platform_users, popup_access_members, record_user, refund_repeat, repeat_refunds, role_for, save_completed_orders, save_location, save_onu_relink_draft, set_popup_access_member, set_user_role, snapshot, sync_orders


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
SGP_BASE_URL = os.getenv("SGP_BASE_URL", "https://sgfibra.sgp.tsmx.app")
SESSION_TTL = 12 * 60 * 60
REMEMBERED_SESSION_TTL = 30 * 24 * 60 * 60
SESSIONS: dict[str, dict[str, Any]] = {}
SESSION_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
KNOWN_ADMIN_ORDERS: dict[int, dict[str, Any]] = {}
KNOWN_ADMIN_ORDERS_LOCK = threading.Lock()
ADMIN_OVERVIEW_CACHE: dict[str, dict[str, Any]] = {}
ADMIN_OVERVIEW_INFLIGHT: dict[str, threading.Event] = {}
ADMIN_OVERVIEW_LOCK = threading.Lock()


def _clear_admin_overview_cache() -> None:
    with ADMIN_OVERVIEW_LOCK:
        ADMIN_OVERVIEW_CACHE.clear()


def _clear_route_caches() -> None:
    with SESSION_LOCK:
        for active_session in SESSIONS.values():
            active_session.pop("orders_cache", None)


def _has_admin_access(user: dict[str, Any]) -> bool:
    cargos = {str(item.get("descricao", "")).casefold().strip() for item in user.get("cargos", [])}
    username = str(user.get("usuario", "")).casefold().strip()
    assigned_role = role_for(username)
    return assigned_role in ("admin", "superadmin") or (not assigned_role and "administrador" in cargos)


def _new_session(credentials: Credentials, user: dict[str, Any], ttl: int = SESSION_TTL) -> str:
    session_id = secrets.token_urlsafe(32)
    with SESSION_LOCK:
        username = str(user.get("usuario", "")); role = role_for(username) or "technician"
        modes = ["technician"] + (["admin"] if _has_admin_access(user) else []) + (["superadmin"] if role == "superadmin" else [])
        popup_enabled = any(str(item.get("username") or "").casefold() == username.casefold() for item in popup_access_members())
        user = {**user, "role": role, "modes": modes, "mode": None if len(modes) > 1 else modes[0], "popup_enabled": popup_enabled}
        record_user(username, str(user.get("nome", username)), login=True)
        SESSIONS[session_id] = {
            "credentials": credentials,
            "user": user,
            "expires": time.time() + ttl,
        }
    return session_id


class Handler(BaseHTTPRequestHandler):
    server_version = "EBF-Tecnico"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            return self._json({"ok": True, "service": "ebf-tecnico"})
        if path == "/api/me":
            return self._authenticated(lambda client, session: session["user"])
        if path == "/api/admin/overview":
            query = parse_qs(parsed.query)
            period = query.get("period", ["day"])[0]
            force = query.get("refresh", ["0"])[0] == "1"
            if period not in ("day", "week", "month"): return self._json({"error": "Período inválido."}, 400)
            return self._authenticated(lambda client, session: self._admin_overview(client, session, period, force))
        if path == "/api/admin/activity":
            selected_date = parse_qs(parsed.query).get("date", [date.today().isoformat()])[0]
            period = parse_qs(parsed.query).get("period", ["day"])[0]
            try: selected = date.fromisoformat(selected_date)
            except ValueError: return self._json({"error": "Data inválida."}, 400)
            if period not in ("day", "week"): return self._json({"error": "Período inválido."}, 400)
            return self._authenticated(lambda client, session: self._admin_activity(client, session, selected, period))
        if path == "/api/admin/address":
            query = parse_qs(parsed.query)
            cep = query.get("cep", [""])[0]
            number = query.get("number", [""])[0]
            return self._authenticated(lambda client, session: self._admin_address_search(client, session, cep, number))
        if path == "/api/admin/stock":
            location_value = parse_qs(parsed.query).get("location", [None])[0]
            if location_value is None:
                return self._authenticated(lambda client, session: self._admin_stock_locations(client, session))
            try: location_id = int(location_value)
            except ValueError: return self._json({"error": "Estoque inválido."}, 400)
            return self._authenticated(lambda client, session: self._admin_stock(client, session, location_id))
        if path == "/api/admin/transfer":
            query = parse_qs(parsed.query)
            try:
                origin_id = int(query.get("origin", [""])[0])
                destination_id = int(query.get("destination", [""])[0])
            except ValueError:
                return self._json({"error": "Selecione os estoques de origem e destino."}, 400)
            return self._authenticated(lambda client, session: self._admin_transfer_options(client, session, origin_id, destination_id))
        if path == "/api/superadmin/users":
            return self._authenticated(lambda _client, session: self._superadmin_users(session))
        if path == "/api/superadmin/scores":
            return self._authenticated(lambda client, session: self._superadmin_scores(client, session))
        if path == "/api/orders":
            status_value = parse_qs(parsed.query).get("status", [None])[0]
            if status_value is None:
                return self._authenticated(lambda client, session: self._cached_technician_orders(client, session))
            try:
                status = int(status_value)
            except ValueError:
                return self._json({"error": "Situação inválida."}, 400)
            return self._authenticated(
                lambda client, session: self._cached_technician_orders(client, session, status)
            )
        if path.startswith("/api/orders/"):
            if "/photos/" in path:
                parts = path.strip("/").split("/")
                try:
                    attachment_id = int(parts[4])
                except (ValueError, IndexError):
                    return self._json({"error": "Anexo inválido."}, 400)
                return self._authenticated_binary(lambda client: client.order_photo(attachment_id))
            if path.endswith("/photos"):
                try:
                    order_id = int(path.strip("/").split("/")[2])
                except (ValueError, IndexError):
                    return self._json({"error": "OS inválida."}, 400)
                return self._authenticated(lambda client, _session: client.order_photos(order_id))
            if path.endswith("/materials"):
                try:
                    order_id = int(path.strip("/").split("/")[2])
                except (ValueError, IndexError):
                    return self._json({"error": "OS inválida."}, 400)
                return self._authenticated(lambda client, _session: client.order_materials(order_id))
            try:
                order_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._json({"error": "OS inválida."}, 400)
            return self._authenticated(lambda client, _session: client.order(order_id))
        if path == "/api/stock":
            return self._authenticated(
                lambda client, session: client.stock_for_user(
                    session["user"].get("nome", ""), session["user"].get("usuario", "")
                )
            )
        if path == "/api/olts":
            return self._authenticated(lambda client, _session: client.olts())
        if path == "/api/report":
            period = parse_qs(parsed.query).get("period", ["week"])[0]
            if period not in ("day", "week", "month"):
                return self._json({"error": "Período inválido."}, 400)
            return self._authenticated(
                lambda client, session: self._cached_report(client, session, period)
            )
        if path == "/api/map/orders":
            return self._authenticated(lambda client, session: self._cached_map(client, session))
        if path == "/api/route":
            if not self._current_session():
                return self._json({"error": "Sessão expirada. Entre novamente."}, 401)
            return self._route(parse_qs(parsed.query))
        if path == "/api/onu/options":
            return self._authenticated(lambda client, _session: client.onu_options())
        if path == "/api/cto/verify":
            raw_ids = parse_qs(parsed.query).get("contracts", [""])[0]
            try:
                contract_ids = [int(value) for value in raw_ids.replace("\n", ",").split(",") if value.strip()]
            except ValueError:
                return self._json({"error": "Informe apenas IDs de contrato separados por vírgula."}, 400)
            return self._authenticated(lambda client, _session: client.verify_cto_contracts(contract_ids))
        if path == "/api/cto/pon-status":
            query = parse_qs(parsed.query)
            try:
                olt_id = int(query.get("olt_id", [""])[0])
                slot = int(query.get("slot", [""])[0])
                pon = int(query.get("pon", [""])[0])
            except ValueError:
                return self._json({"error": "OLT, slot ou PON inválido."}, 400)
            return self._authenticated(lambda client, _session: client.pon_connection_status(olt_id, slot, pon))
        if path.startswith("/api/olts/") and path.endswith("/unauthorized"):
            try:
                olt_id = int(path.strip("/").split("/")[2])
            except (ValueError, IndexError):
                return self._json({"error": "OLT inválida."}, 400)
            return self._authenticated(lambda client, _session: self._unauthorized_onus(client, olt_id))
        if path.startswith("/api/olts/") and path.endswith("/splitters"):
            try:
                olt_id = int(path.strip("/").split("/")[2])
            except (ValueError, IndexError):
                return self._json({"error": "OLT inválida."}, 400)
            query = parse_qs(parsed.query)
            slot = query.get("slot", [""])[0]
            pon = query.get("pon", [""])[0]
            return self._authenticated(lambda client, _session: client.splitters(olt_id, slot, pon))
        return self._static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            session_id = self._session_id()
            if session_id:
                with SESSION_LOCK:
                    SESSIONS.pop(session_id, None)
            self.send_response(204)
            self.send_header("Set-Cookie", "ebf_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.end_headers()
            return
        if path == "/api/mode":
            body = self._read_json(); mode = str(body.get("mode", ""))
            return self._authenticated(lambda _client, session: self._set_mode(session, mode))
        if path == "/api/location":
            body = self._read_json()
            return self._authenticated(lambda _client, session: self._save_location(session, body))
        if path == "/api/admin/transfer":
            body = self._read_json()
            return self._authenticated(lambda client, session: self._admin_transfer(client, session, body))
        if path == "/api/admin/address/coverage":
            body = self._read_json()
            return self._authenticated(lambda _client, session: self._confirm_address_coverage(session, body))
        if path == "/api/superadmin/users/role":
            body = self._read_json()
            return self._authenticated(lambda _client, session: self._set_user_role(session, body))
        if path == "/api/superadmin/scores/refund":
            body = self._read_json()
            return self._authenticated(lambda client, session: self._refund_repeat_score(client, session, body))
        if path.startswith("/api/admin/orders/") and path.endswith("/assign"):
            try: order_id = int(path.strip("/").split("/")[3])
            except (ValueError, IndexError): return self._json({"error": "OS inválida."}, 400)
            body = self._read_json()
            return self._authenticated(lambda _client, session: self._assign_order(session, order_id, body))
        if path.startswith("/api/admin/orders/") and path.endswith("/edit"):
            try: order_id = int(path.strip("/").split("/")[3])
            except (ValueError, IndexError): return self._json({"error": "OS inválida."}, 400)
            body = self._read_json()
            return self._authenticated(lambda client, session: self._admin_edit_order(client, session, order_id, body))
        if path.startswith("/api/orders/") and path.endswith("/status"):
            try:
                order_id = int(path.strip("/").split("/")[2])
            except (ValueError, IndexError):
                return self._json({"error": "OS inválida."}, 400)
            body = self._read_json()
            try:
                status = int(body.get("status"))
            except (TypeError, ValueError):
                return self._json({"error": "Situação inválida."}, 400)
            service_performed = str(body.get("service_performed", ""))[:4000]
            return self._authenticated(
                lambda client, session: self._change_order_status(
                    client, session, order_id, status, service_performed
                )
            )
        if path.startswith("/api/orders/") and path.endswith("/materials"):
            try:
                order_id = int(path.strip("/").split("/")[2])
            except (ValueError, IndexError):
                return self._json({"error": "OS inválida."}, 400)
            body = self._read_json()
            items = body.get("items") if isinstance(body.get("items"), list) else []
            return self._authenticated(
                lambda client, session: client.add_order_materials(
                    order_id, session["user"].get("nome", ""), session["user"].get("usuario", ""), items
                )
            )
        if path.startswith("/api/orders/") and path.endswith("/photos"):
            try:
                order_id = int(path.strip("/").split("/")[2])
            except (ValueError, IndexError):
                return self._json({"error": "OS inválida."}, 400)
            body = self._read_json(max_bytes=12 * 1024 * 1024)
            return self._authenticated(
                lambda client, _session: client.add_order_photo(
                    order_id, str(body.get("description", ""))[:500], str(body.get("filename", ""))[:180],
                    str(body.get("content_type", "")), str(body.get("data", "")),
                )
            )
        if path.startswith("/api/olts/") and path.endswith("/authorize"):
            try:
                olt_id = int(path.strip("/").split("/")[2])
            except (ValueError, IndexError):
                return self._json({"error": "OLT inválida."}, 400)
            body = self._read_json()
            return self._authenticated(lambda client, _session: self._authorize_onu(client, olt_id, body))
        if path.startswith("/api/onus/") and path.endswith("/deauthorize"):
            parts = path.strip("/").split("/")
            if len(parts) != 4 or not parts[2]:
                return self._json({"error": "ONU inválida."}, 400)
            identifier = unquote(parts[2])
            body = self._read_json()
            return self._authenticated(lambda client, _session: self._deauthorize_onu(client, identifier, body))
        self._json({"error": "Rota não encontrada."}, 404)

    def _unauthorized_onus(self, client: SGPClient, olt_id: int) -> list[dict[str, Any]]:
        drafts = onu_relink_drafts()
        result = []
        for onu in client.unauthorized_onus(olt_id):
            serial = str(onu.get("phy_addr") or onu.get("id") or "").strip()
            draft = drafts.get(serial.casefold())
            result.append({**onu, "saved_relink": draft} if draft else onu)
        return result

    def _authorize_onu(self, client: SGPClient, olt_id: int, body: dict[str, Any]) -> dict[str, Any]:
        result = client.authorize_onu(olt_id, body)
        delete_onu_relink_draft(str(body.get("id", "")))
        return result

    def _deauthorize_onu(
        self, client: SGPClient, identifier: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        result = client.deauthorize_onu(identifier)
        values = body.get("values") if isinstance(body.get("values"), dict) else {}
        serial = str(body.get("serial") or identifier)
        save_onu_relink_draft(serial, int(body.get("order_id") or 0), values)
        return result

    def _login(self) -> None:
        remote = self.headers.get("CF-Connecting-IP") or self.client_address[0]
        now = time.time()
        attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(remote, []) if now - stamp < 600]
        LOGIN_ATTEMPTS[remote] = attempts
        if len(attempts) >= 5:
            return self._json({"error": "Muitas tentativas. Aguarde 10 minutos."}, 429)
        body = self._read_json()
        username = str(body.get("username", "")).strip()[:120]
        password = str(body.get("password", ""))[:200]
        remember = body.get("remember") is True
        if not username or not password:
            return self._json({"error": "Informe usuário e senha."}, 400)
        credentials = Credentials(username, password)
        try:
            user = SGPClient(SGP_BASE_URL, credentials).user_info()
        except SGPError as exc:
            LOGIN_ATTEMPTS[remote] = attempts + [now]
            return self._json({"error": str(exc)}, exc.status)
        LOGIN_ATTEMPTS.pop(remote, None)
        session_ttl = REMEMBERED_SESSION_TTL if remember else SESSION_TTL
        session_id = _new_session(credentials, user, session_ttl)
        with SESSION_LOCK:
            user = SESSIONS[session_id]["user"]
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        cookie = f"ebf_session={session_id}; Path=/; HttpOnly; SameSite=Lax; Max-Age={session_ttl}"
        if os.getenv("EBF_SECURE_COOKIE") == "1":
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)
        payload = json.dumps(user, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authenticated(self, action: Any) -> None:
        session_id = self._session_id()
        with SESSION_LOCK:
            session = SESSIONS.get(session_id or "")
            if session and session["expires"] < time.time():
                SESSIONS.pop(session_id, None)
                session = None
        if not session:
            return self._json({"error": "Sessão expirada. Entre novamente."}, 401)
        record_user(
            str(session["user"].get("usuario", "")),
            str(session["user"].get("nome", "")),
            login=False,
        )
        client = SGPClient(SGP_BASE_URL, session["credentials"])
        try:
            result = action(client, session)
            self._json(result)
        except SGPError as exc:
            self._json({"error": str(exc)}, exc.status)

    def _authenticated_binary(self, action: Any) -> None:
        session = self._current_session()
        if not session:
            return self._json({"error": "Sessão expirada. Entre novamente."}, 401)
        try:
            content, content_type = action(SGPClient(SGP_BASE_URL, session["credentials"]))
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
        except SGPError as exc:
            self._json({"error": str(exc)}, exc.status)

    def _session_id(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get("ebf_session")
        return morsel.value if morsel else None

    def _current_session(self) -> dict[str, Any] | None:
        session_id = self._session_id()
        with SESSION_LOCK:
            session = SESSIONS.get(session_id or "")
        return session if session and session["expires"] > time.time() else None

    def _route(self, query: dict[str, list[str]]) -> None:
        try:
            from_lat = float(query["from_lat"][0]); from_lon = float(query["from_lon"][0])
            to_lat = float(query["to_lat"][0]); to_lon = float(query["to_lon"][0])
            if not all((-90 <= lat <= 90 and -180 <= lon <= 180) for lat, lon in ((from_lat, from_lon), (to_lat, to_lon))):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            return self._json({"error": "Coordenadas inválidas."}, 400)
        coordinates = f"{from_lon},{from_lat};{to_lon},{to_lat}"
        url = f"https://router.project-osrm.org/route/v1/driving/{coordinates}?" + urlencode({
            "overview": "full", "geometries": "geojson", "steps": "true"
        })
        try:
            request = Request(url, headers={"User-Agent": "EBF-Tecnico/0.1", "Accept": "application/json"})
            with urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("code") != "Ok" or not data.get("routes"):
                return self._json({"error": "Nenhuma rota viária foi encontrada."}, 404)
            route = data["routes"][0]
            return self._json({
                "geometry": route.get("geometry"), "distance": route.get("distance"),
                "duration": route.get("duration"), "legs": route.get("legs", []),
            })
        except Exception:
            return self._json({"error": "O serviço de rotas está indisponível no momento."}, 503)

    def _cached_report(self, client: SGPClient, session: dict[str, Any], period: str) -> dict[str, Any]:
        cache = session.setdefault("report_cache", {})
        cached = cache.get(period)
        if cached and cached["expires"] > time.time():
            return cached["value"]
        value = client.productivity_report(
            session["user"].get("nome", ""), session["user"].get("usuario_id"), period, repeat_refunds()
        )
        cache[period] = {"value": value, "expires": time.time() + 300}
        return value

    def _cached_map(self, client: SGPClient, session: dict[str, Any]) -> list[dict[str, Any]]:
        cached = session.get("map_cache")
        if cached and cached["expires"] > time.time():
            return cached["value"]
        value = client.map_orders(session["user"].get("nome", ""))
        session["map_cache"] = {"value": value, "expires": time.time() + 120}
        return value

    def _change_order_status(
        self, client: SGPClient, session: dict[str, Any], order_id: int, status: int, service_performed: str
    ) -> dict[str, Any]:
        result = client.update_order_status(order_id, status, service_performed)
        event(order_id, session["user"].get("nome", ""), {1: "closed", 2: "running", 3: "pending"}.get(status, "updated"), service_performed)
        session.pop("map_cache", None)
        session.pop("report_cache", None)
        session.pop("orders_cache", None)
        _clear_admin_overview_cache()
        response: dict[str, Any] = {"result": result, "next_order": None}
        if status != 1:
            return response
        try:
            open_orders = self._technician_orders(client, session, 0)
            candidates = [item for item in open_orders if int(item.get("os_id") or 0) != order_id]
            candidates.sort(key=lambda item: str(item.get("os_data_agendamento") or "9999-12-31"))
            if candidates:
                next_order = candidates[0]
                client.start_order(int(next_order["os_id"]))
                next_order["os_status"] = 2
                next_order["os_status_txt"] = "Em execução"
                response["next_order"] = next_order
        except SGPError as exc:
            response["warning"] = f"A OS foi encerrada, mas a próxima não pôde ser iniciada: {exc}"
        return response

    def _set_mode(self, session: dict[str, Any], mode: str) -> dict[str, Any]:
        if mode not in session["user"].get("modes", []):
            raise SGPError("Perfil não autorizado.", 403)
        session["user"]["mode"] = mode
        return session["user"]

    def _require_admin(self, session: dict[str, Any]) -> None:
        if session["user"].get("mode") not in ("admin", "superadmin") or "admin" not in session["user"].get("modes", []):
            raise SGPError("Acesso exclusivo para administradores.", 403)

    def _require_superadmin(self, session: dict[str, Any]) -> None:
        if session["user"].get("mode") != "superadmin" or session["user"].get("role") != "superadmin":
            raise SGPError("Acesso exclusivo para superadministradores.", 403)

    def _superadmin_users(self, session: dict[str, Any]) -> dict[str, Any]:
        self._require_superadmin(session)
        with SESSION_LOCK:
            active = {str(value["user"].get("usuario", "")).casefold() for value in SESSIONS.values() if value["expires"] > time.time()}
        popup_users = {str(item.get("username") or "").casefold() for item in popup_access_members()}
        return {"users": [{**item, "active": str(item["username"]).casefold() in active, "popup_enabled": str(item["username"]).casefold() in popup_users} for item in platform_users()]}

    def _set_user_role(self, session: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        self._require_superadmin(session)
        username = str(body.get("username", "")).strip()[:120]; role = str(body.get("role", "")).strip()
        if not username: raise SGPError("Informe o usuário.", 400)
        if username.casefold() == "gustavo" and role != "superadmin":
            raise SGPError("Gustavo é o superadmin principal e não pode perder esse acesso.", 409)
        if username.casefold() == str(session["user"].get("usuario", "")).casefold() and role != "superadmin":
            raise SGPError("Você não pode remover seu próprio acesso de superadmin.", 409)
        granted_by = str(session["user"].get("usuario", "Superadmin"))
        try:
            set_user_role(username, role, granted_by)
            if isinstance(body.get("popup_enabled"), bool):
                set_popup_access_member(username, body["popup_enabled"], granted_by)
        except ValueError: raise SGPError("Perfil inválido.", 400)
        current_token = self._session_id()
        with SESSION_LOCK:
            for token, active_session in list(SESSIONS.items()):
                active_username = str(active_session["user"].get("usuario", ""))
                if active_username.casefold() == username.casefold() and token != current_token:
                    SESSIONS.pop(token, None)
        return {"notice": f"{username} atualizado para {role}. A mudança valerá no próximo login."}

    def _superadmin_scores(self, client: SGPClient, session: dict[str, Any], technician: str = "", month: str = "") -> dict[str, Any]:
        self._require_superadmin(session)
        return client.team_productivity_report(repeat_refunds(), technician, month)

    def _refund_repeat_score(self, client: SGPClient, session: dict[str, Any], body: dict[str, Any], report_override: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_superadmin(session)
        requested = body.get("items") if isinstance(body.get("items"), list) else [body]
        if not requested:
            raise SGPError("Selecione pelo menos uma repetida.", 400)
        month = str(body.get("month") or "").strip()
        requested_technicians = {
            str(item.get("technician") or "").strip()
            for item in requested if isinstance(item, dict) and str(item.get("technician") or "").strip()
        }
        if len(requested_technicians) != 1:
            raise SGPError("Analise as repetidas de um técnico por vez.", 400)
        technician_filter = next(iter(requested_technicians))
        # Restrict the recalculation to the technician being reviewed. Loading
        # every repeated customer in the company made historical refunds hang.
        report = report_override or client.team_productivity_report(repeat_refunds(), technician_filter, month)
        validated: list[tuple[int, int, str, int, float, dict[str, Any]]] = []
        for request_item in requested:
            try:
                original_id = int(request_item.get("original_order_id"))
                repeat_id = int(request_item.get("repeat_order_id"))
            except (AttributeError, TypeError, ValueError):
                raise SGPError("Selecione uma repetida válida.", 400)
            technician = str(request_item.get("technician", "")).strip()
            raw_restored_ids = request_item.get("restored_order_ids")
            if not isinstance(raw_restored_ids, list):
                raw_restored_ids = [request_item.get("restored_order_id") or original_id]
            try:
                restored_order_ids = {int(value) for value in raw_restored_ids}
            except (TypeError, ValueError):
                raise SGPError("Selecione quais ordens devem voltar a pontuar.", 400)
            detail = next((
                repeated
                for item in report["technicians"]
                if str(item["name"]).casefold() == technician.casefold()
                for repeated in item["repeated"].get("details", [])
                if int(repeated.get("original_order_id") or 0) == original_id
                and int(repeated.get("repeat_order_id") or 0) == repeat_id
            ), None)
            if not detail:
                raise SGPError("Uma das penalidades não foi encontrada no cálculo atual.", 404)
            allowed = {original_id} if detail.get("original_suspended") else set()
            if detail.get("same_technician"):
                allowed.add(repeat_id)
            if not restored_order_ids or not restored_order_ids.issubset(allowed):
                raise SGPError("Uma das ordens escolhidas não pode receber pontos nesta repetida.", 400)
            restored_points = sum(
                float(detail.get("repeat_points") if order_id == repeat_id else detail.get("original_points") or 0)
                for order_id in restored_order_ids
            )
            restored_order_id = -1 if restored_order_ids == {original_id, repeat_id} else next(iter(restored_order_ids))
            validated.append((original_id, repeat_id, technician, restored_order_id, restored_points, detail))
        applied = 0
        for original_id, repeat_id, technician, restored_order_id, restored_points, detail in validated:
            if detail.get("refunded"):
                continue
            refund_repeat(
                original_id, repeat_id, technician, restored_points,
                str(session["user"].get("usuario", "Superadmin")), restored_order_id,
            )
            applied += 1
        with SESSION_LOCK:
            for active_session in SESSIONS.values():
                active_session.pop("report_cache", None)
        return {"notice": f"Pontuação de {applied} repetida(s) estornada." if applied else "As pontuações selecionadas já estavam estornadas."}

    def _admin_overview(self, client: SGPClient, session: dict[str, Any], period: str, force: bool = False) -> dict[str, Any]:
        self._require_admin(session)
        cache_key = period
        now = time.time()
        with ADMIN_OVERVIEW_LOCK:
            cached = ADMIN_OVERVIEW_CACHE.get(cache_key)
            if not force and cached and cached["expires"] > now:
                session.setdefault("admin_cache", {})[period] = cached
                return {**cached["value"], "cache": "fresh"}
            pending = ADMIN_OVERVIEW_INFLIGHT.get(cache_key)
            if pending is None:
                pending = threading.Event()
                ADMIN_OVERVIEW_INFLIGHT[cache_key] = pending
                owner = True
            else:
                owner = False
        if not owner:
            pending.wait(timeout=25)
            with ADMIN_OVERVIEW_LOCK:
                shared = ADMIN_OVERVIEW_CACHE.get(cache_key)
            if shared and shared.get("stale_until", 0) > time.time():
                session.setdefault("admin_cache", {})[period] = shared
                return {**shared["value"], "cache": "fresh" if shared["expires"] > time.time() else "stale"}
            raise SGPError("A atualização do painel ainda está em andamento. Tente novamente em alguns segundos.", 503)
        try:
            orders, closed_orders = client.admin_orders(period)
        except SGPError:
            if cached and cached.get("stale_until", 0) > now:
                return {**cached["value"], "cache": "stale"}
            raise
        finally:
            if "orders" not in locals():
                with ADMIN_OVERVIEW_LOCK:
                    ADMIN_OVERVIEW_INFLIGHT.pop(cache_key, None)
                    pending.set()
        sync_orders(orders + closed_orders)
        with KNOWN_ADMIN_ORDERS_LOCK:
            for item in orders + closed_orders:
                order_id = int(item.get("os_id") or 0)
                if order_id and item.get("latitude") is not None and item.get("longitude") is not None:
                    KNOWN_ADMIN_ORDERS[order_id] = {**item, "_cached_at": now}
            cutoff = now - 30 * 24 * 60 * 60
            for order_id, item in list(KNOWN_ADMIN_ORDERS.items()):
                if float(item.get("_cached_at") or 0) < cutoff:
                    KNOWN_ADMIN_ORDERS.pop(order_id, None)
        operations = snapshot()
        technicians = sorted({str(item.get("os_tecnico_responsavel") or "Sem técnico") for item in orders + closed_orders})
        value = {"period": period, "orders": orders, "closed_orders": closed_orders, "technicians": technicians, **operations}
        cache_entry = {"value": value, "expires": now + 180, "stale_until": now + 900}
        session.setdefault("admin_cache", {})[period] = cache_entry
        with ADMIN_OVERVIEW_LOCK:
            ADMIN_OVERVIEW_CACHE[cache_key] = cache_entry
            ADMIN_OVERVIEW_INFLIGHT.pop(cache_key, None)
            pending.set()
        return value

    def _admin_activity(self, client: SGPClient, session: dict[str, Any], selected: date, period: str) -> dict[str, Any]:
        self._require_admin(session)
        start = selected if period == "day" else selected - timedelta(days=selected.weekday())
        end = start if period == "day" else start + timedelta(days=6)
        if start <= date.today() <= end: sync_orders(client.admin_statuses())
        imported = client.completed_timeline(start.isoformat(), min(end, date.today()).isoformat()) if start <= date.today() else []
        scheduled_orders = client.scheduled_timeline(start.isoformat(), end.isoformat())
        save_completed_orders(imported)
        events = snapshot(start.isoformat()) if period == "day" else snapshot()
        if period == "week":
            start_ts = datetime.combine(start, datetime.min.time()).timestamp(); end_ts = datetime.combine(end + timedelta(days=1), datetime.min.time()).timestamp()
            events["events"] = [item for item in events["events"] if start_ts <= item["created_at"] < end_ts]
        return {"date": selected.isoformat(), "period": period, "start": start.isoformat(), "end": end.isoformat(), "completed_orders": completed_between(start.isoformat(), end.isoformat()), "scheduled_orders": scheduled_orders, **events}

    def _admin_address_search(self, client: SGPClient, session: dict[str, Any], cep: str, number: str) -> dict[str, Any]:
        self._require_admin(session)
        digits = "".join(character for character in str(cep) if character.isdigit())
        number = str(number).strip()[:80]
        if len(digits) != 8 or not number:
            raise SGPError("Informe um CEP válido e o número do imóvel.", 400)
        try:
            with urlopen(Request(f"https://viacep.com.br/ws/{digits}/json/", headers={"User-Agent": "EBF-Tecnico/0.1"}), timeout=8) as response:
                address = json.loads(response.read().decode("utf-8"))
            if address.get("erro"):
                raise SGPError("CEP não encontrado.", 404)
            description = f"{address.get('logradouro')}, {number}, {address.get('localidade')}, {address.get('uf')}, Brasil"
            geocode_url = "https://nominatim.openstreetmap.org/search?" + urlencode({
                "format": "jsonv2", "limit": 1, "countrycodes": "br", "q": description,
            })
            with urlopen(Request(geocode_url, headers={"User-Agent": "EBF-Tecnico/0.1 (operacao@ebf.local)"}), timeout=10) as response:
                places = json.loads(response.read().decode("utf-8"))
            if not places:
                raise SGPError("Não foi possível localizar esse número no mapa.", 404)
        except SGPError:
            raise
        except Exception as exc:
            raise SGPError("O serviço de consulta de endereço está indisponível.", 503) from exc
        latitude, longitude = float(places[0]["lat"]), float(places[0]["lon"])
        known: dict[int, dict[str, Any]] = {}
        for cached in session.get("admin_cache", {}).values():
            for order in cached.get("value", {}).get("orders", []) + cached.get("value", {}).get("closed_orders", []):
                order_id = int(order.get("os_id") or 0)
                if order_id:
                    known[order_id] = order
        with KNOWN_ADMIN_ORDERS_LOCK:
            for order_id, order in KNOWN_ADMIN_ORDERS.items():
                known.setdefault(order_id, order)
        open_orders, closed_orders = list(known.values()), []
        nearby = []
        same_street = []
        target_street = unicodedata.normalize("NFKD", str(address.get("logradouro") or "")).encode("ascii", "ignore").decode().casefold()
        for order in open_orders + closed_orders:
            order_street = unicodedata.normalize("NFKD", str(order.get("endereco_logradouro") or "")).encode("ascii", "ignore").decode().casefold()
            if target_street and order_street == target_street:
                same_street.append(order)
            if order.get("latitude") is None or order.get("longitude") is None:
                continue
            lat2, lon2 = math.radians(float(order["latitude"])), math.radians(float(order["longitude"]))
            lat1, lon1 = math.radians(latitude), math.radians(longitude)
            value = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
            item = {**order, "distance": 6371*2*math.atan2(math.sqrt(value), math.sqrt(max(0, 1-value)))}
            nearby.append(item)
        nearby.sort(key=lambda item: item["distance"])
        confirmed = covered_street(digits)
        return {
            "address": description,
            "cep": digits,
            "street": address.get("logradouro"),
            "city": address.get("localidade"),
            "state": address.get("uf"),
            "latitude": latitude,
            "longitude": longitude,
            "nearby": nearby[:12],
            "same_street": same_street[:20],
            "coverage": {"covered": bool(confirmed or same_street), "confirmed": bool(confirmed), "evidence": len(same_street)},
            "known_orders": len(known),
            "source": "cache",
        }

    def _confirm_address_coverage(self, session: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(session)
        return confirm_covered_street(
            str(body.get("cep", "")), str(body.get("street", "")),
            str(body.get("city", "")), str(body.get("state", "")),
            str(session["user"].get("usuario") or session["user"].get("nome") or "Admin"),
        )

    def _admin_stock_locations(self, client: SGPClient, session: dict[str, Any]) -> dict[str, Any]:
        self._require_superadmin(session)
        excluded = {"empresa", "almoxerifado"}
        all_locations = client.stock_locations()
        company = self._company_stock(all_locations)
        company_id = int(company.get("id") or 0) if company else 0
        locations = [
            item for item in all_locations
            if int(item.get("id") or 0) != company_id
            and str(item.get("descricao", "")).casefold() not in excluded
        ]
        return {
            "locations": sorted(locations, key=lambda item: str(item.get("descricao", "")).casefold()),
            "company_stock": company,
        }

    @staticmethod
    def _company_stock(locations: list[dict[str, Any]]) -> dict[str, Any] | None:
        def normalized(value: Any) -> str:
            return " ".join(unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold().split())
        exact = next((item for item in locations if normalized(item.get("descricao")) == "empresa"), None)
        return exact or next((item for item in locations if "empresa" in normalized(item.get("descricao"))), None)

    def _admin_stock(self, client: SGPClient, session: dict[str, Any], location_id: int) -> dict[str, Any]:
        self._require_superadmin(session)
        stock = client.stock_for_location(location_id)
        usage = client.daily_usage_for_location(location_id)
        rules = [
            ("Modem giga", 3, ("modem giga",)), ("Wi-Fi 6", 2, ("wifi 6", "wi-fi 6")),
            ("Modem básico", 1, ("modem basico", "zte basica")),
            ("Conector verde", 10, ("conector apc", "conector verde")),
            ("Conector azul", 10, ("conector upc", "conector azul")),
            ("Isolante", 1, ("isolante",)), ("Cunha", 50, ("cunha",)), ("Drop", 200, ("drop",)),
        ]
        def normalized(value: Any) -> str:
            return " ".join(unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold().split())
        replenishment = []
        for label, target, terms in rules:
            current = sum(float(item.get("quantity") or 0) for item in stock["items"] if any(term in normalized(item.get("description")) for term in terms))
            missing = max(0, target - current)
            if missing > 0: replenishment.append({"name": label, "target": target, "current": current, "missing": missing})
        return {**stock, "usage_today": usage, "replenishment": replenishment, "standards": [{"name": label, "target": target} for label, target, _terms in rules]}

    def _admin_transfer_options(self, client: SGPClient, session: dict[str, Any], origin_id: int, destination_id: int) -> dict[str, Any]:
        self._require_superadmin(session)
        if origin_id == destination_id:
            raise SGPError("O estoque de origem deve ser diferente do destino.", 400)
        locations = client.stock_locations()
        origin = next((item for item in locations if int(item.get("id") or 0) == origin_id), None)
        destination = next((item for item in locations if int(item.get("id") or 0) == destination_id), None)
        if not origin or not destination:
            raise SGPError("Estoque de origem ou destino não localizado.", 404)
        source = client.stock_for_location(origin_id, origin, prefer_admin=ADMIN_ENDPOINTS_ONLY)
        target = self._admin_stock(client, session, destination_id)
        missing = target["replenishment"]
        rule_terms = {
            "Modem giga": ("modem giga",), "Wi-Fi 6": ("wifi 6", "wi-fi 6"),
            "Modem básico": ("modem basico", "zte basica"),
            "Conector verde": ("conector apc", "conector verde"),
            "Conector azul": ("conector upc", "conector azul"),
            "Isolante": ("isolante",), "Cunha": ("cunha",), "Drop": ("drop",),
        }
        def normalized(value: Any) -> str:
            return " ".join(unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold().split())
        needed_items = []
        for item in source["items"]:
            description = normalized(item.get("description"))
            need = next((entry for entry in missing if any(term in description for term in rule_terms.get(entry["name"], ()))), None)
            if need:
                needed_items.append({**item, "replenishment_name": need["name"], "recommended_quantity": need["missing"]})
        return {
            "origin": source["location"], "destination": destination,
            "items": needed_items, "replenishment": missing,
            "endpoint_test_mode": ADMIN_ENDPOINTS_ONLY,
        }

    def _admin_transfer(self, client: SGPClient, session: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        self._require_superadmin(session)
        try:
            origin_id, destination_id = int(body.get("origin")), int(body.get("destination"))
        except (TypeError, ValueError):
            raise SGPError("Selecione os estoques de origem e destino.", 400)
        company = self._company_stock(client.stock_locations())
        if not company or int(company.get("id") or 0) != origin_id:
            raise SGPError("A reposição deve sair obrigatoriamente do estoque Empresa.", 400)
        items = body.get("items") if isinstance(body.get("items"), list) else []
        options = self._admin_transfer_options(client, session, origin_id, destination_id)
        allowed = {int(item["product_id"]): item for item in options["items"]}
        totals: dict[str, int] = {}
        for item in items:
            try:
                product_id, quantity = int(item.get("product_id")), int(item.get("quantity"))
            except (TypeError, ValueError):
                raise SGPError("Produto ou quantidade inválida na reposição.", 400)
            source_item = allowed.get(product_id)
            if not source_item:
                raise SGPError("Só é permitido enviar materiais indicados para reposição.", 400)
            group = str(source_item["replenishment_name"])
            totals[group] = totals.get(group, 0) + quantity
            if totals[group] > int(float(source_item["recommended_quantity"])):
                raise SGPError(f"A quantidade de {group} ultrapassa a reposição necessária.", 400)
        if ADMIN_ENDPOINTS_ONLY:
            probe = client.probe_admin_transfer_form(origin_id, destination_id)
            if not probe.get("ready"):
                raise SGPError(
                    "O formulário administrativo abriu, mas os campos de transferência não foram reconhecidos. Nenhum material foi movimentado.",
                    422,
                )
            return {
                "ok": True, "test_only": True, "probe": probe,
                "notice": "Teste concluído: formulário administrativo validado. Nenhum material foi movimentado.",
            }
        result = client.transfer_stock(
            origin_id, destination_id, session["user"].get("usuario_id"), items,
            str(body.get("observation") or ""),
        )
        session.pop("admin_cache", None)
        _clear_admin_overview_cache()
        return {"ok": True, "notice": "Materiais transferidos para o técnico com sucesso.", "result": result}

    def _assign_order(self, session: dict[str, Any], order_id: int, body: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(session)
        technician = str(body.get("technician", "")).strip()[:160]
        if not technician: raise SGPError("Selecione um técnico.", 400)
        assign(order_id, technician, session["user"].get("nome", "Administrador"))
        _clear_route_caches()
        session.pop("admin_cache", None)
        _clear_admin_overview_cache()
        return {"ok": True, "notice": "Direcionamento registrado no EBF. A sincronização do responsável no SGP depende da liberação desse endpoint pela API."}

    def _admin_edit_order(self, client: SGPClient, session: dict[str, Any], order_id: int, body: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(session)
        technician = str(body.get("technician", "")).strip()[:160]
        note = str(body.get("note", "")).strip()[:3000]
        content = str(body.get("content", "")).strip()[:5000]
        if not technician and not note and not content:
            raise SGPError("Escolha um responsável ou escreva uma anotação.", 400)
        if technician:
            assign(order_id, technician, session["user"].get("nome", "Administrador"))
            _clear_route_caches()
        if note:
            client.add_order_note(order_id, f"[Gestão EBF] {note}")
        if content:
            client.update_order_content(order_id, content)
        session.pop("admin_cache", None)
        _clear_admin_overview_cache()
        return {"ok": True, "notice": "Informações administrativas salvas na OS."}

    def _save_location(self, session: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        try:
            latitude, longitude = float(body["latitude"]), float(body["longitude"])
            accuracy = float(body.get("accuracy") or 0)
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180): raise ValueError
        except (KeyError, TypeError, ValueError):
            raise SGPError("Localização inválida.", 400)
        save_location(session["user"].get("nome", ""), latitude, longitude, accuracy)
        return {"ok": True}

    def _technician_orders(self, client: SGPClient, session: dict[str, Any], status: int | None = None) -> list[dict[str, Any]]:
        name = session["user"].get("nome", "")
        base = client.orders() if status is None else client.orders_by_status(name, status)
        known = {int(item.get("os_id") or 0) for item in base}
        for order_id in assignments_for(name):
            if order_id in known: continue
            order = client.order(order_id, include_onu=False)
            scheduled_today = _scheduled_on((order or {}).get("os_data_agendamento"), _business_today())
            if order and (status == 1 or scheduled_today) and (status is None or int(order.get("os_status") or 0) == status):
                base.append(order)
        return base

    def _cached_technician_orders(
        self, client: SGPClient, session: dict[str, Any], status: int | None = None
    ) -> list[dict[str, Any]]:
        key = "route" if status is None else str(status)
        cache = session.setdefault("orders_cache", {})
        cached = cache.get(key)
        now = time.time()
        if cached and cached["expires"] > now:
            return cached["value"]
        value = self._technician_orders(client, session, status)
        cache[key] = {"value": value, "expires": now + 15}
        return value

    def _read_json(self, max_bytes: int = 16_384) -> dict[str, Any]:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), max_bytes)
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _json(self, value: Any, status: int = 200) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC.resolve() not in target.parents and target != STATIC.resolve():
            return self.send_error(HTTPStatus.FORBIDDEN)
        if not target.is_file():
            target = STATIC / "index.html"
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)


class EBFHTTPServer(HTTPServer):
    allow_reuse_address = True
    request_queue_size = max(128, int(os.getenv("EBF_REQUEST_QUEUE", "256")))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=max(8, int(os.getenv("EBF_HTTP_WORKERS", "48"))),
            thread_name_prefix="ebf-http",
        )
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        self.executor.submit(self._process_request, request, client_address)

    def _process_request(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self) -> None:
        super().server_close()
        self.executor.shutdown(wait=True, cancel_futures=True)


def main() -> None:
    host = os.getenv("EBF_HOST", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("EBF_PORT", "8080"))
    print(f"EBF Técnico disponível em http://{host}:{port}")
    EBFHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
