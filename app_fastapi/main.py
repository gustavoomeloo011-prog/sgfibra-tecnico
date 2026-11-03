from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import OperationalError, InterfaceError
from psycopg_pool import PoolTimeout

from app_fastapi import postgres_operations

postgres_operations.configure_operations()

from app.operations import (
    add_manual_removal_payments,
    cache_snapshot,
    delete_cache_snapshots_prefix,
    delete_cache_snapshots_except,
    confirmed_removals,
    connector_returns,
    delete_expired_connector_returns,
    delete_platform_user,
    list_tutorials,
    save_tutorial,
    delete_tutorial,
    delivered_removals,
    delete_expired_removals,
    delete_return_obligation,
    dismiss_connector_returns,
    dismiss_confirmed_removals,
    dismiss_installation_material_alerts,
    exclude_removal_payments,
    equipment_reference,
    expired_removals,
    expired_connector_returns,
    installation_material_alerts,
    cto_query_history,
    coverage_buildings,
    save_coverage_building,
    delete_coverage_building,
    coverage_pon_areas,
    save_coverage_pon_area,
    delete_coverage_pon_area,
    platform_users,
    popup_access_members,
    record_user,
    record_cto_query,
    return_obligations,
    route_alert_reviews,
    route_monitor_members,
    route_service_times,
    role_for,
    save_equipment_reference,
    save_confirmed_removals,
    save_connector_returns,
    set_sheet_connector_total,
    replace_installation_material_alerts,
    save_cache_snapshot,
    stock_balance_snapshot_before,
    save_stock_balance_snapshot,
    save_return_obligation,
    save_route_alert_review,
    save_route_service_times,
    set_user_role,
    set_route_monitor_member,
    set_popup_access_member,
    update_connector_return,
    upsert_sheet_connector,
    upsert_sheet_removal,
)
from app.server import (
    Handler,
    REMEMBERED_SESSION_TTL,
    SESSION_TTL,
    SGP_BASE_URL,
)
from app.sgp import (
    Credentials, SGPClient, SGPError, _business_today, _is_modem_product, _operational_week, _same_person,
    _evaluate_material_rules, _material_rule_service_text, connector_classification, _coordinates,
    reconcile_stock_day, stock_day_with_calculated_opening,
    build_route_monitor,
)
from app_fastapi.state import store
from app_fastapi import logistics, material_ai


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TUTORIAL_UPLOADS = ROOT / "uploads" / "tutorials"
TUTORIAL_UPLOADS.mkdir(parents=True, exist_ok=True)
services = object.__new__(Handler)
business_limit = asyncio.Semaphore(min(16, max(4, int(os.getenv("V2_BUSINESS_CONCURRENCY", "8")))))
SYSTEM_DISABLED = os.getenv("SYSTEM_DISABLED", "0") != "0"
VOIP_ENABLED = os.getenv("ENABLE_VOIP", "1") == "1"
SHEETS_SYNC_SECRET = os.getenv("SHEETS_SYNC_SECRET", "").strip()
SHEETS_WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "").strip()


async def run_sync(function: Callable[[], Any]) -> Any:
    async with business_limit:
        return await asyncio.to_thread(function)


LOGIN_WINDOW_SECONDS = 600
LOGIN_ACCOUNT_LIMIT = 5
LOGIN_IP_LIMIT = 20


def _request_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _login_limit_keys(request: Request, username: str) -> tuple[str, str]:
    remote_hash = hashlib.sha256(_request_ip(request).encode()).hexdigest()[:24]
    account_hash = hashlib.sha256(str(username).strip().casefold().encode()).hexdigest()[:24]
    return f"security:login:ip:{remote_hash}", f"security:login:account:{account_hash}"


async def _login_attempts(key: str) -> int:
    value = await store.get(key)
    return int(value.get("count") or 0) if isinstance(value, dict) else 0


async def _record_login_failure(keys: tuple[str, str]) -> None:
    for key in keys:
        lock_name = f"login-counter:{key}"
        for _ in range(20):
            if await store.acquire(lock_name, 5):
                try:
                    count = await _login_attempts(key)
                    await store.set(key, {"count": count + 1}, LOGIN_WINDOW_SECONDS)
                finally:
                    await store.release(lock_name)
                break
            await asyncio.sleep(.01)


async def _clear_login_failures(keys: tuple[str, str]) -> None:
    for key in keys:
        await store.delete(key)


async def _authenticate_sgp(credentials: Credentials) -> dict[str, Any]:
    """Retry only transient, read-only authentication failures."""
    last_error: SGPError | None = None
    for attempt in range(3):
        try:
            return await run_sync(lambda: SGPClient(SGP_BASE_URL, credentials).user_info())
        except SGPError as exc:
            if exc.status < 500:
                raise
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(.35 * (attempt + 1))
    raise last_error or SGPError("Não foi possível consultar o SGP agora.", 503)


async def _role_for_login(username: str) -> str:
    """Retry the authorization lookup once when PostgreSQL briefly reconnects."""
    try:
        return await run_sync(lambda: role_for(username))
    except Exception:
        await asyncio.sleep(.15)
        try:
            return await run_sync(lambda: role_for(username))
        except Exception as exc:
            raise SGPError("O banco de dados está reconectando. Tente entrar novamente em alguns segundos.", 503) from exc


def send_sheet_sync(action: str, items: list[dict[str, Any]]) -> bool:
    if not SHEETS_SYNC_SECRET or not SHEETS_WEBHOOK_URL or not items:
        return False
    payload = json.dumps({"secret": SHEETS_SYNC_SECRET, "action": action, "items": items}, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(SHEETS_WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json", "User-Agent": "EBF-Tecnico/1.0"})
    # O Apps Script pode levar mais tempo ao atualizar muitas linhas e aplicar
    # validações. Não use o limite curto das chamadas comuns ao SGP aqui.
    with urllib.request.urlopen(request, timeout=120) as response:
        return 200 <= int(response.status) < 300


def removal_sheet_row(item: dict[str, Any], delivered: bool = False) -> dict[str, Any]:
    evidence = str(item.get("evidence") or "")
    original = re.search(r"Sincronizado da planilha:\s*([^|\s]+)", evidence, re.I)
    sync_id = original.group(1) if original else f"os-{item.get('os_id')}"
    return {
        "sync_id": sync_id, "date": str(item.get("evidence_at") or item.get("closed_at") or "")[:10],
        "technician": item.get("technician"), "serial": item.get("serial"), "cpf": item.get("cpf"),
        "return_type": item.get("return_type") or "retirada", "client": item.get("client"), "delivered": delivered,
    }


def connector_sheet_row(item: dict[str, Any], delivered: bool = False) -> dict[str, Any]:
    return {
        "entity": "connector", "sync_id": f"connector-{item.get('source_id')}",
        "date": str(item.get("used_at") or "")[:10], "technician": item.get("technician"),
        "client": item.get("client"), "product": item.get("product"),
        "quantity": float(item.get("quantity") or 0), "os_id": item.get("os_id"),
        "reason": item.get("reason"), "note": item.get("note"), "delivered": delivered,
    }


async def promote_manual_modems(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Move manual modem notes into the canonical pending-return ledger."""
    pending = await run_sync(confirmed_removals)
    existing_serials = {
        re.sub(r"[^A-Z0-9]", "", str(item.get("serial") or "").upper())
        for item in pending
    }
    promoted: list[dict[str, Any]] = []
    today = _business_today().isoformat()
    for obligation in items:
        technician = str(obligation.get("technician") or "").strip()
        for modem in obligation.get("modems") or []:
            source = modem if isinstance(modem, dict) else {"gpon": modem, "type": "troca"}
            serial = re.sub(r"[^A-Z0-9]", "", str(source.get("gpon") or "").upper())
            if not technician or not serial or serial in existing_serials:
                continue
            return_type = str(source.get("type") or "troca").casefold()
            sync_key = hashlib.sha256(f"{technician.casefold()}:{serial}".encode("utf-8")).hexdigest()[:20]
            created = await run_sync(lambda value={
                "sync_id": f"manual-{sync_key}", "date": today, "technician": technician,
                "serial": serial, "cpf": "", "return_type": return_type,
                "client": "Lançamento manual do supervisor", "delivered": False,
            }: upsert_sheet_removal(value))
            existing_serials.add(serial)
            promoted.append({
                "os_id": created["os_id"], "technician": technician,
                "client": "Lançamento manual do supervisor", "cpf": "", "serial": serial,
                "return_type": return_type, "closed_at": today, "evidence_at": today,
                "evidence": f"Sincronizado da planilha: manual-{sync_key}",
            })
    sheet_synced = True
    if promoted:
        sheet_synced = await run_sync(lambda: send_sheet_sync(
            "upsert_many", [removal_sheet_row(item) for item in promoted],
        ))
    return promoted, sheet_synced


def connector_summary_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        name = str(item.get("technician") or "Não identificado").strip()
        key = re.sub(r"\W+", "", name.casefold()) or "naoidentificado"
        if key not in grouped:
            grouped[key] = {"entity": "connector_summary", "sync_id": f"connector-summary-{key}", "technician": name, "quantity": 0.0}
        grouped[key]["quantity"] += float(item.get("quantity") or 0)
    return [{**item, "quantity": round(float(item["quantity"]), 2)} for item in grouped.values()]


def send_connector_summary() -> bool:
    return send_sheet_sync("replace_connector_summary", connector_summary_rows(connector_returns()))


def cache_identity(session: dict[str, Any]) -> str:
    user = session.get("user", {})
    raw = f"{user.get('usuario')}:{user.get('mode')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def central_sgp_client() -> SGPClient | None:
    username = os.getenv("SGP_TRAFFIC_USERNAME", "").strip()
    password = os.getenv("SGP_TRAFFIC_PASSWORD", "")
    return SGPClient(SGP_BASE_URL, Credentials(username, password)) if username and password else None


async def cached(
    session: dict[str, Any],
    name: str,
    ttl: int,
    function: Callable[[], Any],
    *,
    force: bool = False,
    stale_ttl: int | None = None,
    persistent_ttl: int = 14 * 86400,
    allow_stale_on_error: bool = True,
    shared: bool = False,
) -> Any:
    """Cache only user-requested queries. Nothing refreshes in the background."""
    if name.startswith(("stock", "admin-stock", "transfer-options", "material-usage", "material-monitor", "material-analysis", "launched-modem", "weekly-summary", "open-service-map", "route-monitor", "route-itinerary", "order-detail", "orders-today")):
        name = f"{name}:logistics-{await run_sync(logistics.revision)}"
        allow_stale_on_error = False
    # Some expensive operational maps are deliberately shared.  They are
    # still protected by the route permission, but one supervisor refresh is
    # enough for every permitted office user to view the same saved snapshot.
    identity = "shared" if shared else cache_identity(session)
    key = f"cache:data:{identity}:{name}"
    persistent_key = f"{identity}:{name}"
    stale_key = f"{key}:stale"
    stale = await store.get(stale_key)
    if not force:
        value = await store.get(key)
        if value is not None:
            return value
        try:
            value = await run_sync(lambda: cache_snapshot(persistent_key, ttl, persistent_ttl))
        except Exception:
            value = None
        if value is not None:
            await store.set(key, value, ttl)
            return value
    if stale is None:
        try:
            stale = await run_sync(lambda: cache_snapshot(persistent_key, None, persistent_ttl))
        except Exception:
            stale = None
    # A score query may take tens of seconds, but its distributed lock must
    # never survive for the full cache TTL after a cancelled/dead worker.
    lock_ttl = min(120, max(30, ttl))
    owner = await store.acquire(f"refresh:{identity}:{name}", lock_ttl)
    if not owner:
        for _ in range(600):
            await asyncio.sleep(.1)
            value = await store.get(key)
            if value is not None:
                return value
        if stale is not None and allow_stale_on_error:
            return stale
        raise SGPError("A consulta já está em andamento. Aguarde alguns segundos.", 503)
    try:
        try:
            value = await run_sync(function)
        except Exception:
            if stale is not None and allow_stale_on_error:
                return stale
            raise
        await store.set(key, value, ttl)
        await store.set(stale_key, value, stale_ttl or max(600, ttl * 5))
        try:
            await run_sync(lambda: save_cache_snapshot(persistent_key, value, persistent_ttl))
        except Exception:
            pass
        return value
    finally:
        await store.release(f"refresh:{identity}:{name}")


async def invalidate_cached(session: dict[str, Any], name_prefix: str) -> None:
    """Remove hot and persistent copies after a business mutation."""
    identity = cache_identity(session)
    try:
        await store.delete_prefix(f"cache:data:{identity}:{name_prefix}")
        await run_sync(lambda: delete_cache_snapshots_prefix(f"{identity}:{name_prefix}"))
    except Exception:
        pass


def logistics_actor(session):
    user = session.get("user", {})
    if user.get("mode") not in ("technician", "superadmin"):
        raise HTTPException(403, "Seu perfil não possui acesso à logística.")
    actor = str(user.get("usuario") or "").strip()
    if not actor:
        raise HTTPException(401, "Entre novamente para identificar seu usuário.")
    return actor


async def stock_mutation_finished():
    # A cache failure must not turn a completed SGP operation into a retryable 500.
    try:
        await run_sync(lambda: logistics.revision(True))
    except Exception:
        logging.getLogger(__name__).exception("Failed to invalidate logistics queries after stock mutation")


async def logistics_result(source, actor, detail):
    try:
        await run_sync(lambda: logistics.audit(source, actor, detail))
    except Exception:
        logging.getLogger(__name__).exception("Failed to persist logistics operation outcome")




def serializable_session(session: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in session.items() if key != "credentials"}


async def session_context(request: Request) -> tuple[str, dict[str, Any], SGPClient]:
    session_id = request.cookies.get("ebf_session", "")
    session = await store.get_session(session_id)
    if not session:
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    user = session.get("user", {})
    if user.get("mode") not in ("technician", "office", "superadmin"):
        raise HTTPException(403, "Perfil sem acesso ao EBF Técnico.")
    current_role = await run_sync(lambda: role_for(str(user.get("usuario") or "")))
    if not current_role or current_role == "disabled":
        await store.delete_session(session_id)
        raise HTTPException(403, "Seu acesso ao EBF Técnico foi desativado.")
    credentials = Credentials(
        str(session.pop("credential_username", "")),
        str(session.pop("credential_password", "")),
    )
    if not credentials.username or not credentials.password:
        raise HTTPException(401, "Sessão inválida. Entre novamente.")
    session["credentials"] = credentials
    return session_id, session, SGPClient(SGP_BASE_URL, credentials)


async def save_context(session_id: str, session: dict[str, Any]) -> None:
    credentials = session.get("credentials")
    value = serializable_session(session)
    if isinstance(credentials, Credentials):
        value["credential_username"] = credentials.username
        value["credential_password"] = credentials.password
    await store.save_session(session_id, value)


async def call_context(
    context: tuple[str, dict[str, Any], SGPClient],
    function: Callable[[SGPClient, dict[str, Any]], Any],
) -> Any:
    session_id, session, client = context
    try:
        return await run_sync(lambda: function(client, session))
    finally:
        # Persisting a refreshed session is useful, but a transient database or
        # Valkey error here must not replace a successful SGP response with 500.
        try:
            await save_context(session_id, session)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await store.connect()
    keepalive_task = asyncio.create_task(render_keepalive()) if keepalive_url() else None
    sheet_cleanup_task = asyncio.create_task(sheet_retention_cleanup())
    try:
        yield
    finally:
        if keepalive_task:
            keepalive_task.cancel()
            await asyncio.gather(keepalive_task, return_exceptions=True)
        sheet_cleanup_task.cancel()
        await asyncio.gather(sheet_cleanup_task, return_exceptions=True)
        await store.close()
        postgres_operations.close_operations()


def keepalive_url() -> str:
    if os.getenv("RENDER_KEEPALIVE", "1") == "0":
        return ""
    base = (os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    return f"{base}/healthz" if base.startswith(("https://", "http://")) else ""


async def render_keepalive() -> None:
    """Keep an opted-in Render web service warm without touching SGP or the DB."""
    url = keepalive_url()
    while url:
        await asyncio.sleep(600)
        try:
            await asyncio.to_thread(
                lambda: urllib.request.urlopen(  # noqa: S310 - URL is deployment configuration
                    urllib.request.Request(url, headers={"User-Agent": "EBF-Render-Keepalive/1"}), timeout=10,
                ).close()
            )
        except (OSError, TimeoutError, ValueError):
            pass


async def sheet_retention_cleanup() -> None:
    """Delete 14-day-old removals from Sheets and the DB without touching SGP."""
    while True:
        try:
            expired = await run_sync(expired_removals)
            if expired:
                rows = [removal_sheet_row(item, bool(item.get("dismissed_at"))) for item in expired]
                synced = await run_sync(lambda: send_sheet_sync("delete_many", rows))
                if synced:
                    ids = [int(item.get("os_id") or 0) for item in expired]
                    await run_sync(lambda: delete_expired_removals(ids))
            expired_connectors = await run_sync(expired_connector_returns)
            if expired_connectors:
                rows = [connector_sheet_row(item, bool(item.get("dismissed_at"))) for item in expired_connectors]
                synced = await run_sync(lambda: send_sheet_sync("delete_many", rows))
                if synced:
                    ids = [str(item.get("source_id") or "") for item in expired_connectors]
                    await run_sync(lambda: delete_expired_connector_returns(ids))
        except Exception:  # keep cleanup alive across temporary DB/Google failures
            pass
        await asyncio.sleep(3600)


app = FastAPI(title="EBF Técnico Essencial", version="3.0.0", lifespan=lifespan)


@app.middleware("http")
async def api_authentication_and_security_headers(request: Request, call_next):
    path = request.url.path
    public_api = path in ("/api/login", "/api/integrations/google-sheets") or (path == "/api/maintenance" and request.method == "GET")
    if path.startswith("/api/") and not public_api and request.method != "OPTIONS":
        session_id = request.cookies.get("ebf_session", "")
        try:
            authenticated = session_id and await store.get_session(session_id)
        except (OperationalError, InterfaceError, PoolTimeout):
            return JSONResponse({"error": "Banco temporariamente indisponível. Seu acesso não foi removido; tente novamente em instantes."}, status_code=503,
                                headers={"Cache-Control": "no-store", "Retry-After": "5"})
        if not authenticated:
            return JSONResponse(
                {"error": "Sessão expirada. Entre novamente."}, status_code=401,
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )
    if path.startswith("/api/") and path != "/api/integrations/google-sheets" and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = str(request.headers.get("origin") or "").rstrip("/")
        expected = str(request.base_url).rstrip("/")
        forwarded_host = str(request.headers.get("x-forwarded-host") or "").strip()
        forwarded_proto = str(request.headers.get("x-forwarded-proto") or "https").split(",", 1)[0].strip()
        allowed = {expected}
        if forwarded_host:
            allowed.add(f"{forwarded_proto}://{forwarded_host}".rstrip("/"))
        if origin and origin not in allowed:
            return JSONResponse({"error": "Origem da requisição não autorizada."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(self), geolocation=(self), microphone=()"
    if path.startswith("/api/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def maintenance_mode(request: Request, call_next):
    if SYSTEM_DISABLED and request.url.path != "/healthz":
        return JSONResponse(
            {
                "error": "EBF Técnico temporariamente desativado para diagnóstico.",
                "maintenance": True,
            },
            status_code=503,
            headers={"Cache-Control": "no-store", "Retry-After": "3600"},
        )
    return await call_next(request)


from app_fastapi.maintenance import install as install_maintenance
install_maintenance(app)


@app.exception_handler(SGPError)
async def sgp_error_handler(_request: Request, exc: SGPError):
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


@app.exception_handler(HTTPException)
async def http_error_handler(_request: Request, exc: HTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
@app.exception_handler(PoolTimeout)
async def database_unavailable_handler(_request: Request, exc):
    logging.getLogger(__name__).warning("Database temporarily unavailable: %s", type(exc).__name__)
    return JSONResponse({"error": "Banco temporariamente indisponível. Seus acessos estão preservados; tente novamente em instantes."},
                        status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "5"})


@app.get("/healthz")
async def healthz():
    return {
        "ok": not SYSTEM_DISABLED,
        "maintenance": SYSTEM_DISABLED,
        "service": "ebf-tecnico-essential",
        "background_sync": False,
        "render_keepalive": bool(keepalive_url()),
        "database": "postgresql" if postgres_operations.POSTGRES_ACTIVE else "sqlite-fallback",
        "valkey": bool(store.redis),
    }


@app.post("/api/integrations/google-sheets")
async def google_sheets_webhook(request: Request):
    if not SHEETS_SYNC_SECRET:
        raise HTTPException(503, "A integração com a planilha ainda não foi configurada.")
    body = await request.json()
    supplied = str(request.headers.get("X-EBF-Sync-Secret") or body.get("secret") or "")
    if not hmac.compare_digest(supplied, SHEETS_SYNC_SECRET):
        raise HTTPException(403, "Chave da integração inválida.")
    if body.get("action") == "health":
        return {"ok": True, "service": "google-sheets-sync"}
    raw_items = body.get("items") if isinstance(body.get("items"), list) else [body.get("item") or body]
    if not 1 <= len(raw_items) <= 200 or any(not isinstance(item, dict) for item in raw_items):
        raise HTTPException(400, "Envie de 1 a 200 registros válidos.")
    results = []
    try:
        for item in raw_items:
            entity = str(item.get("entity") or "").casefold()
            if entity == "connector_summary":
                results.append(await run_sync(lambda value=item: set_sheet_connector_total(value.get("technician"), value.get("quantity"))))
            else:
                operation = upsert_sheet_connector if entity == "connector" else upsert_sheet_removal
                results.append(await run_sync(lambda value=item, function=operation: function(value)))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "items": results}


@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    username = str(body.get("username", "")).strip()[:120]
    password = str(body.get("password", ""))[:200]
    remember = body.get("remember") is True
    now = time.time()
    limit_keys = _login_limit_keys(request, username)
    if await _login_attempts(limit_keys[0]) >= LOGIN_IP_LIMIT or await _login_attempts(limit_keys[1]) >= LOGIN_ACCOUNT_LIMIT:
        raise HTTPException(429, "Muitas tentativas. Aguarde 10 minutos.")
    if not username or not password:
        raise HTTPException(400, "Informe usuário e senha.")
    credentials = Credentials(username, password)
    try:
        user = await _authenticate_sgp(credentials)
    except SGPError as exc:
        if exc.status in (401, 403):
            await _record_login_failure(limit_keys)
        raise
    try:
        await _clear_login_failures(limit_keys)
    except Exception:
        pass
    resolved_username = str(user.get("usuario") or username)
    role = await _role_for_login(resolved_username)
    # The primary SG Fibra account is authorized by deployment configuration
    # as well as the local role table, so a fresh ephemeral Render instance
    # can log in before its SQLite file has been populated.
    if not role and username.casefold() == "gon":
        role = "superadmin"
    if not role or role == "disabled":
        raise HTTPException(403, "Seu usuário ainda não foi autorizado pelo supervisor.")
    mode = role if role in ("office", "superadmin", "admin") else "technician"
    popup_members = await run_sync(popup_access_members)
    popup_enabled = any(_same_person(user.get("usuario", username), item.get("username")) for item in popup_members)
    user = {
        **user,
        "role": role,
        "modes": [mode],
        "mode": mode,
        "popup_enabled": popup_enabled,
    }
    ttl = REMEMBERED_SESSION_TTL if remember else SESSION_TTL
    session_id = await store.create_session(
        {
            "credential_username": username,
            "credential_password": password,
            "user": user,
            "expires": now + ttl,
        },
        ttl,
    )
    try:
        await run_sync(lambda: record_user(username, str(user.get("nome", username)), login=True))
    except Exception:
        # Login auditing must never invalidate an authenticated session.
        pass
    response.set_cookie(
        "ebf_session",
        session_id,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=(
            os.getenv("EBF_SECURE_COOKIE") == "1"
            or str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip() == "https"
            or request.url.scheme == "https"
        ),
    )
    return user


@app.post("/api/logout", status_code=204)
async def logout(request: Request, response: Response):
    session_id = request.cookies.get("ebf_session", "")
    if session_id:
        await store.delete_session(session_id)
    response.delete_cookie("ebf_session")


@app.get("/api/me")
async def me(context=Depends(session_context)):
    user = context[1]["user"]
    members = await run_sync(popup_access_members)
    user["popup_enabled"] = any(_same_person(user.get("usuario"), item.get("username")) for item in members)
    user["material_launch_policy"] = technician_material_launch_policy()
    return user


def validate_video_url(value: str) -> str:
    url = str(value or '').strip()
    if not url:
        return ''
    if url.startswith('/tutorial-media/') and Path(url).name == url.rsplit('/', 1)[-1]:
        return url
    from urllib.parse import urlparse
    parsed = urlparse(url)
    allowed = {'youtube.com', 'www.youtube.com', 'youtu.be', 'vimeo.com', 'www.vimeo.com'}
    host = (parsed.hostname or '').lower()
    direct_video = parsed.path.lower().endswith(('.mp4', '.webm', '.ogg', '.m3u8'))
    if parsed.scheme not in ('http', 'https') or (not host.endswith(tuple(allowed)) and not direct_video):
        raise HTTPException(400, 'URL de vídeo inválida. Use YouTube, Vimeo ou deixe em branco.')
    return url


@app.get('/api/tutorials')
async def tutorials_list(context=Depends(session_context)):
    include_drafts = context[1].get('user', {}).get('mode') == 'superadmin'
    return {'items': await run_sync(lambda: list_tutorials(include_drafts))}


@app.post('/api/superadmin/tutorials/upload')
async def tutorials_upload(file: UploadFile = File(...), context=Depends(session_context)):
    require_superadmin_session(context[1])
    allowed = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.ogg': 'video/ogg', '.m4v': 'video/mp4'}
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, 'Formato inválido. Use MP4, WebM, OGG ou M4V.')
    safe_name = f"{secrets.token_urlsafe(18)}{suffix}"
    target = TUTORIAL_UPLOADS / safe_name
    limit = 500 * 1024 * 1024
    total = 0
    try:
        with target.open('wb') as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > limit:
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, 'O vídeo excede o limite de 500 MB.')
                output.write(chunk)
    finally:
        await file.close()
    return {'url': f'/tutorial-media/{safe_name}', 'size': total, 'content_type': allowed[suffix]}


@app.post('/api/superadmin/tutorials')
async def tutorials_create(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    body['video_url'] = validate_video_url(body.get('video_url'))
    try:
        item = await run_sync(lambda: save_tutorial(body, None, str(context[1]['user'].get('usuario') or 'Gustavo')))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return item


@app.put('/api/superadmin/tutorials/{tutorial_id}')
async def tutorials_update(tutorial_id: int, request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    body['video_url'] = validate_video_url(body.get('video_url'))
    try:
        return await run_sync(lambda: save_tutorial(body, tutorial_id, str(context[1]['user'].get('usuario') or 'Gustavo')))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete('/api/superadmin/tutorials/{tutorial_id}', status_code=204)
async def tutorials_delete(tutorial_id: int, context=Depends(session_context)):
    require_superadmin_session(context[1])
    await run_sync(lambda: delete_tutorial(tutorial_id))


@app.get("/api/stock")
async def stock(request: Request, context=Depends(session_context)):
    force = request.query_params.get("refresh") == "1"
    session = context[1]
    return await cached(
        session,
        "stock",
        300,
        lambda: context[2].stock_for_user(
            session["user"].get("nome", ""),
            session["user"].get("usuario", ""),
        ),
        force=force,
        stale_ttl=3600,
    )

def require_technician(session: dict[str, Any]) -> None:
    if session.get("user", {}).get("mode") != "technician":
        raise SGPError("Entre com um perfil técnico para acessar esta função.", 403)

def require_superadmin_session(session: dict[str, Any]) -> None:
    if session.get("user", {}).get("mode") != "superadmin":
        raise SGPError("Acesso exclusivo para superadministradores.", 403)


async def require_voip_reader(session):
    if not VOIP_ENABLED:
        raise SGPError('Módulo VOIP não está habilitado nesta empresa.', 404)
    mode = session.get('user', {}).get('mode')
    if mode not in ('superadmin', 'office', 'admin'):
        raise SGPError('Acesso VOIP permitido para supervisores e escritório.', 403)
    if mode in ('office', 'admin'):
        return
    current = await run_sync(lambda: role_for(str(session.get('user', {}).get('usuario') or '')))
    if current != 'superadmin':
        raise SGPError('Acesso VOIP permitido somente para supervisores.', 403)


async def require_voip_supervisor(session):
    if not VOIP_ENABLED:
        raise SGPError('Módulo VOIP não está habilitado nesta empresa.', 404)
    require_superadmin_session(session)
    current = await run_sync(lambda: role_for(str(session.get('user', {}).get('usuario') or '')))
    if current != 'superadmin':
        raise SGPError('Alterações VOIP são exclusivas para supervisores.', 403)


async def require_voip_assigner(session):
    if not VOIP_ENABLED:
        raise SGPError('Módulo VOIP não está habilitado nesta empresa.', 404)
    if session.get('user', {}).get('mode') not in ('superadmin', 'office'):
        raise SGPError('Vinculação VOIP permitida para supervisores e escritório.', 403)


@app.get('/api/voip')
async def voip_inventory(context=Depends(session_context)):
    await require_voip_reader(context[1])
    from app import voip
    result = await run_sync(lambda: cache_snapshot(voip.snapshot_key(), retention_seconds=365 * 86400))
    return JSONResponse(result or {'items': [], 'updated_at': None}, headers={'Cache-Control': 'no-store'})


@app.post('/api/voip/refresh')
async def voip_refresh(context=Depends(session_context)):
    await require_voip_supervisor(context[1])
    from app import voip
    result = await run_sync(lambda: voip.refresh(save_cache_snapshot))
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@app.post('/api/voip/{number}/password')
async def voip_password(number: str, context=Depends(session_context)):
    await require_voip_supervisor(context[1])
    from app import voip
    result = await run_sync(lambda: voip.reveal(number))
    return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


@app.put('/api/voip/{number}')
async def voip_update(number: str, request: Request, context=Depends(session_context)):
    await require_voip_supervisor(context[1])
    from app import voip
    body = await request.json()
    if not isinstance(body, dict) or not ({'observation', 'password'} & set(body)):
        raise HTTPException(400, 'Informe a observação ou a nova senha.')
    result = await exclusive_cto_query(
        lambda: run_sync(lambda: voip.update_account(number, body.get('observation'), body.get('password'))),
        key=f'voip-update:{number}', busy_message='Este número já está sendo atualizado. Aguarde.',
    )
    snapshot = result.pop('inventory')
    await run_sync(lambda: save_cache_snapshot(voip.snapshot_key(), snapshot, 365 * 86400))
    return JSONResponse({**result, 'inventory': snapshot}, headers={'Cache-Control': 'no-store'})


@app.post('/api/voip/{number}/assign')
async def voip_assign(number: str, request: Request, context=Depends(session_context)):
    await require_voip_assigner(context[1])
    from app import voip
    from app.voip_assignment import assign, build_voip_note, resolve_internet_target
    from app.telephone_assignment import create_telephone_service
    from app.telephone_line_assignment import activate_telephone_service, create_telephone_line
    body = await request.json()
    requested_id = body.get('contract_id') if isinstance(body, dict) else None
    async def execute():
        secret = await run_sync(lambda: voip.reveal(number))
        target = await run_sync(lambda: resolve_internet_target(context[2], requested_id))
        note = build_voip_note(number, secret['password'])
        result = await run_sync(lambda: assign(context[2], target['service_id'], number, secret['password'], note))
        telephone = await run_sync(lambda: create_telephone_service(
            context[2], target['contract_id'], number, secret['password'], note,
        ))
        line = await run_sync(lambda: create_telephone_line(
            context[2], telephone['service_id'], number, secret['password'],
        ))
        activation = await run_sync(lambda: activate_telephone_service(
            context[2], telephone['service_id'],
        ))
        agil = await run_sync(lambda: voip.update_account(number, f"Contrato {target['contract_id']}"))
        snapshot = agil.pop('inventory')
        await run_sync(lambda: save_cache_snapshot(voip.snapshot_key(), snapshot, 365 * 86400))
        return {**result, 'contract_id': target['contract_id'], 'service_id': target['service_id'],
                'telephone': telephone, 'line': line, 'activation': activation, 'inventory': snapshot}
    result = await exclusive_cto_query(execute, key=f'voip-assign:{number}',
                                       busy_message='Este número já está sendo vinculado. Aguarde.')
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})

def require_office_tools(session: dict[str, Any]) -> None:
    if session.get("user", {}).get("mode") not in ("office", "superadmin"):
        raise SGPError("Acesso permitido para escritório e supervisores.", 403)


def require_onu_link_tools(session: dict[str, Any]) -> None:
    if session.get("user", {}).get("mode") not in ("technician", "office", "superadmin"):
        raise SGPError("Acesso permitido para técnicos, escritório e supervisores.", 403)


def session_username(session: dict[str, Any]) -> str:
    user = session.get("user", {})
    return str(user.get("usuario") or user.get("nome") or "").strip()


def technician_material_launch_policy(now: datetime | None = None) -> dict[str, Any]:
    """Operational window for technician material launches in São Paulo time."""
    local_now = now.astimezone(ZoneInfo("America/Sao_Paulo")) if now else datetime.now(ZoneInfo("America/Sao_Paulo"))
    allowed = 6 <= local_now.hour < 20
    next_time = (local_now.replace(hour=20, minute=0, second=0, microsecond=0) if allowed
                 else (local_now if local_now.hour < 6 else local_now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0))
    return {"allowed": allowed, "next_at": next_time.isoformat(), "timezone": "America/Sao_Paulo"}


def technician_owns_order(order: dict[str, Any], session: dict[str, Any]) -> bool:
    name = session.get("user", {}).get("nome") or session.get("user", {}).get("usuario")
    return bool(
        _same_person(order.get("os_tecnico_responsavel"), name)
        or any(_same_person(aux, name) for aux in order.get("os_tecnicos_auxiliares", []))
    )


@app.get("/api/technician/score")
async def technician_score(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    force = request.query_params.get("refresh") == "1"
    name = str(context[1]["user"].get("nome") or context[1]["user"].get("usuario") or "")
    return await cached(
        context[1], "technician-score-v6", 1800,
        lambda: context[2].team_productivity_report(postgres_operations.repeat_refunds(), name),
        force=force, stale_ttl=86400,
    )


@app.get("/api/technician/weekly-summary")
async def technician_weekly_summary(request: Request, context=Depends(session_context)):
    mode = context[1].get("user", {}).get("mode")
    if mode not in ("technician", "superadmin"):
        raise SGPError("Acesso permitido para técnicos e supervisores.", 403)
    force = request.query_params.get("refresh") == "1"
    user = context[1].get("user", {})
    if mode == "superadmin":
        username = str(request.query_params.get("technician") or "").strip()[:120]
        if not username:
            raise HTTPException(400, "Escolha um técnico para consultar.")
        registered = next((item for item in await run_sync(platform_users) if str(item.get("username") or "").casefold() == username.casefold()), None)
        if not registered or str(registered.get("role") or "") != "technician":
            raise HTTPException(404, "Técnico não encontrado nos acessos do app.")
        display_name = str(registered.get("name") or username).strip()
    else:
        username = str(user.get("usuario") or "").strip()
        display_name = str(user.get("nome") or username).strip()
    client = central_sgp_client() or context[2]
    week_start, week_end = _operational_week(_business_today())
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    friday_close = datetime.combine(week_end, clock_time(21, 0), tzinfo=now.tzinfo)
    retention = max(60, int((friday_close - now).total_seconds()))
    return await cached(
        context[1], f"weekly-summary-v6:{username.casefold()}:{week_start.isoformat()}", 600,
        lambda: client.technician_weekly_summary(username, display_name),
        force=force, stale_ttl=retention, persistent_ttl=retention,
    )


def _weekly_team_summary(client: SGPClient, technicians: list[dict[str, Any]]) -> dict[str, Any]:
    def load(item: dict[str, Any]) -> dict[str, Any]:
        username = str(item.get("username") or "").strip()
        name = str(item.get("name") or username).strip()
        try:
            report = client.technician_weekly_summary(username, name)
            totals = {"services": 0, "removals": 0, "devices": 0.0, "connectors": 0.0, "drop": 0.0}
            for day in report.get("days", []):
                totals["services"] += sum(int(day.get(key) or 0) for key in ("installations", "removals", "correctives", "other_services"))
                totals["removals"] += int(day.get("removals") or 0)
                totals["devices"] += sum(float(value or 0) for value in (day.get("devices") or {}).values())
                totals["connectors"] += float(day.get("connectors") or 0)
                totals["drop"] += float(day.get("drop") or 0)
            return {"username": username, "name": name, "totals": totals, "start": report.get("start"), "end": report.get("end")}
        except Exception as exc:
            return {"username": username, "name": name, "error": str(exc), "totals": {"services": 0, "removals": 0, "devices": 0, "connectors": 0, "drop": 0}}
    with ThreadPoolExecutor(max_workers=min(3, max(1, len(technicians)))) as pool:
        results = list(pool.map(load, technicians))
    week_start, week_end = _operational_week(_business_today())
    return {"start": week_start.isoformat(), "end": week_end.isoformat(), "technicians": results}


@app.get("/api/superadmin/weekly-summary-all")
async def weekly_summary_all(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    force = request.query_params.get("refresh") == "1"
    technicians = [item for item in await run_sync(platform_users) if str(item.get("role") or "") == "technician"]
    client = central_sgp_client() or context[2]
    week_start, week_end = _operational_week(_business_today())
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    retention = max(60, int((datetime.combine(week_end, clock_time(21), tzinfo=now.tzinfo) - now).total_seconds()))
    return await cached(
        context[1], f"weekly-summary-all-v4:{week_start.isoformat()}", retention,
        lambda: _weekly_team_summary(client, technicians), force=force,
        stale_ttl=retention, persistent_ttl=retention,
    )


@app.get("/api/technician/orders")
async def technician_orders(request: Request, context=Depends(session_context)):
    require_technician(context[1])
    try:
        status = int(request.query_params.get("status", "0"))
    except ValueError:
        raise HTTPException(400, "Situação inválida.")
    if status not in (0, 1, 2, 3):
        raise HTTPException(400, "Situação inválida.")
    force = request.query_params.get("refresh") == "1"
    return await cached(
        context[1], f"orders-today:{status}", 120,
        lambda: context[2].orders_by_status(
            context[1]["user"].get("nome") or context[1]["user"].get("usuario") or "",
            status,
        ),
        force=force, stale_ttl=900,
    )


@app.get("/api/technician/orders/{order_id}")
async def technician_order(order_id: int, request: Request, context=Depends(session_context)):
    mode = context[1].get("user", {}).get("mode")
    if mode not in ("technician", "office", "superadmin"):
        raise SGPError("Acesso permitido para técnicos, escritório e supervisores.", 403)
    def load(client: SGPClient, session: dict[str, Any]):
        order = client.order(order_id, include_onu=True, include_signal_context=False)
        if not order or (mode == "technician" and not technician_owns_order(order, session)):
            raise SGPError("Esta OS não pertence à sua rota de hoje.", 403)
        return {
            "order": order,
            "materials": client.order_materials(order_id),
            "photos": client.order_photos(order_id),
        }
    payload = await cached(
        context[1], f"order-detail-v1:{order_id}", 120,
        lambda: load(context[2], context[1]),
        force=request.query_params.get("refresh") == "1", stale_ttl=900, persistent_ttl=86400,
    )
    return {
        **payload,
        "draft": await run_sync(lambda: postgres_operations.order_text_draft(order_id, session_username(context[1]))) if mode == "technician" else None,
    }


@app.get("/api/technician/orders/{order_id}/disconnects")
async def technician_order_disconnects(order_id: int, request: Request, context=Depends(session_context)):
    mode = context[1].get("user", {}).get("mode")
    if mode not in ("technician", "office", "superadmin"):
        raise SGPError("Acesso permitido para técnicos, escritório e supervisores.", 403)
    try:
        start = datetime.strptime(str(request.query_params.get("start") or ""), "%Y-%m-%d").date()
        end = datetime.strptime(str(request.query_params.get("end") or ""), "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(400, "Informe as datas inicial e final.") from exc
    if start > end or (end - start).days > 92:
        raise HTTPException(400, "Escolha um período de até 93 dias.")
    def load(client: SGPClient, session: dict[str, Any]):
        order = client.order(order_id, include_onu=False, include_signal_context=False)
        if not order or (mode == "technician" and not technician_owns_order(order, session)):
            raise SGPError("Esta OS não pertence à sua rota de hoje.", 403)
        client_id = int(order.get("cliente_id") or 0)
        contract_id = int(order.get("contrato_id") or order.get("clientecontrato_id") or 0)
        service_id = int(order.get("servico_id") or 0)
        if not client_id or not contract_id:
            raise SGPError("O SGP não informou o cliente ou contrato desta OS.", 422)
        try:
            return client.traffic_disconnects(client_id, contract_id, start, end, service_id or None)
        except SGPError as exc:
            if exc.status != 403:
                raise
            traffic_client = central_sgp_client()
            if not traffic_client:
                raise SGPError(
                    "O usuário técnico não acessa o extrato. Configure a credencial central de tráfego no servidor.", 503,
                ) from exc
            return traffic_client.traffic_disconnects(client_id, contract_id, start, end, service_id or None)
    ttl = 300 if start <= _business_today() <= end else 86400
    return await cached(
        context[1], f"disconnects-v1:{order_id}:{start.isoformat()}:{end.isoformat()}", ttl,
        lambda: load(context[2], context[1]), stale_ttl=86400, persistent_ttl=30 * 86400,
    )


@app.post("/api/technician/orders/{order_id}/onu-label")
async def technician_order_onu_label(order_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    body = await request.json()
    label = " ".join(str(body.get("label") or "").split())
    vlan = body.get("vlan")
    if not label:
        raise HTTPException(400, "Informe a nova etiqueta do modem.")

    order = await run_sync(lambda: context[2].order(order_id, include_onu=True, include_signal_context=False))
    if not order or not technician_owns_order(order, context[1]):
        raise SGPError("Esta OS não pertence ao técnico logado.", 403)
    onu = order.get("onu") if isinstance(order.get("onu"), dict) else {}
    onu_id = onu.get("onu") or onu.get("onuid") or onu.get("pk") or order.get("onu_id")
    if not onu_id and str(onu.get("id") or "").isdigit():
        onu_id = onu.get("id")
    if not onu_id:
        raise SGPError("O SGP não informou o código interno do modem vinculado.", 422)
    central = central_sgp_client()
    if not central:
        raise SGPError("Configure a credencial central do SGP no servidor.", 503)
    result = await run_sync(lambda: central.update_onu_label(onu_id, label, vlan))
    await invalidate_cached(context[1], f"technical-info-v1:{order_id}")
    await invalidate_cached(context[1], f"order-detail-v1:{order_id}")
    return {"ok": True, "result": result}


@app.get("/api/technician/orders/{order_id}/technical-info")
async def technician_order_technical_info(order_id: int, request: Request, context=Depends(session_context)):
    mode = context[1].get("user", {}).get("mode")
    if mode not in ("technician", "office", "superadmin"):
        raise SGPError("Acesso permitido para técnicos, escritório e supervisores.", 403)

    def load():
        client = context[2]
        order = client.order(order_id, include_onu=True, include_signal_context=False)
        if not order or (mode == "technician" and not technician_owns_order(order, context[1])):
            raise SGPError("Esta OS não pertence à sua rota de hoje.", 403)
        login = str(order.get("servico_login") or "").strip()
        radius = (central_sgp_client() or client).radius_technical_info(login)
        onu = order.get("onu") if isinstance(order.get("onu"), dict) else {}
        radius["onu"] = {
            "serial": onu.get("phy_addr") or onu.get("serial") or onu.get("id"),
            "olt": onu.get("olt_name") or onu.get("olt"), "slot": onu.get("slot"), "pon": onu.get("pon"),
            "signal": (onu.get("signal_context") or {}).get("current") if isinstance(onu.get("signal_context"), dict) else onu.get("info_rx"),
        }
        return radius

    return await cached(
        context[1], f"technical-info-v1:{order_id}", 300, load,
        force=request.query_params.get("refresh") == "1", stale_ttl=86400,
    )


@app.get("/api/technician/orders/{order_id}/pon-analysis")
async def technician_order_pon_analysis(order_id: int, request: Request, context=Depends(session_context)):
    mode = context[1].get("user", {}).get("mode")
    if mode not in ("technician", "office", "superadmin"):
        raise SGPError("Acesso permitido para técnicos, escritório e supervisores.", 403)

    def load():
        order = context[2].order(order_id, include_onu=False, include_signal_context=False)
        if not order or (mode == "technician" and not technician_owns_order(order, context[1])):
            raise SGPError("Esta OS não pertence à sua rota de hoje.", 403)
        contract_id = int(order.get("contrato_id") or order.get("clientecontrato_id") or 0)
        if not contract_id:
            raise SGPError("O SGP não informou o contrato desta OS.", 422)
        central = central_sgp_client()
        if not central:
            raise SGPError("Configure a credencial central do SGP no servidor.", 503)
        return central.pon_attenuation(contract_id)

    return await cached(
        context[1], f"pon-analysis-v2:{order_id}", 300, load,
        force=request.query_params.get("refresh") == "1", stale_ttl=1800,
    )


@app.post("/api/technician/orders/{order_id}/status")
async def technician_order_status(order_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    body = await request.json()
    try:
        status = int(body.get("status"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Situação inválida.")
    service = str(body.get("service_performed") or "")[:4000]
    def update(client: SGPClient, session: dict[str, Any]):
        order = client.order(order_id, include_onu=False)
        if not order or not technician_owns_order(order, session):
            raise SGPError("Esta OS não pertence ao técnico logado.", 403)
        return client.update_order_status(order_id, status, service, current=order)
    result = await call_context(context, update)
    tracking_warning = None
    if status in (1, 3):
        await _end_order_tracking(order_id, "finished" if status == 1 else "paused")
    elif status == 2:
        # Each trip gets a fresh token. Pausing/reopening an OS must not reuse
        # the prior customer's link.
        try:
            await _prepare_order_tracking(order_id, context)
        except HTTPException as exc:
            # The SGP status update already succeeded. Report tracking setup
            # separately so the UI never claims GPS/chat are active.
            tracking_warning = str(exc.detail)
        else:
            tracking_warning = None
    await run_sync(lambda: postgres_operations.delete_order_text_draft(order_id, session_username(context[1])))
    await invalidate_cached(context[1], "orders-today:")
    await invalidate_cached(context[1], "technician-score")
    await invalidate_cached(context[1], f"order-detail-v1:{order_id}")
    return {"ok": True, "result": result, "tracking_warning": tracking_warning}


@app.get("/api/technician/orders/{order_id}/tracking")
async def technician_order_tracking(order_id: int, context=Depends(session_context)):
    require_technician(context[1])
    session = await _tracking_for_technician(order_id, context)
    token = session.get("token")
    if not token:
        # Tokens are never persisted in recoverable form. Return the URL only
        # at session creation; clients can keep using its token thereafter.
        index = await run_sync(lambda: postgres_operations.tracking_session(f"os-index-{order_id}"))
        token_hash = str((index or {}).get("token_hash") or "")
        session = await run_sync(lambda: postgres_operations.tracking_session(token_hash)) or session
    return {"state": session.get("state"), "order_id": order_id,
            "token": session.get("ready_token") if session.get("state") == "ready" else None,
            "tracking_url": session.get("tracking_url"), "last_location": session.get("last_location"),
            "messages": session.get("messages", []), "unread_technician": session.get("unread_technician", 0),
            "expires_at": session.get("expires_at")}


@app.post("/api/technician/orders/{order_id}/tracking/start")
async def technician_start_order_tracking(order_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    body = await request.json()
    def validate_running():
        order = context[2].order(order_id, include_onu=False, include_signal_context=False)
        if not order or not technician_owns_order(order, context[1]):
            raise SGPError("Esta OS não pertence ao técnico logado.", 403)
        if int(order.get("os_status") or -1) != 2:
            raise SGPError("Inicie a OS como Em execução antes de ativar o acompanhamento.", 409)
    await call_context(context, lambda client, session: validate_running())
    session = await _tracking_for_technician(order_id, context)
    if session.get("state") in ("arrived", "finished", "cancelled", "paused"):
        raise HTTPException(409, "O acompanhamento desta visita foi encerrado.")
    try:
        lat, lon = float(body["latitude"]), float(body["longitude"])
        accuracy = max(0.0, min(float(body.get("accuracy") or 0), 5000.0))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Localização inválida.")
    session["state"] = "traveling"
    session["last_location"] = {"latitude": lat, "longitude": lon, "accuracy": accuracy, "updated_at": time.time()}
    token = str(session.pop("ready_token", ""))
    if not token:
        raise HTTPException(409, "O link desta visita já foi criado e não pode ser regenerado.")
    session["tracking_url"] = str(request.base_url).rstrip("/") + f"/acompanhar/{token}"
    session.pop("token", None)
    await run_sync(lambda: postgres_operations.save_tracking_session(session["token_hash"], session, 12 * 3600))
    return {"state": "traveling", "tracking_url": session["tracking_url"], "expires_at": session["expires_at"]}


@app.post("/api/technician/orders/{order_id}/tracking/location")
async def technician_order_location(order_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    session = await _tracking_for_technician(order_id, context)
    if session.get("state") != "traveling": raise HTTPException(409, "O deslocamento não está ativo.")
    if session.get("technician") and not _same_person(session.get("technician"), _technician_identity(context[1])):
        raise HTTPException(403, "Esta sessão não pertence ao técnico logado.")
    body = await request.json()
    try:
        lat, lon = float(body["latitude"]), float(body["longitude"])
        accuracy = max(0.0, min(float(body.get("accuracy") or 0), 5000.0))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180): raise ValueError
    except (KeyError, TypeError, ValueError): raise HTTPException(400, "Localização inválida.")
    session["last_location"] = {"latitude": lat, "longitude": lon, "accuracy": accuracy, "updated_at": time.time()}
    await run_sync(lambda: postgres_operations.save_tracking_session(session["token_hash"], session, 12 * 3600))
    return {"ok": True}


@app.post("/api/technician/orders/{order_id}/tracking/arrived")
async def technician_order_arrived(order_id: int, context=Depends(session_context)):
    require_technician(context[1])
    session = await _tracking_for_technician(order_id, context)
    if session.get("state") != "traveling": raise HTTPException(409, "Não há deslocamento ativo para esta OS.")
    session["state"] = "arrived"
    session["arrived_at"] = time.time()
    session["last_location"] = None
    session["chat_closed"] = True
    await run_sync(lambda: postgres_operations.save_tracking_session(session["token_hash"], session, 12 * 3600))
    return {"state": "arrived", "arrived_at": session["arrived_at"]}


@app.get("/api/public/tracking/{token}")
async def public_order_tracking(token: str):
    try:
        session = await _tracking_for_token(token)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        session = None
    if session is None:
        return await _legacy_public_tracking_payload(token)
    if session.get("state") in ("cancelled", "finished", "paused"):
        raise HTTPException(410, "Este acompanhamento foi encerrado.")
    loc = session.get("last_location")
    fresh = session.get("state") == "traveling" and loc and time.time() - float(loc.get("updated_at") or 0) <= 90
    return {"client": session.get("client"), "technician": str(session.get("technician") or "Técnico").split()[0],
            "destination": session.get("destination"), "location": loc if fresh else None,
            "status": "arrived" if session.get("state") == "arrived" else "on_the_way" if fresh else "location_unavailable",
            "updated_at": loc.get("updated_at") if loc else None,
            "arrived_at": session.get("arrived_at"),
            "messages": session.get("messages", []), "chat_open": session.get("state") == "traveling",
            "unread_client": session.get("unread_client", 0)}


@app.post("/api/public/tracking/{token}/messages")
async def public_order_tracking_message(token: str, request: Request):
    body = await request.json()
    try:
        session = await _tracking_for_token(token)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        raise HTTPException(410, "Chat indisponível para este link antigo.")
    return await _post_tracking_message(session, "Cliente", body.get("text"), "client")


async def _legacy_public_tracking_payload(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", token):
        raise HTTPException(404, "Acompanhamento não encontrado.")
    tracking = await run_sync(lambda: cache_snapshot(_tracking_key(token), retention_seconds=8 * 3600))
    if not tracking or float(tracking.get("expires_at") or 0) <= time.time():
        raise HTTPException(410, "Este acompanhamento expirou.")
    active = await run_sync(lambda: cache_snapshot(_active_tracking_key(tracking["technician"]), retention_seconds=8 * 3600))
    if not active or not hmac.compare_digest(str(active.get("token_hash") or ""), hashlib.sha256(token.encode()).hexdigest()):
        raise HTTPException(410, "Este acompanhamento foi encerrado ou substituído por outro serviço.")
    shift = await run_sync(lambda: cache_snapshot(_technician_shift_key(tracking["technician"]), retention_seconds=2 * 86400))
    location = await run_sync(lambda: cache_snapshot(_live_location_key(tracking["technician"]), retention_seconds=12 * 3600))
    fresh = bool(_shift_is_active(shift) and location and time.time() - float(location.get("updated_at") or 0) <= 180)
    public_location = ({key: location.get(key) for key in ("latitude", "longitude", "accuracy", "updated_at")} if fresh else None)
    return {"client": tracking["client"], "technician": tracking["technician"].split()[0],
            "scheduled_at": tracking.get("scheduled_at"), "destination": tracking["destination"],
            "location": public_location, "status": "on_the_way" if fresh else "waiting_location",
            "expires_at": tracking["expires_at"], "messages": [], "chat_open": False}


async def _post_tracking_message(session: dict[str, Any], author: str, text_value: str, side: str):
    if session.get("state") != "traveling" or session.get("chat_closed"):
        raise HTTPException(409, "O chat foi encerrado quando o técnico chegou ao endereço.")
    text_value = str(text_value or "").strip()
    if not text_value or len(text_value) > 1000: raise HTTPException(400, "A mensagem deve ter de 1 a 1000 caracteres.")
    messages = session.setdefault("messages", [])
    recent = [m for m in messages if float(m.get("created_at") or 0) > time.time() - 60 and m.get("side") == side]
    if len(recent) >= 10: raise HTTPException(429, "Aguarde antes de enviar novas mensagens.")
    messages.append({"author": author, "text": text_value, "created_at": time.time(), "side": side})
    session["messages"] = messages[-300:]
    target = "unread_client" if side == "technician" else "unread_technician"
    session[target] = int(session.get(target) or 0) + 1
    await run_sync(lambda: postgres_operations.save_tracking_session(session["token_hash"], session, 12 * 3600))
    return {"ok": True}


@app.post("/api/technician/orders/{order_id}/tracking/messages")
async def technician_tracking_message(order_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    session = await _tracking_for_technician(order_id, context)
    body = await request.json()
    return await _post_tracking_message(session, _technician_identity(context[1]), body.get("text"), "technician")


@app.post("/api/technician/orders/{order_id}/draft")
async def technician_order_draft(order_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    body = await request.json()
    value = str(body.get("text") or "")[:4000]
    result = await run_sync(
        lambda: postgres_operations.save_order_text_draft(order_id, session_username(context[1]), value),
    )
    return {"ok": True, "updated_at": result["updated_at"]}


@app.post("/api/technician/orders/{order_id}/materials")
async def technician_order_materials(order_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    if not technician_material_launch_policy()["allowed"]:
        raise HTTPException(423, "Lançamento de materiais fica bloqueado entre 20h00 e 06h00. Tente novamente a partir das 06h00.")
    body = await request.json()
    items = body.get("items") if isinstance(body.get("items"), list) else []
    mode = str(body.get("mode") or "comodato").strip().casefold()
    source = f"os-materials:{order_id}:{time.time_ns()}"
    def launch(client: SGPClient, session: dict[str, Any]):
        order = client.order(order_id, include_onu=False)
        if not order or not technician_owns_order(order, session):
            raise SGPError("Esta OS não pertence ao técnico logado.", 403)
        user = session["user"]
        logistics.audit(source, logistics_actor(session), {"action": "Lançamento de materiais solicitado", "os_id": order_id, "items": items})
        return client.add_order_materials(
            order_id, user.get("nome", ""), user.get("usuario", ""), items,
            order=order, mode=mode,
        )
    try:
        result = await call_context(context, launch)
    except Exception:
        await logistics_result(source, logistics_actor(context[1]), {"action": "Lançamento não confirmado; conferir OS no SGP"})
        raise
    else:
        await logistics_result(source, logistics_actor(context[1]), {"action": "Materiais lançados", "os_id": order_id})
    finally:
        await stock_mutation_finished()
    await invalidate_cached(context[1], "stock")
    await invalidate_cached(context[1], f"order-detail-v1:{order_id}")
    return {"ok": True, "result": result}


@app.post("/api/technician/orders/{order_id}/photos")
async def technician_order_photo(order_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    body = await request.json()
    def upload(client: SGPClient, session: dict[str, Any]):
        order = client.order(order_id, include_onu=False)
        if not order or not technician_owns_order(order, session):
            raise SGPError("Esta OS não pertence ao técnico logado.", 403)
        return client.add_order_photo(
            order_id, str(body.get("description") or "")[:500],
            str(body.get("filename") or "")[:180], str(body.get("content_type") or ""),
            str(body.get("data") or ""),
        )
    result = await call_context(context, upload)
    await invalidate_cached(context[1], f"order-detail-v1:{order_id}")
    return {"ok": True, "result": result}


@app.get("/api/technician/orders/{order_id}/photos/{attachment_id}")
async def technician_order_photo_content(
    order_id: int, attachment_id: int, context=Depends(session_context),
):
    mode = context[1].get("user", {}).get("mode")
    if mode not in ("technician", "office", "superadmin"):
        raise SGPError("Acesso permitido para técnicos, escritório e supervisores.", 403)

    def load(client: SGPClient, session: dict[str, Any]):
        order = client.order(order_id, include_onu=False, include_signal_context=False)
        if not order or (mode == "technician" and not technician_owns_order(order, session)):
            raise SGPError("Esta OS não pertence à sua rota de hoje.", 403)
        photos = client.order_photos(order_id)
        valid_ids = {
            int(value)
            for photo in photos
            for value in (photo.get("id"), photo.get("imagem_id"), photo.get("anexo_id"))
            if str(value or "").isdigit()
        }
        if attachment_id not in valid_ids:
            raise SGPError("Foto não encontrada nesta OS.", 404)
        return client.order_photo(attachment_id)

    content, content_type = await call_context(context, load)
    return Response(
        content=content,
        media_type=content_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=3600", "Content-Disposition": "inline"},
    )


@app.get("/api/technician/olts")
async def technician_olts(context=Depends(session_context)):
    require_technician(context[1])
    return await cached(context[1], "olts", 1800, lambda: context[2].olts(), stale_ttl=86400)


@app.get("/api/superadmin/onu-link/client")
async def superadmin_onu_link_client(client_id: int | None = None, contract_id: int | None = None, context=Depends(session_context)):
    require_onu_link_tools(context[1])
    identifier = contract_id if contract_id is not None else client_id
    if identifier is None or identifier <= 0:
        raise HTTPException(400, "Informe um cliente ou contrato válido.")
    cache_key = f"onu-link-contract-v1:{contract_id}" if contract_id is not None else f"onu-link-client-v2:{client_id}"
    return await cached(
        context[1], cache_key, 300,
        lambda: context[2].onu_link_client(identifier, contract_id), stale_ttl=3600, persistent_ttl=86400,
    )


@app.get("/api/office/modem-search")
async def office_modem_search(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    suffix = str(request.query_params.get("suffix") or "").strip()
    return await cached(
        context[1], f"modem-search-v2:{suffix.upper()}", 300,
        lambda: context[2].find_onu_by_suffix_detailed(suffix),
        stale_ttl=1800,
    )


@app.get("/api/office/open-service-map")
async def office_open_service_map(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    service_day = _business_today()
    daily_key = f"daily-open-service-map:{service_day.isoformat()}"
    def load_map():
        delete_cache_snapshots_except("daily-open-service-map:", daily_key)
        previous = cache_snapshot(daily_key) or {}
        result = context[2].open_service_map(service_day, previous.get("items") or [])
        save_cache_snapshot(daily_key, result)
        return result
    return await cached(
        context[1], f"open-service-map-v4:{service_day.isoformat()}", 45,
        load_map, force=request.query_params.get("refresh") == "1", stale_ttl=300,
    )


@app.get("/api/office/itinerary")
async def office_itinerary(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    technician = str(request.query_params.get("technician") or "").strip()
    members = await run_sync(route_monitor_members)
    allowed = {str(m.get("display_name") or m.get("username") or "").strip() for m in members}
    if not technician or technician not in allowed:
        raise HTTPException(400, "Selecione um técnico do mapa.")
    day = _business_today()
    key = hashlib.sha256(technician.encode()).hexdigest()[:20]
    return await cached(context[1], f"route-itinerary:{day}:{key}", 60,
                        lambda: context[2].technician_itinerary(technician, day),
                        force=request.query_params.get("refresh") == "1", stale_ttl=180)


def _itinerary_road_route(points: list[tuple[float, float]]) -> dict[str, Any]:
    """Fetch a road route server-side so browser CORS never falls back to a straight line."""
    coordinates = ";".join(f"{longitude:.6f},{latitude:.6f}" for latitude, longitude in points)
    query = f"/route/v1/driving/{coordinates}?overview=full&geometries=geojson&steps=true"
    route = None
    last_error: Exception | None = None
    for provider in ("https://router.project-osrm.org", "https://routing.openstreetmap.de/routed-car"):
        try:
            request = urllib.request.Request(provider + query, headers={"User-Agent": "EBF-Tecnico/1.0"})
            with urllib.request.urlopen(request, timeout=18) as response:  # noqa: S310 - fixed public routing providers
                route = (json.loads(response.read().decode("utf-8")) or {}).get("routes", [None])[0]
            if route:
                break
        except Exception as error:
            last_error = error
    if not route:
        raise SGPError("Não foi possível calcular a rota pelas ruas agora.", 503) from last_error
    if not isinstance(route, dict) or not isinstance(route.get("geometry", {}).get("coordinates"), list):
        raise SGPError("O roteador não retornou um trajeto válido.", 503)
    legs = []
    for item in route.get("legs", []):
        leg_coordinates: list[list[float]] = []
        for step in item.get("steps", []):
            coordinates = step.get("geometry", {}).get("coordinates", [])
            if not isinstance(coordinates, list):
                continue
            if leg_coordinates and coordinates and leg_coordinates[-1] == coordinates[0]:
                leg_coordinates.extend(coordinates[1:])
            else:
                leg_coordinates.extend(coordinates)
        legs.append({"distance": float(item.get("distance") or 0), "geometry": {"coordinates": leg_coordinates}})
    return {
        # Keep the same GeoJSON shape used by the browser map renderer.
        "geometry": {"coordinates": route["geometry"]["coordinates"]},
        "legs": legs,
    }


@app.get("/api/office/itinerary-route")
async def office_itinerary_route(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    raw = str(request.query_params.get("points") or "")
    points: list[tuple[float, float]] = []
    try:
        for item in raw.split(";"):
            latitude, longitude = (float(value) for value in item.split(",", 1))
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError
            points.append((latitude, longitude))
    except (TypeError, ValueError):
        raise HTTPException(400, "Coordenadas inválidas para o itinerário.")
    if not 2 <= len(points) <= 25:
        raise HTTPException(400, "Informe entre 2 e 25 paradas para traçar a rota.")
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return await cached(context[1], f"route-itinerary-road-v2:{key}", 900,
                        lambda: _itinerary_road_route(points), stale_ttl=86400)


@app.get("/api/office/pon-road-route")
async def office_pon_road_route(request: Request, context=Depends(session_context)):
    """Trace a selected PON through streets without making the browser use a straight line."""
    require_office_tools(context[1])
    raw = str(request.query_params.get("points") or "")
    points: list[tuple[float, float]] = []
    try:
        for item in raw.split(";"):
            latitude, longitude = (float(value) for value in item.split(",", 1))
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError
            points.append((latitude, longitude))
    except (TypeError, ValueError):
        raise HTTPException(400, "Coordenadas inválidas para a PON.")
    if not 2 <= len(points) <= 25:
        raise HTTPException(400, "Informe entre 2 e 25 pontos da PON.")
    key = hashlib.sha256(raw.encode()).hexdigest()[:24]
    return await cached(
        context[1], f"pon-road-route-v1:{key}", 365 * 86400,
        lambda: _itinerary_road_route(points), stale_ttl=365 * 86400,
        persistent_ttl=365 * 86400, shared=True,
    )


@app.get("/api/office/orders/{order_id}/assignment")
async def office_assignment_options(order_id: int, context=Depends(session_context)):
    require_office_tools(context[1])
    return await run_sync(lambda: context[2].order_assignment(order_id))


@app.get("/api/office/orders/{order_id}/certification")
async def office_certification(order_id: int, context=Depends(session_context)):
    require_office_tools(context[1])
    from app.certification import overview
    return await run_sync(lambda: overview(context[2], order_id))


@app.get("/api/office/orders/{order_id}/certification/signals")
async def office_certification_signals(order_id: int, context=Depends(session_context)):
    require_office_tools(context[1])
    from app.certification import signals
    return await cached(context[1], f"certification-signals-v1:{order_id}", 120,
                        lambda: signals(context[2], order_id))


@app.post("/api/office/orders/{order_id}/certification")
async def office_save_certification(order_id: int, request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    from app.certification import save
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Certificação inválida.")
    actor = session_username(context[1])
    async def execute():
        result = await run_sync(lambda: save(context[2], order_id, body, actor))
        await logistics_result(f"certification:{order_id}:{body.get('request_id')}", actor,
                               {"action": "Certificação salva no cliente SGP", "os_id": order_id})
        return result
    return await exclusive_cto_query(execute, key=f"certification:{order_id}",
                                     busy_message="Esta OS já está recebendo uma certificação. Aguarde.")


@app.get("/api/office/pon-analysis")
async def office_pon_analysis(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    try:
        olt_id = int(request.query_params.get("olt_id") or 0)
        slot = int(request.query_params.get("slot") or -1)
        pon = int(request.query_params.get("pon") or -1)
    except (TypeError, ValueError):
        raise HTTPException(400, "Selecione uma OLT, um Slot e uma PON válidos.")
    if olt_id <= 0 or slot < 0 or pon < 0:
        raise HTTPException(400, "Selecione uma OLT, um Slot e uma PON.")
    client = central_sgp_client() or context[2]
    return await cached(
        context[1], f"pon-port-analysis-v1:{olt_id}:{slot}:{pon}", 300,
        lambda: client.pon_port_analysis(olt_id, slot, pon),
        force=request.query_params.get("refresh") == "1", stale_ttl=1800, shared=True,
    )


@app.post("/api/office/orders/{order_id}/assignment")
async def office_assign_order(order_id: int, request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    body = await request.json()
    try:
        technician_id = int(body.get("technician_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Selecione um técnico válido.")
    if order_id <= 0 or technician_id <= 0 or not isinstance(body.get("revision"), str):
        raise HTTPException(400, "Reabra a seleção de técnico desta OS.")
    actor = session_username(context[1])
    source = f"order-assignment:{order_id}:{time.time_ns()}"
    async def execute():
        await run_sync(lambda: logistics.audit(source, actor, {
            "action": "Direcionamento de OS solicitado", "os_id": order_id, "technician_id": technician_id,
        }))
        try:
            result = await run_sync(lambda: context[2].order_assignment(order_id, technician_id, body["revision"]))
        except Exception:
            await logistics_result(source, actor, {"action": "Direcionamento não confirmado; conferir OS no SGP", "os_id": order_id})
            raise
        finally:
            await stock_mutation_finished()
        await logistics_result(source, actor, {"action": "Direcionamento confirmado no SGP", **result})
        # Reassignment revokes the old public link immediately. A new
        # technician/session can create a fresh link when the OS starts again.
        await _end_order_tracking(order_id, "cancelled")
        return result
    return await exclusive_cto_query(execute, key=f"order-assignment:{order_id}",
                                     busy_message="Esta OS já está sendo direcionada. Aguarde a conclusão.")


@app.get("/api/office/withdrawal-candidates")
async def office_withdrawal_candidates(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    service_day = _business_today().isoformat()
    withdrawal_client = central_sgp_client() or context[2]
    return await cached(
        context[1], f"withdrawal-candidates-v2:{service_day}", 86400,
        lambda: withdrawal_client.withdrawal_candidates(40),
        force=request.query_params.get("refresh") == "1", stale_ttl=86400,
    )


@app.post("/api/office/withdrawals")
async def office_create_withdrawal(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    body = await request.json()
    try:
        contract_id = int(body.get("contract_id") or 0)
        technician_id = int(body.get("technician_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Contrato ou técnico inválido.")
    if contract_id <= 0 or technician_id <= 0:
        raise HTTPException(400, "Selecione o cliente e o técnico.")
    lock_key = f"operation:create-withdrawal:{contract_id}"
    if not await store.acquire(lock_key, 120):
        raise HTTPException(409, "A retirada deste contrato já está sendo aberta.")
    opened_by = str((context[1].get("user") or {}).get("nome") or (context[1].get("user") or {}).get("usuario") or "Gustavo")
    withdrawal_client = central_sgp_client() or context[2]
    try:
        result = await run_sync(lambda: withdrawal_client.create_withdrawal_order(contract_id, technician_id, opened_by))
    finally:
        await store.release(lock_key)
    await store.delete_prefix(f"cache:data:{cache_identity(context[1])}:withdrawal-candidates-v2:")
    return result


@app.get("/api/superadmin/onu-link/olts")
async def superadmin_onu_link_olts(context=Depends(session_context)):
    require_onu_link_tools(context[1])
    return await cached(context[1], "olts", 1800, lambda: context[2].olts(), stale_ttl=86400)


@app.get("/api/superadmin/onu-link/olts/{olt_id}/unauthorized")
async def superadmin_onu_link_unauthorized(olt_id: int, context=Depends(session_context)):
    require_onu_link_tools(context[1])
    return await call_context(context, lambda client, _session: client.unauthorized_onus(olt_id))


@app.get("/api/superadmin/onu-link/options")
async def superadmin_onu_link_options(context=Depends(session_context)):
    require_onu_link_tools(context[1])
    return await cached(context[1], "onu-options", 3600, lambda: context[2].onu_options(), stale_ttl=86400)


@app.post("/api/superadmin/onu-link/olts/{olt_id}/authorize")
async def superadmin_onu_link_authorize(olt_id: int, request: Request, context=Depends(session_context)):
    require_onu_link_tools(context[1])
    body = await request.json()
    try:
        client_id = int(body.pop("client_id", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "ID do cliente inválido.")
    if client_id <= 0:
        raise HTTPException(400, "ID do cliente inválido.")
    try:
        contract_id = int(body.get("contrato") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Contrato inválido.")
    if contract_id <= 0:
        raise HTTPException(400, "Contrato inválido.")
    expected = await call_context(context, lambda client, _session: client.onu_link_client(client_id, contract_id))
    if int(expected.get("client_id") or 0) != client_id:
        raise HTTPException(400, "O contrato não pertence ao cliente informado.")
    if str(body.get("contrato") or "") != str(expected.get("contract_id") or ""):
        raise HTTPException(400, "O contrato não pertence ao cliente informado.")
    if str(body.get("service") or "") != str(expected.get("service") or ""):
        raise HTTPException(400, "O serviço não pertence ao cliente informado.")
    body.update({
        "contrato": expected["contract_id"], "service": expected["service"],
        "description": expected["description"], "pppoe_login": expected["service"],
    })
    try:
        result = await call_context(context, lambda client, _session: client.authorize_onu(olt_id, body))
    except SGPError as exc:
        # The SGP can commit the ONU and close the connection before returning
        # its JSON response. Confirm the resulting state before telling the
        # technician that the operation failed (and prompting a duplicate try).
        if exc.status < 500:
            raise
        try:
            confirmed = await call_context(
                context, lambda client, _session: client.onu_link_client(client_id, contract_id)
            )
        except SGPError:
            raise exc
        submitted_onu = re.sub(r"[^A-Z0-9]", "", str(body.get("id") or "").upper())
        linked_onu = re.sub(r"[^A-Z0-9]", "", str(confirmed.get("linked_onu_id") or "").upper())
        if not submitted_onu or submitted_onu != linked_onu:
            raise exc
        result = {"ok": True, "confirmed_after_sgp_timeout": True}
    await invalidate_cached(context[1], f"onu-link-client-v2:{client_id}")
    await invalidate_cached(context[1], f"onu-link-contract-v1:{contract_id}")
    return {"ok": True, "result": result}


@app.get("/api/technician/olts/{olt_id}/unauthorized")
async def technician_unauthorized_onus(olt_id: int, context=Depends(session_context)):
    require_technician(context[1])
    return await call_context(context, lambda client, _session: client.unauthorized_onus(olt_id))


@app.get("/api/technician/onu/options")
async def technician_onu_options(context=Depends(session_context)):
    require_technician(context[1])
    return await cached(context[1], "onu-options", 3600, lambda: context[2].onu_options(), stale_ttl=86400)


@app.post("/api/technician/olts/{olt_id}/authorize")
async def technician_authorize_onu(olt_id: int, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    body = await request.json()
    try:
        order_id = int(body.pop("order_id"))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "OS inválida para vincular o modem.")
    def authorize(client: SGPClient, session: dict[str, Any]):
        order = client.order(order_id, include_onu=True, include_signal_context=False)
        if not order or not technician_owns_order(order, session):
            raise SGPError("Esta OS não pertence ao técnico logado.", 403)
        if order.get("onu"):
            linked = order["onu"]
            requested = str(body.get("id") or "").strip().casefold()
            linked_ids = {str(linked.get("id") or "").strip().casefold(), str(linked.get("phy_addr") or "").strip().casefold()}
            if requested and requested in linked_ids:
                return {"already_linked": True, "onu": linked}
            raise SGPError("Esta OS já possui outro modem vinculado.", 409)
        expected_contract = order.get("contrato_id") or order.get("clientecontrato_id")
        if expected_contract and str(body.get("contrato") or "") != str(expected_contract):
            raise SGPError("O contrato informado não pertence a esta OS.", 400)
        return client.authorize_onu(olt_id, body)
    result = await call_context(context, authorize)
    await invalidate_cached(context[1], f"order-detail-v1:{order_id}")
    return {"ok": True, "result": result}


@app.post("/api/technician/onus/{identifier}/deauthorize")
async def technician_deauthorize_onu(identifier: str, request: Request, context=Depends(session_context)):
    require_technician(context[1])
    body = await request.json()
    try:
        order_id = int(body.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "OS inválida para desvincular o modem.")
    def deauthorize(client: SGPClient, session: dict[str, Any]):
        order = client.order(order_id, include_onu=True, include_signal_context=False)
        if not order or not technician_owns_order(order, session):
            raise SGPError("Esta OS não pertence ao técnico logado.", 403)
        onu = order.get("onu") or {}
        valid = {str(onu.get("id") or ""), str(onu.get("phy_addr") or "")}
        if str(identifier) not in valid:
            raise SGPError("Esta ONU não está vinculada à OS informada.", 409)
        return client.deauthorize_onu(identifier)
    result = await call_context(context, deauthorize)
    await invalidate_cached(context[1], f"order-detail-v1:{order_id}")
    return {"ok": True, "result": result}


@app.get("/api/admin/stock")
async def admin_stock(request: Request, context=Depends(session_context)):
    location = request.query_params.get("location")
    force = request.query_params.get("refresh") == "1"
    if location is None:
        return await cached(
            context[1],
            "stock-locations",
            1800,
            lambda: services._admin_stock_locations(context[2], context[1]),
            force=force,
            stale_ttl=86400,
        )
    try:
        location_id = int(location)
    except ValueError:
        raise HTTPException(400, "Estoque inválido.")
    return await cached(
        context[1],
        f"admin-stock:{location_id}",
        300,
        lambda: services._admin_stock(context[2], context[1], location_id),
        force=force,
        stale_ttl=3600,
    )


def _material_monitor_snapshot(day: date, company_stock: dict[str, Any] | None, audit: dict[str, Any] | None = None) -> dict[str, Any]:
    """One supervisor view from the latest SGP scan and saved reconciliation."""
    saved_reconciliation = cache_snapshot("stock-reconciliation-general-v2", 4 * 86400, 4 * 86400) or {}
    reports = [item.get("detail") for item in saved_reconciliation.get("items", []) if isinstance(item.get("detail"), dict)]
    product_totals: dict[str, float] = {}
    used_since_six = 0.0
    for report in reports:
        for item in report.get("items", []):
            name = str(item.get("product") or "Material não identificado")
            product_totals[name] = product_totals.get(name, 0.0) + float(item.get("current") or 0)
        used_since_six += sum(float(movement.get("quantity") or 0) for movement in report.get("movements", []) if movement.get("flow") == "uses")
    company_items = (company_stock or {}).get("items", [])
    connectors = connector_returns()
    removals = confirmed_removals()
    material_alerts = installation_material_alerts()
    cases = logistics.list_cases()
    today_prefix = day.isoformat()
    connector_usage = (audit or {}).get("connector_usage") or {}
    material_rules = (audit or {}).get("material_rules") or []
    if not isinstance(material_rules, list):
        material_rules = []
    material_rule_alerts = [item for item in material_rules if item.get("status") != "correct"]
    today_connectors = connector_usage.get("connectors") if isinstance(connector_usage, dict) else None
    if not isinstance(today_connectors, list):
        today_connectors = [item for item in connectors if str(item.get("used_at") or "").startswith(today_prefix)]
    return {
        "updated_at": time.time(), "day": day.isoformat(),
        "company_stock": {"location": (company_stock or {}).get("location"), "items": company_items,
                          "total_units": sum(float(item.get("quantity") or 0) for item in company_items)},
        "reconciliation": {"checked_stocks": len(reports), "checked_at": saved_reconciliation.get("observed_at"),
                             "items": [{"product": name, "quantity": quantity} for name, quantity in sorted(product_totals.items(), key=lambda entry: entry[0].casefold())],
                             "used_since_six": used_since_six},
        "today": {"closed_orders": (audit or {}).get("closed_orders"), "connectors": today_connectors,
                  "connector_notes": [item for item in today_connectors if str(item.get("note") or item.get("service_note") or "").strip()],
                  "removals_found": (audit or {}).get("removals_found", len((audit or {}).get("removals", []))),
                  "installations_review": material_alerts,
                  "material_rules": material_rules},
        "pending": {"connector_returns": connectors, "modem_returns": removals,
                    "installation_alerts": material_alerts, "material_rule_alerts": material_rule_alerts,
                    "cases": [item for item in cases if item.get("status") != "resolved"]},
        "events": logistics.audit_history()["items"][:12],
    }


def _refresh_material_monitor(client: SGPClient, session: dict[str, Any]) -> dict[str, Any]:
    """Read SGP once per monitor cache cycle, then persist auditable findings."""
    day = _business_today()
    audit = client.general_daily_material_audit(day)
    connector_usage = client.daily_material_usage_report(day)
    save_confirmed_removals(audit["removals"])
    save_connector_returns(audit["connectors"])
    replace_installation_material_alerts(day, audit["installations"])
    locations = services._admin_stock_locations(client, session)
    company = locations.get("company_stock")
    company_stock = services._admin_stock(client, session, int(company.get("id") or 0)) if company else None
    return {"company_stock": company_stock, "audit": {"closed_orders": audit.get("closed_orders"),
            "removals_found": len(audit.get("removals", [])), "connector_usage": connector_usage,
            "material_rules": audit.get("material_rules", []), "ai_available": material_ai.configured()}, "sgp_updated_at": time.time()}


@app.get("/api/superadmin/material-monitor")
async def material_monitor(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    force = request.query_params.get("refresh") == "1"
    client = central_sgp_client() or context[2]
    retention = 4 * 86400
    latest_key = f"material-monitor-latest-v2:{cache_identity(context[1])}"
    if not force:
        latest = await run_sync(lambda: cache_snapshot(latest_key, None, retention))
        if isinstance(latest, dict) and latest.get("company_stock") is not None:
            report = await run_sync(lambda: _material_monitor_snapshot(
                _business_today(), latest.get("company_stock"), latest.get("audit"),
            ))
            report["sgp_updated_at"] = latest.get("sgp_updated_at")
            report["saved_snapshot"] = True
            report["ai_available"] = bool((latest.get("audit") or {}).get("ai_available"))
            return report
    base = await cached(
        context[1], "material-monitor-v1", 300,
        lambda: _refresh_material_monitor(client, context[1]), force=force,
        stale_ttl=retention, persistent_ttl=retention, allow_stale_on_error=True,
    )
    await run_sync(lambda: save_cache_snapshot(latest_key, base, retention))
    # Local pending items/events are read on every screen refresh. The costly
    # SGP result is also retained as the fast-opening snapshot for four days.
    report = await run_sync(lambda: _material_monitor_snapshot(_business_today(), base.get("company_stock"), base.get("audit")))
    report["sgp_updated_at"] = base.get("sgp_updated_at")
    report["ai_available"] = bool((base.get("audit") or {}).get("ai_available"))
    return report


@app.post("/api/superadmin/material-analysis/{order_id}/ai")
async def material_analysis_ai(order_id: int, request: Request, context=Depends(session_context)):
    """Request an explainable AI review; never write to the SGP from this route."""
    require_superadmin_session(context[1])
    if not material_ai.configured():
        raise HTTPException(503, "A IA de materiais ainda não foi configurada no servidor.")
    try:
        selected_day = datetime.strptime(str(request.query_params.get("date") or _business_today().isoformat()), "%Y-%m-%d").date()
    except ValueError:
        selected_day = _business_today()
    client = central_sgp_client() or context[2]
    def load() -> dict[str, Any]:
        order = client.order(order_id, include_onu=False) or {}
        if not order:
            raise SGPError("OS não encontrada.", 404)
        materials = client.order_materials(order_id)
        system_analysis = _evaluate_material_rules(order, materials, order)
        if not system_analysis.get("expected"):
            system_analysis.update(status="needs_review", reason="Não há regra automática cadastrada para este tipo de OS.")
        onu: dict[str, Any] = {}
        try:
            onu = client.onu_for_service(order.get("servico_id"), order.get("servico_login")) or {}
            if onu:
                onu["signal_context"] = client.onu_signal_context(onu) or {}
        except (SGPError, OSError, TimeoutError, TypeError, ValueError):
            onu = {}
        service_text = _material_rule_service_text(order)
        connector_quantity = float((system_analysis.get("actual") or {}).get("conectores") or 0)
        modem_history: dict[str, Any] = {"available": True, "event": None}
        try:
            summary = {
                "os_id": order_id, "cliente_id": order.get("cliente_id"), "cliente": order.get("cliente"),
                "cliente_cpf": order.get("cliente_cpf") or order.get("cliente_cpfcnpj"),
                "contrato_id": order.get("contrato_id") or order.get("contrato"),
                "os_motivo_descricao": order.get("os_motivo_descricao"), "os_data_finalizacao": order.get("os_data_finalizacao"),
                "os_tecnico_responsavel": order.get("os_tecnico_responsavel"), "usuario_finalizacao": order.get("usuario_finalizacao"),
            }
            events = client.confirmed_removals_for_day(selected_day, [summary])
            event = events[0] if events else None
            modem_history["event"] = ({
                "return_type": event.get("return_type"), "old_serial": event.get("serial"),
                "replacement_serial": event.get("replacement_serial"), "evidence_at": event.get("evidence_at"),
            } if event else None)
        except (SGPError, OSError, TimeoutError, TypeError, ValueError) as error:
            modem_history = {"available": False, "event": None, "message": str(error)}
        safe_order = {
            "os_id": order_id, "service": order.get("os_motivo_descricao") or order.get("os_servicoprestado"),
            "service_performed": order.get("os_servicoprestado"), "plan": order.get("plano") or order.get("servico_descricao"),
            "technician_notes": order.get("os_conteudo") or order.get("os_observacao") or "",
            "signal": (onu.get("signal_context") or {}).get("current"),
        }
        outages = {"total": None, "message": "Quedas indisponíveis."}
        try:
            client_id = order.get("cliente_id")
            contract_id = order.get("contrato_id") or order.get("contrato")
            if str(client_id).isdigit() and str(contract_id).isdigit():
                outages = client.traffic_disconnects(int(client_id), int(contract_id), selected_day - timedelta(days=30), selected_day)
        except (SGPError, OSError, TimeoutError, TypeError, ValueError) as error:
            outages = {"total": None, "message": str(error)}
        return material_ai.review({
            "order": safe_order, "materials": materials, "system_analysis": system_analysis, "modem_history": modem_history,
            "outages": outages, "signal_context": onu.get("signal_context") or {}, "analysis_date": selected_day.isoformat(),
            "return_policy": {
                "connectors_must_return": connector_classification(service_text) == "return" and connector_quantity > 0,
                "connector_quantity": connector_quantity,
            },
        })
    try:
        analysis = await cached(
            context[1], f"material-analysis-ai-v2:{order_id}:{selected_day.isoformat()}", 900, load,
            force=request.query_params.get("refresh") == "1", persistent_ttl=4 * 86400,
        )
    except RuntimeError as error:
        status = 429 if "limite temporário" in str(error).lower() or "cota de crédito" in str(error).lower() else 502
        raise HTTPException(status, str(error)) from error
    return {"analysis": analysis, "generated_at": time.time()}


def _material_analysis_day(client: SGPClient, selected_day: date) -> dict[str, Any]:
    """Build the daily, read-only audit used by the material control screen.

    Keep the source rows from ``material_analysis_for_day`` intact: the UI must
    show every closed OS, including the ones for which there is no automated
    material rule yet.
    """
    report = client.material_analysis_for_day(selected_day)
    report["generated_at"] = time.time()
    report["ai_available"] = material_ai.configured()
    return report


@app.get("/api/superadmin/material-analysis")
async def material_analysis_day(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    raw_day = str(request.query_params.get("date") or "")
    try:
        selected_day = datetime.strptime(raw_day, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Escolha uma data válida para as OS encerradas.")
    if selected_day > _business_today():
        raise HTTPException(400, "A data da análise não pode estar no futuro.")
    force = request.query_params.get("refresh") == "1"
    client = central_sgp_client() or context[2]
    # Historical daily audits are expensive. They remain available for four
    # days and the cache is invalidated by normal logistics changes.
    return await cached(
        context[1], f"material-analysis-day-v1:{selected_day.isoformat()}", 600,
        lambda: _material_analysis_day(client, selected_day), force=force,
        stale_ttl=4 * 86400, persistent_ttl=4 * 86400, allow_stale_on_error=True,
    )


_pon_jobs: set[asyncio.Task] = set()
_pon_health_jobs: set[asyncio.Task] = set()
PON_HEALTH_SNAPSHOT = "pon-health-scan-v1"
PON_HEALTH_BUILD = "pon-health-scan-build-v1"
# The persistent store exempts this snapshot from expiration cleanup.
PON_HEALTH_RETENTION = 30 * 86400


@app.get("/api/office/pon-map-progress")
async def office_pon_map_progress(context=Depends(session_context)):
    from app import pon_areas
    require_office_tools(context[1])
    result = await run_sync(lambda: cache_snapshot("pon-map-build", None, pon_areas.RETENTION))
    if result and result.get("state") == "running" and time.time() - result.get("updated_at", 0) > 180:
        result = {**result, "state": "error", "message": "Geração interrompida ou sem resposta. Consulte novamente para continuar."}
    return result


def _pon_health_row(group: dict[str, Any], result: dict[str, Any] | None = None, error: Exception | None = None) -> dict[str, Any]:
    """Keep the general optical screen compact; detailed ONUs stay on the PON page."""
    reference = (result or {}).get("reference") or {}
    summary = (result or {}).get("pon_summary") or {}
    return {
        "olt_id": group.get("olt_id"),
        "olt": reference.get("olt_name") or group.get("olt") or group.get("olt_name") or str(group.get("olt_id") or "OLT"),
        "slot": group.get("slot"), "pon": group.get("pon"),
        "total": int(summary.get("total") or 0), "measured": int(summary.get("measured") or 0),
        "good": int(summary.get("good") or 0), "limit": int(summary.get("limit") or 0),
        "bad": int(summary.get("bad") or 0), "no_signal": int(summary.get("no_signal") or 0),
        "bad_percent": float(summary.get("bad_percent") or 0),
        "status": (result or {}).get("pon_status") or ("unavailable" if error else "not_pon"),
        "diagnosis": (result or {}).get("diagnosis") or ("Sem leitura disponível" if error else "PON dentro do esperado"),
        "checked_at": (result or {}).get("checked_at"),
        "error": str(error)[:220] if error else None,
    }


async def _scan_pon_health(client, token: str, olt_ids: list[int] | None = None) -> None:
    """Scan serially to protect the SGP session and retain useful partial results."""
    from app import pon_areas
    cancelled = threading.Event()
    pulse = None
    try:
        source = await run_sync(lambda: cache_snapshot(pon_areas.SNAPSHOT, None, pon_areas.RETENTION)) or {}
        groups = sorted(
            [item for item in source.get("groups") or [] if item.get("olt_id") not in (None, "") and item.get("slot") not in (None, "") and item.get("pon") not in (None, "")],
            key=lambda item: (str(item.get("olt") or ""), int(item.get("slot") or 0), int(item.get("pon") or 0)),
        )
        if olt_ids:
            groups = [group for group in groups if int(group["olt_id"]) in olt_ids]
        if not groups:
            raise RuntimeError("Ainda não há PONs salvas no Mapa PON. Consulte o mapa primeiro.")
        previous = await run_sync(lambda: cache_snapshot(PON_HEALTH_SNAPSHOT, None, PON_HEALTH_RETENTION)) or {}
        by_key = {
            (str(item.get("olt_id")), str(item.get("slot")), str(item.get("pon"))): item
            for item in previous.get("results") or []
        }
        # Never blank the operational screen during an update. Existing cards
        # remain visible and are replaced one by one as the new SGP readings
        # arrive.
        payload: dict[str, Any] = {
            "generated_at": previous.get("generated_at"),
            "results": list(by_key.values()), "total": len(groups),
        }
        await run_sync(lambda: save_cache_snapshot(PON_HEALTH_SNAPSHOT, payload, PON_HEALTH_RETENTION))

        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                try:
                    if not await store.maintain_owned(PON_HEALTH_BUILD, token, 120):
                        cancelled.set(); return
                except Exception:
                    cancelled.set(); return

        pulse = asyncio.create_task(heartbeat())
        for index, group in enumerate(groups, start=1):
            if cancelled.is_set():
                raise RuntimeError("Consulta interrompida. Os resultados já carregados foram preservados.")
            try:
                result = await run_sync(lambda group=group: client.pon_port_analysis(int(group["olt_id"]), int(group["slot"]), int(group["pon"])))
                row = _pon_health_row(group, result=result)
            except Exception as error:
                # One unavailable PON must not abort the operational overview.
                row = _pon_health_row(group, error=error)
            row_key = (str(row.get("olt_id")), str(row.get("slot")), str(row.get("pon")))
            if row.get("error") and row_key in by_key:
                row = {**by_key[row_key], "error": "Falha ao atualizar; última leitura preservada: " + row["error"]}
            by_key[row_key] = row
            payload["results"] = list(by_key.values())
            payload["updated_at"] = time.time()
            await run_sync(lambda payload=copy.deepcopy(payload): save_cache_snapshot(PON_HEALTH_SNAPSHOT, payload, PON_HEALTH_RETENTION))
            await run_sync(lambda index=index: save_cache_snapshot(PON_HEALTH_BUILD, {
                "state": "running", "stage": "Consultando sinais das PONs", "done": index, "total": len(groups),
                "updated_at": time.time(),
            }, PON_HEALTH_RETENTION))
            await asyncio.sleep(.12)
        payload["generated_at"] = time.time()
        await run_sync(lambda: save_cache_snapshot(PON_HEALTH_SNAPSHOT, payload, PON_HEALTH_RETENTION))
        await run_sync(lambda: save_cache_snapshot(PON_HEALTH_BUILD, {"state": "complete", "done": len(groups), "total": len(groups), "updated_at": time.time()}, PON_HEALTH_RETENTION))
    except Exception as error:
        await run_sync(lambda: save_cache_snapshot(PON_HEALTH_BUILD, {"state": "error", "message": str(error), "updated_at": time.time()}, PON_HEALTH_RETENTION))
    finally:
        cancelled.set()
        if pulse:
            pulse.cancel()
        await store.maintain_owned(PON_HEALTH_BUILD, token)


@app.get("/api/office/pon-health")
async def office_pon_health(context=Depends(session_context)):
    from app import pon_areas
    require_office_tools(context[1])
    result = await run_sync(lambda: cache_snapshot(PON_HEALTH_SNAPSHOT, None, PON_HEALTH_RETENTION)) or {"results": [], "total": 0}
    result["results"] = list({(str(row.get("olt_id")), str(row.get("slot")), str(row.get("pon"))): row for row in result.get("results", [])}.values())
    build = await run_sync(lambda: cache_snapshot(PON_HEALTH_BUILD, None, PON_HEALTH_RETENTION))
    if build and build.get("state") == "running" and time.time() - build.get("updated_at", 0) > 600:
        build = {**build, "state": "error", "message": "Consulta interrompida. Resultados salvos preservados; clique em Atualizar consulta para tentar novamente."}
    inventory = await run_sync(lambda: cache_snapshot(pon_areas.SNAPSHOT, None, pon_areas.RETENTION)) or {}
    return {**result, "build": build, "olts": inventory.get("olts", [])}


@app.post("/api/superadmin/pon-health/scan")
async def superadmin_scan_pon_health(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json() if await request.body() else {}
    try:
        olt_ids = sorted({int(value) for value in body.get("olt_ids", [])})
        if any(value <= 0 for value in olt_ids):
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(400, "Selecione OLTs válidas.")
    token = secrets.token_urlsafe(24)
    if not await store.acquire_owned(PON_HEALTH_BUILD, token, 120):
        build = await run_sync(lambda: cache_snapshot(PON_HEALTH_BUILD, None, PON_HEALTH_RETENTION))
        return {"started": False, "build": build}
    await run_sync(lambda: save_cache_snapshot(PON_HEALTH_BUILD, {
        "state": "running", "stage": "Preparando consulta geral de PONs", "done": 0, "total": 0, "updated_at": time.time(),
    }, PON_HEALTH_RETENTION))
    task = asyncio.create_task(_scan_pon_health(central_sgp_client() or context[2], token, olt_ids))
    _pon_health_jobs.add(task)
    task.add_done_callback(_pon_health_jobs.discard)
    return {"started": True}


async def _generate_pon_map(client, token):
    from app import pon_areas
    cancelled = threading.Event()
    last_progress = [0.0]
    def progress(stage, done, total):
        if cancelled.is_set():
            raise RuntimeError("Geração interrompida. Consulte novamente para continuar pelos dados salvos.")
        if time.monotonic() - last_progress[0] < 2 and done != total:
            return
        last_progress[0] = time.monotonic()
        save_cache_snapshot("pon-map-build", {"state": "running", "stage": stage, "done": done, "total": total,
                                           "updated_at": time.time()}, pon_areas.RETENTION)
    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            try:
                owned = await store.maintain_owned("pon-map-build", token, 120)
            except Exception:
                cancelled.set()
                return
            if not owned:
                cancelled.set()
                return
            status = await run_sync(lambda: cache_snapshot("pon-map-build", None, pon_areas.RETENTION))
            if status and status.get("state") == "running":
                await run_sync(lambda: save_cache_snapshot("pon-map-build", {**status, "updated_at": time.time()}, pon_areas.RETENTION))
    pulse = asyncio.create_task(heartbeat())
    try:
        await asyncio.to_thread(pon_areas.generate, client, progress)
        await run_sync(lambda: save_cache_snapshot("pon-map-build", {"state": "complete", "updated_at": time.time()}, pon_areas.RETENTION))
    except Exception as error:
        await run_sync(lambda: save_cache_snapshot("pon-map-build", {"state": "error", "message": str(error), "updated_at": time.time()}, pon_areas.RETENTION))
    finally:
        cancelled.set()
        pulse.cancel()
        await store.maintain_owned("pon-map-build", token)


@app.get("/api/office/service-coverage-map")
async def office_service_coverage_map(request: Request, context=Depends(session_context)):
    from app import pon_areas
    require_office_tools(context[1])
    force = request.query_params.get("refresh") == "1"
    if force:
        require_superadmin_session(context[1])
        token = secrets.token_urlsafe(24)
        if await store.acquire_owned("pon-map-build", token, 120):
            await run_sync(lambda: save_cache_snapshot("pon-map-build", {"state": "running", "stage": "Iniciando consulta de todas as PONs", "done": 0, "total": 0, "updated_at": time.time()}, pon_areas.RETENTION))
            task = asyncio.create_task(_generate_pon_map(context[2], token))
            _pon_jobs.add(task)
            task.add_done_callback(_pon_jobs.discard)
    result = await run_sync(lambda: cache_snapshot(pon_areas.SNAPSHOT, None, pon_areas.RETENTION))
    if result is None:
        result = await run_sync(lambda: cache_snapshot("shared:pon-map-v2", None, pon_areas.RETENTION))
    build = await run_sync(lambda: cache_snapshot("pon-map-build", None, pon_areas.RETENTION))
    if build and build.get("state") == "running" and time.time() - build.get("updated_at", 0) > 180:
        build = {**build, "state": "error", "message": "Geração interrompida. Consulte novamente para continuar."}
    manual_areas = await run_sync(coverage_pon_areas)
    return {**(result or {"clients": [], "groups": [], "olts": [], "total": 0}), "build": build,
            "buildings": [], "manual_areas": manual_areas}


@app.post("/api/superadmin/coverage-pon-areas")
async def superadmin_save_coverage_pon_area(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    try:
        area_id = int(body["id"]) if body.get("id") not in (None, "") else None
        return await run_sync(lambda: save_coverage_pon_area(
            area_id, body.get("olt_id"), body.get("olt_name"), body.get("slot"), body.get("pon"),
            body.get("note"), body.get("geometry"), session_username(context[1]),
        ))
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@app.delete("/api/superadmin/coverage-pon-areas/{area_id}", status_code=204)
async def superadmin_delete_coverage_pon_area(area_id: int, context=Depends(session_context)):
    require_superadmin_session(context[1])
    await run_sync(lambda: delete_coverage_pon_area(area_id))


@app.post("/api/superadmin/coverage-pon-areas/generate")
async def superadmin_generate_coverage_pon_areas(context=Depends(session_context)):
    from app import pon_areas
    require_superadmin_session(context[1])
    snapshot = await run_sync(lambda: cache_snapshot(pon_areas.SNAPSHOT, None, pon_areas.RETENTION))
    if not snapshot:
        raise HTTPException(409, "Atualize o inventário do Mapa PON antes de gerar as áreas.")
    existing = await run_sync(coverage_pon_areas)
    occupied = {(str(item.get("olt_id") or ""), str(item.get("slot")), str(item.get("pon"))) for item in existing}
    suggestions = await run_sync(lambda: pon_areas.suggested_manual_areas(snapshot))
    actor = session_username(context[1])
    saved, skipped = [], []
    for item in suggestions:
        key = (str(item["olt_id"]), str(item["slot"]), str(item["pon"]))
        if key in occupied:
            continue
        try:
            area = await run_sync(lambda item=item: save_coverage_pon_area(
                None, item["olt_id"], item["olt_name"], item["slot"], item["pon"], item["note"], item["geometry"], actor,
            ))
            saved.append({**area, "clients_used": item["clients_used"], "outliers_ignored": item["outliers_ignored"]})
        except Exception as error:
            # One invalid geometry or a transient write must never discard the
            # complete supervisor batch.  Keep enough context for the UI and
            # application log to show which PON needs a manual drawing.
            logging.getLogger(__name__).warning("Could not save generated PON area %s", key, exc_info=True)
            skipped.append({"olt_id": key[0], "slot": key[1], "pon": key[2], "reason": str(error)[:180]})
    return {"saved": saved, "created": len(saved), "preserved": len(existing), "skipped": skipped,
            "total": len(existing) + len(saved)}


@app.post("/api/superadmin/coverage-pon-areas/generate-selected")
async def superadmin_generate_selected_coverage_pon_area(request: Request, context=Depends(session_context)):
    """Create one *draft* area only for the supervisor-selected PON.

    The first automatic contour is deliberately isolated from the bulk action:
    it lets the supervisor visually validate the customer cluster before any
    other PON is persisted.  Existing manual work is never overwritten.
    """
    from app import pon_areas
    require_superadmin_session(context[1])
    body = await request.json()
    olt_id, slot, pon = (str(body.get(key) or "").strip() for key in ("olt_id", "slot", "pon"))
    if not olt_id or not slot or not pon:
        raise HTTPException(400, "Selecione OLT, Slot e PON antes de gerar a área.")
    snapshot = await run_sync(lambda: cache_snapshot(pon_areas.SNAPSHOT, None, pon_areas.RETENTION))
    if not snapshot:
        raise HTTPException(409, "Atualize o inventário do Mapa PON antes de gerar a área.")
    existing = await run_sync(coverage_pon_areas)
    key = (olt_id, slot, pon)
    saved_area = next((item for item in existing
                       if (str(item.get("olt_id") or ""), str(item.get("slot")), str(item.get("pon"))) == key), None)
    if saved_area:
        # The action is safe to repeat.  Returning the persisted shape lets the
        # browser recover from a refresh or a delayed click without asking the
        # supervisor to delete a valid area just to see it again.
        return {**saved_area, "already_saved": True, "clients_used": 0, "outliers_ignored": 0}
    suggestion = next((item for item in await run_sync(lambda: pon_areas.suggested_manual_areas(snapshot))
                       if (str(item["olt_id"]), str(item["slot"]), str(item["pon"])) == key), None)
    if not suggestion:
        raise HTTPException(422, "Não há coordenadas válidas suficientes nesta PON para sugerir uma área.")
    try:
        saved = await run_sync(lambda: save_coverage_pon_area(
            None, suggestion["olt_id"], suggestion["olt_name"], suggestion["slot"], suggestion["pon"],
            suggestion["note"], suggestion["geometry"], session_username(context[1]),
        ))
    except (TypeError, ValueError) as error:
        raise HTTPException(422, str(error)) from error
    return {**saved, "clients_used": suggestion["clients_used"], "outliers_ignored": suggestion["outliers_ignored"]}


@app.post("/api/superadmin/coverage-buildings")
async def superadmin_save_coverage_building(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    try:
        building_id = int(body["id"]) if body.get("id") not in (None, "") else None
        return await run_sync(lambda: save_coverage_building(
            building_id, body.get("name"), body.get("note"),
            float(body.get("latitude")), float(body.get("longitude")), session_username(context[1]),
        ))
    except (TypeError, ValueError) as error:
        raise HTTPException(400, str(error)) from error


@app.delete("/api/superadmin/coverage-buildings/{building_id}", status_code=204)
async def superadmin_delete_coverage_building(building_id: int, context=Depends(session_context)):
    require_superadmin_session(context[1])
    await run_sync(lambda: delete_coverage_building(building_id))


@app.get("/api/superadmin/material-analysis/{order_id}")
async def material_analysis_detail(order_id: int, context=Depends(session_context)):
    """Read a compact customer/OS context for a supervisor review card."""
    require_superadmin_session(context[1])
    client = central_sgp_client() or context[2]

    def load() -> dict[str, Any]:
        # A partial technical lookup (ONU/signal) must not prevent the
        # supervisor from seeing an OS and its stock launch.
        try:
            order = client.order(order_id, include_onu=False) or {}
        except (SGPError, OSError, TimeoutError, TypeError, ValueError) as error:
            # A malformed/temporarily unavailable SGP detail response must
            # not turn the supervisor's card click into a dead end.
            order = {"os_id": order_id, "detail_warning": str(error)}
        warnings = [order["detail_warning"]] if order.get("detail_warning") else []
        try:
            materials = client.order_materials(order_id)
        except (SGPError, OSError, TimeoutError, TypeError, ValueError) as error:
            materials = []
            warnings.append(f"Materiais não puderam ser consultados: {error}")
        onu: dict[str, Any] = {}
        try:
            found_onu = client.onu_for_service(order.get("servico_id"), order.get("servico_login"))
            if found_onu:
                onu = found_onu
                onu["signal_context"] = client.onu_signal_context(onu) or {}
        except (SGPError, OSError, TimeoutError, TypeError, ValueError):
            onu = {}
        client_id = order.get("cliente_id") or order.get("cliente")
        contract_id = order.get("contrato_id") or order.get("contrato")
        outages: dict[str, Any] = {"total": None, "items": [], "message": "Dados de quedas não disponíveis."}
        try:
            if str(client_id).isdigit() and str(contract_id).isdigit():
                outages = client.traffic_disconnects(int(client_id), int(contract_id), _business_today() - timedelta(days=30), _business_today())
        except (SGPError, OSError, TimeoutError, TypeError, ValueError) as error:
            outages = {"total": None, "items": [], "message": str(error)}
        return {
            "order": {
                "id": order_id, "client": order.get("cliente") or "Cliente não informado",
                "contract_id": contract_id, "technician": order.get("os_tecnico_responsavel") or "Não identificado",
                "service": order.get("os_motivo_descricao") or order.get("os_servicoprestado") or "Serviço não informado",
                "performed": order.get("os_servicoprestado") or "", "content": order.get("os_conteudo") or order.get("os_observacao") or "",
                "closed_at": order.get("os_data_finalizacao"), "address": order.get("endereco") or order.get("contrato_endereco") or "",
                "plan": order.get("plano") or order.get("servico_descricao") or "Não informado",
                "detail_warning": order.get("detail_warning") or "",
            },
            "materials": materials,
            "warnings": warnings,
            "signal": (onu.get("signal_context") or {}) if onu else {},
            "onu": {"gpon": onu.get("gpon") or onu.get("serial") or onu.get("onu_id"), "rx": onu.get("info_rx")} if onu else {},
            "outages": outages,
        }
    return await cached(context[1], f"material-analysis-detail-v2:{order_id}", 900, load,
                        stale_ttl=4 * 86400, persistent_ttl=4 * 86400, allow_stale_on_error=True)


@app.get("/api/superadmin/launched-modems")
async def launched_modems(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    force = request.query_params.get("refresh") == "1"
    location_value = str(request.query_params.get("location", "")).strip().casefold()
    try:
        location_id = 0 if location_value == "all" else int(location_value)
        selected_day = datetime.strptime(request.query_params.get("date", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(400, "Escolha o estoque e uma data válida.")
    if selected_day > _business_today():
        raise HTTPException(400, "A data não pode estar no futuro.")
    client = central_sgp_client() or context[2]
    if location_id == 0:
        location_name = "Todos os técnicos"
    else:
        locations = await run_sync(client.stock_locations)
        location = next((item for item in locations if int(item.get("id") or 0) == location_id), None)
        if not location:
            raise HTTPException(404, "Estoque não encontrado no SGP.")
        location_name = str(location.get("descricao") or f"Estoque {location_id}")
    return await cached(
        context[1], f"launched-modems-v2:{location_id}:{selected_day.isoformat()}", 900,
        lambda: client.launched_modems_report(location_id, location_name, selected_day),
        force=force, stale_ttl=86400, persistent_ttl=86400, allow_stale_on_error=False,
    )


@app.get("/api/superadmin/modem-exchanges")
async def modem_exchanges(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    force = request.query_params.get("refresh") == "1"
    try:
        selected_day = datetime.strptime(request.query_params.get("date", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(400, "Escolha uma data válida.")
    if selected_day > _business_today():
        raise HTTPException(400, "A data não pode estar no futuro.")
    client = central_sgp_client() or context[2]
    return await cached(
        context[1], f"modem-exchanges-v2:{selected_day.isoformat()}", 900,
        lambda: client.modem_exchanges_report(selected_day),
        force=force, stale_ttl=86400, persistent_ttl=86400,
    )

def load_modem_purchase_options(client: SGPClient) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        locations_future = pool.submit(client.stock_locations)
        suppliers_future = pool.submit(client.stock_suppliers)
        companies_future = pool.submit(client.stock_companies)
        products_future = pool.submit(client.products)
        locations = locations_future.result()
        suppliers = suppliers_future.result()
        companies = companies_future.result()
        products = products_future.result()
    company_stock = next(
        (item for item in locations if "empresa" in str(item.get("descricao") or "").casefold()), None,
    )
    default_supplier = next(
        (item for item in suppliers if "ebf" in str(item.get("nome") or "").casefold()), None,
    )
    company = next(
        (item for item in companies if "eb fox" in str(item.get("nome") or item.get("razaosocial") or "").casefold()), None,
    )
    products = [item for item in products if _is_modem_product(item)]
    if not company_stock:
        raise SGPError("O estoque Empresa não foi localizado.", 404)
    if not default_supplier:
        raise SGPError("Nenhum fornecedor foi localizado no SGP.", 404)
    if not company:
        raise SGPError("A empresa EB FOX não foi localizada no SGP.", 404)
    return {
        "stock": company_stock, "supplier": default_supplier, "company": company,
        "products": sorted(products, key=lambda item: str(item.get("descricao") or "").casefold()),
    }


def purchase_customer_conflict(error: SGPError) -> tuple[str, str] | None:
    """Read the serial and customer named by SGP's duplicate purchase error."""
    match = re.search(
        r'refer.ncia\s+["\']?([A-Z0-9]{8,32})["\']?.*?Cliente:\s*([^"\']+)',
        str(error), re.I | re.S,
    )
    return (match.group(1).upper(), match.group(2).strip()) if match else None


@app.get("/api/superadmin/material-usage")
async def material_usage(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    period = request.query_params.get("period", "day")
    if period not in {"day", "month"}:
        raise HTTPException(400, "Período inválido.")
    try:
        selected = datetime.strptime(request.query_params.get("date", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(400, "Escolha uma data válida.")
    today = _business_today()
    if selected > today:
        raise HTTPException(400, "A data não pode estar no futuro.")
    start = selected.replace(day=1) if period == "month" else selected
    end = min((start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1), today) if period == "month" else selected
    client = central_sgp_client() or context[2]

    def load_report():
        connectors = client.daily_material_usage_report(start, end)
        return {"period": period, "start": start.isoformat(), "end": end.isoformat(),
                "connectors": connectors}

    return await cached(
        context[1], f"material-usage-v4:{period}:{start.isoformat()}:{end.isoformat()}", 900,
        load_report, force=request.query_params.get("refresh") == "1",
        stale_ttl=86400, persistent_ttl=86400, allow_stale_on_error=False,
    )


@app.get("/api/superadmin/inventory")
async def inventory_report(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    raw_location = request.query_params.get("location", "").strip()
    try:
        location_id = int(raw_location) if raw_location else None
    except ValueError:
        raise HTTPException(400, "Selecione um estoque válido.")
    if location_id is not None and location_id <= 0:
        raise HTTPException(400, "Selecione um estoque válido.")
    client = central_sgp_client() or context[2]
    locations = await cached(context[1], "inventory-locations-v1", 3600, client.stock_locations)
    report = await cached(
        context[1], f"inventory-available-v2:{location_id or 'all'}", 300,
        lambda: client.available_stock_report(location_id),
        force=request.query_params.get("refresh") == "1", stale_ttl=3600,
        persistent_ttl=86400, allow_stale_on_error=False,
    )
    return {**report, "locations": locations}


async def reconcile_stock_location(
    client: SGPClient, location: dict[str, Any], observed_at: float | None = None,
) -> dict[str, Any]:
    """Read one location from 06:00 and preserve a balance snapshot for the next check."""
    location_id = int(location.get("id") or 0)
    if location_id <= 0:
        raise SGPError("Estoque inválido.", 400)
    # A general query must not give later stock reads the first stock's timestamp.
    observed_at = time.time()
    query_revision = await run_sync(logistics.revision)
    zone = ZoneInfo("America/Sao_Paulo")
    observed_local = datetime.fromtimestamp(observed_at, zone)
    day_start_local = observed_local.replace(hour=6, minute=0, second=0, microsecond=0)
    if observed_local < day_start_local:
        day_start_local -= timedelta(days=1)
    day_start_at = day_start_local.timestamp()
    previous = await run_sync(lambda: stock_balance_snapshot_before(location_id, day_start_at))
    current = await run_sync(lambda: client.available_stock_report(location_id))
    read_started_at = observed_at
    observed_at = time.time()
    observed_local = datetime.fromtimestamp(observed_at, zone)
    result: dict[str, Any] = {"location": location.get("descricao"), "observed_at": observed_at,
                              "day_start_at": day_start_at,
                              "current_total": sum(float(item.get("quantity") or 0) for item in current["items"])}
    if previous and day_start_at - previous["captured_at"] <= 30 * 86400:
        start = datetime.fromtimestamp(previous["captured_at"], zone).replace(tzinfo=None)
        end = datetime.fromtimestamp(observed_at, zone).replace(tzinfo=None)
        movements = await run_sync(lambda: client.stock_movements_between(location_id, start, end))
        name = str(location.get("descricao") or "")
        result.update(reconcile_stock_day(name, previous["items"], current["items"], movements,
                                          day_start_local.replace(tzinfo=None)))
        result["baseline_at"] = day_start_at
        result["source_snapshot_at"] = previous["captured_at"]
        result["opening_source"] = "unknown" if result.get("opening_unknown") else "snapshot"
    else:
        start = day_start_local.replace(tzinfo=None) - timedelta(microseconds=1)
        end = observed_local.replace(tzinfo=None)
        movements = await run_sync(lambda: client.stock_movements_between(location_id, start, end))
        result.update(stock_day_with_calculated_opening(str(location.get("descricao") or ""), current["items"], movements))
        result["baseline_at"] = day_start_at
        result["opening_source"] = "calculated" if result.get("opening_calculated") else "unknown"
    result["movements"] = [movement for movement in movements
                           if movement.get("flow") and
                           datetime.fromisoformat(movement["at_iso"]) >= day_start_local.replace(tzinfo=None)]
    result["movement_count"] = len(result["movements"])
    # SGP has no atomic balance+ledger endpoint. Do not certify a snapshot that
    # changed while its pages were being read, or carry it into tomorrow.
    concurrent = [item for item in movements if datetime.fromisoformat(item["at_iso"]) >=
                  datetime.fromtimestamp(read_started_at, zone).replace(tzinfo=None)]
    if concurrent:
        result.update(incomplete=True, balanced=None,
                      message="Houve movimentação durante a leitura do saldo. Consulte novamente para conferir.")
    result["revision"] = query_revision
    result["outdated"] = query_revision != await run_sync(logistics.revision)
    if result["outdated"]:
        result.update(incomplete=True, balanced=None, message="O estoque foi alterado durante a consulta. Consulte novamente.")
    for item in result.get("items", []):
        if item.get("difference") not in (None, 0) and not result.get("incomplete"):
            await run_sync(lambda item=item: logistics.alert(
                f"balance:{location_id}:{day_start_at}:{item['product']}:{item['difference']}",
                f"Saldo divergente · {str(location.get('descricao') or '')[:100]}",
                f"Material: {item['product']}. Esperado: {item['expected']}; atual: {item['current']}; diferença: {item['difference']}. Consulte Conferência de saldo antes de corrigir."))
    compact_balance = [{"description": item.get("description"), "quantity": item.get("quantity")}
                       for item in current["items"]]
    if not concurrent and not result["outdated"]:
        await run_sync(lambda: save_stock_balance_snapshot(location_id, compact_balance, observed_at))
    return result


@app.get("/api/superadmin/stock-reconciliation")
async def stock_reconciliation(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    try:
        location_id = int(request.query_params.get("location", ""))
    except (TypeError, ValueError):
        raise HTTPException(400, "Selecione um estoque.")
    if location_id <= 0:
        raise HTTPException(400, "Selecione um estoque.")
    cache_key = f"stock-reconciliation-detail-v2:{location_id}"
    if request.query_params.get("saved") == "1":
        saved = await run_sync(lambda: cache_snapshot(cache_key, 4 * 86400, 4 * 86400))
        if saved:
            saved["outdated"] = saved.get("revision") != await run_sync(logistics.revision)
            return saved
    client = central_sgp_client() or context[2]
    locations = await run_sync(client.stock_locations)
    location = next((item for item in locations if int(item.get("id") or 0) == location_id), None)
    if not location:
        raise HTTPException(404, "Estoque não encontrado no SGP.")
    report = await reconcile_stock_location(client, location)
    await run_sync(lambda: save_cache_snapshot(cache_key, report, 4 * 86400))
    return report


@app.get("/api/superadmin/stock-reconciliation/general")
async def stock_reconciliation_general(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    if request.query_params.get("saved") == "1":
        saved = await run_sync(lambda: cache_snapshot("stock-reconciliation-general-v2", 4 * 86400, 4 * 86400))
        if saved:
            saved["outdated"] = saved.get("revision") != await run_sync(logistics.revision)
            return saved
        raise HTTPException(404, "Ainda não existe consulta geral salva.")
    client = central_sgp_client() or context[2]
    locations = await run_sync(client.stock_locations)
    observed_at = time.time()
    query_revision = await run_sync(logistics.revision)
    semaphore = asyncio.Semaphore(3)

    async def inspect(location: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                report = await reconcile_stock_location(client, location, observed_at)
                await run_sync(lambda: save_cache_snapshot(
                    f"stock-reconciliation-detail-v2:{int(location.get('id') or 0)}", report, 4 * 86400,
                ))
                return {
                    "id": int(location.get("id") or 0), "location": report.get("location"),
                    "current_total": report.get("current_total", 0),
                    "movement_count": report.get("movement_count", 0),
                    "balanced": report.get("balanced"), "opening_unknown": report.get("opening_unknown", False),
                    "opening_calculated": report.get("opening_calculated", False),
                    "incomplete": report.get("incomplete", False),
                    "differences": sum(1 for item in report.get("items", []) if item.get("difference") not in (None, 0)),
                    "pending": len(report.get("unmatched_movements", [])),
                    # The general consultation already loaded this report. Keep it
                    # with the saved summary so the UI can render every stock
                    # without reopening (or querying) one technician at a time.
                    "detail": report,
                }
            except SGPError as error:
                return {"id": int(location.get("id") or 0), "location": location.get("descricao"), "error": str(error)}
            except Exception:
                return {"id": int(location.get("id") or 0), "location": location.get("descricao"), "error": "Não foi possível concluir esta consulta."}

    reports = await asyncio.gather(*(inspect(location) for location in locations))
    result = {"observed_at": observed_at, "total": len(reports), "items": reports,
              "revision": query_revision, "outdated": query_revision != await run_sync(logistics.revision)}
    await run_sync(lambda: save_cache_snapshot("stock-reconciliation-general-v2", result, 4 * 86400))
    return result


@app.get("/api/superadmin/inventory/onu")
async def inventory_onu_status(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    gpon = str(request.query_params.get("gpon") or "").strip()
    if not gpon:
        raise HTTPException(400, "Informe o GPON da referência.")
    client = central_sgp_client() or context[2]
    return await cached(
        context[1], f"inventory-onu-v1:{client.onu_search_serial(gpon)}", 120,
        lambda: client.stock_onu_status(gpon),
        force=request.query_params.get("refresh") == "1", stale_ttl=600,
        persistent_ttl=86400,
    )


@app.get("/api/superadmin/modem-purchase/options")
async def modem_purchase_options(context=Depends(session_context)):
    require_superadmin_session(context[1])
    client = central_sgp_client() or context[2]
    return await cached(
        context[1], "modem-purchase-options-v5", 3600,
        lambda: load_modem_purchase_options(client), stale_ttl=86400,
    )


@app.post("/api/superadmin/modem-purchase")
async def modem_purchase(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    try:
        product_id = int(body.get("product_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Selecione o modelo do modem.")
    raw_gpons = body.get("gpons") if isinstance(body.get("gpons"), list) else [body.get("gpon")]
    gpons = [
        "".join(character for character in str(value or "").upper() if character.isalnum())
        for value in raw_gpons
        if str(value or "").strip()
    ]
    mac = "".join(character for character in str(body.get("mac") or "").upper() if character in "0123456789ABCDEF")
    photo_hash = str(body.get("photo_hash") or "").lower()
    if not gpons or len(gpons) > 50 or len(set(gpons)) != len(gpons):
        raise HTTPException(400, "Informe de 1 a 50 GPONs diferentes.")
    if any(not (8 <= len(gpon) <= 24 and any(character.isalpha() for character in gpon) and any(character.isdigit() for character in gpon)) for gpon in gpons):
        raise HTTPException(400, "Confira os GPONs informados.")
    if mac and len(mac) != 12:
        raise HTTPException(400, "Confira o MAC lido na foto. Ele deve possuir 12 caracteres hexadecimais.")
    if photo_hash and (len(photo_hash) != 64 or any(character not in "0123456789abcdef" for character in photo_hash)):
        raise HTTPException(400, "A identificação da foto da etiqueta é inválida.")
    local_references = {gpon: equipment_reference(gpon, mac if len(gpons) == 1 else "") for gpon in gpons}
    if mac and local_references[gpons[0]] and str(local_references[gpons[0]].get("gpon") or "").upper() != gpons[0]:
        raise HTTPException(409, "Este MAC já pertence a outro GPON comprado pelo EBF Técnico.")
    options = await cached(
        context[1], "modem-purchase-options-v5", 3600,
        lambda: load_modem_purchase_options(central_sgp_client() or context[2]), stale_ttl=86400,
    )
    valid_product_ids = {int(item.get("id") or 0) for item in options.get("products", [])}
    if product_id not in valid_product_ids:
        raise HTTPException(400, "Selecione um modelo de modem válido.")

    def purchase(client: SGPClient, session: dict[str, Any]):
        # A purchase must use the authenticated supervisor's SGP credentials.
        # The central traffic credential is intentionally read-only for stock
        # writes and may not have the purchase permission granted to Gustavo.
        stock, supplier, company = options["stock"], options["supplier"], options["company"]
        stock_id = int(stock["id"])
        # Read-only checks are independent; finish every check before any
        # return or purchase. Limit concurrency to protect the SGP.
        with ThreadPoolExecutor(max_workers=min(6, 2 * len(gpons))) as pool:
            location_checks = {gpon: pool.submit(client.modem_reference_location, gpon) for gpon in gpons}
            onu_checks = {gpon: pool.submit(client.stock_onu_status, gpon) for gpon in gpons}
            preflight = {gpon: (location_checks[gpon].result(), onu_checks[gpon].result()) for gpon in gpons}
        if len(gpons) > 1:
            new_gpons = []
            returns = []
            already_company = []
            for current_gpon in gpons:
                location, onu = preflight[current_gpon]
                if location.get("kind") == "existing_unknown":
                    raise SGPError(f"O GPON {current_gpon} possui histórico sem origem confirmada. Nenhuma alteração foi feita.", 409)
                if location.get("kind") == "stock" and int(location.get("stock_id") or 0) == stock_id:
                    already_company.append(current_gpon)
                    continue
                if onu["installed"] and location.get("kind") != "client":
                    raise SGPError(f"O GPON {current_gpon} está cadastrado como ONU, mas o cliente atual não foi confirmado. Nenhuma alteração foi feita.", 409)
                if location.get("kind") in ("client", "stock"):
                    if location.get("kind") == "client" and not location.get("launch_item_id") and not location.get("loan_id"):
                        raise SGPError(f"O lançamento ou comodato do GPON {current_gpon} não foi localizado. Nenhuma alteração foi feita.", 409)
                    if location.get("kind") == "client" and str(location.get("movement_type") or "").casefold() == "os":
                        if not location.get("client_id") or not client.client_os_launch_confirmed(int(location["client_id"]), int(location["launch_item_id"]), current_gpon):
                            raise SGPError(f"O GPON {current_gpon} não foi confirmado na OS do cliente. Nenhuma alteração foi feita.", 409)
                    returns.append((current_gpon, location))
                elif local_references[current_gpon]:
                    raise SGPError(f"O GPON {current_gpon} já foi comprado anteriormente, mas não foi localizado no SGP. Nenhuma alteração foi feita.", 409)
                else:
                    new_gpons.append(current_gpon)
            if returns and body.get("confirm_return") is not True:
                detail = ", ".join(f"{gpon} ({location.get('client') or location.get('stock_name') or 'origem identificada'})" for gpon, location in returns)
                raise SGPError(f"Modens para devolver ao estoque Empresa: {detail}. Confirme o retorno; os outros {len(new_gpons)} serão comprados.", 409)
            returned = []
            purchased = []
            saved = []
            try:
                for current_gpon, location in returns:
                    client.return_modem_to_company(location, stock_id, current_gpon)
                    returned.append(current_gpon)
                while new_gpons:
                    try:
                        result = client.purchase_modems(
                            int(supplier["id"]), int(company["id"]), stock_id,
                            product_id, new_gpons, validate_catalog=False, check_duplicates=False,
                            prefer_admin_form=bool(session.get("modem_purchase_admin_form")),
                        )
                        if isinstance(result, dict) and result.get("via") == "admin-form":
                            session["modem_purchase_admin_form"] = True
                    except SGPError as error:
                        conflict = purchase_customer_conflict(error)
                        if not conflict or conflict[0] not in new_gpons:
                            raise
                        current_gpon, customer_name = conflict
                        location = client.locate_customer_modem(current_gpon, customer_name)
                        if body.get("confirm_return") is not True:
                            raise SGPError(
                                f"O GPON {current_gpon} está com o cliente {customer_name}. "
                                "Confirme o retorno ao estoque Empresa antes de comprar os demais.", 409,
                            ) from error
                        client.return_modem_to_company(location, stock_id, current_gpon)
                        returned.append(current_gpon)
                        new_gpons.remove(current_gpon)
                        continue
                    purchased = new_gpons[:]
                    purchase_id = str((result or {}).get("id") or (result or {}).get("compra_id") or "") if isinstance(result, dict) else ""
                    saved = [save_equipment_reference(
                        current_gpon, None, product_id, stock_id, purchase_id,
                        photo_hash, str(session["user"].get("usuario") or "Superadmin"),
                    ) for current_gpon in new_gpons]
                    break
            except SGPError as error:
                if returned or purchased:
                    raise SGPError(f"Operação parcial: devolvidos {', '.join(returned) or 'nenhum'}; comprados {', '.join(purchased) or 'nenhum'}. Falha: {error}", error.status) from error
                raise
            return {"returned": bool(returned), "returned_gpons": returned,
                    "already_company_gpons": already_company,
                    "purchased_gpons": purchased, "references": saved, "stock": stock,
                    "count": len(gpons)}
        gpon = gpons[0]
        local_reference = local_references[gpon]
        location, onu_status = preflight[gpon]
        if location.get("kind") == "existing_unknown":
            raise SGPError(
                "Este GPON possui histórico no SGP, mas a origem atual não foi identificada. "
                "Nenhuma compra foi criada; confira o comodato antes de continuar.",
                409,
            )
        if location.get("kind") == "stock" and int(location.get("stock_id") or 0) == stock_id:
            raise SGPError("O modem já está no saldo da Empresa!", 409)
        if onu_status["installed"] and location.get("kind") != "client":
            raise SGPError(
                f"O GPON {gpon} está cadastrado como ONU, mas o lançamento ativo no cliente não foi identificado. "
                "Nenhuma compra ou devolução foi feita.", 409,
            )
        if location.get("kind") in ("stock", "client"):
            if body.get("confirm_return") is not True:
                origin = (
                    f"cliente {location.get('client') or 'não identificado'} "
                    f"(cadastro #{location.get('client_id') or '?'}, contrato #{location.get('contract_id') or '?'}, "
                    f"lançamento #{location.get('launch_item_id') or '?'})"
                    if location.get("kind") == "client"
                    else f"estoque {location.get('stock_name') or 'não identificado'}"
                )
                raise SGPError(
                    f"Este GPON já pertence ao {origin}. A ação será devolver para o estoque Empresa, sem criar nova compra. Confirme o retorno.",
                    409,
                )
            if location.get("kind") == "client" and not location.get("loan_id") and (not location.get("launch_item_id") or not location.get("client_id")):
                raise SGPError(
                    "O lançamento exato e o cadastro do cliente não foram ambos confirmados. "
                    "Nenhuma devolução foi feita; confira os dois cadastros no SGP.", 409,
                )
            if location.get("kind") == "client" and str(location.get("movement_type") or "").casefold() == "os":
                if not client.client_os_launch_confirmed(int(location["client_id"]), int(location["launch_item_id"]), gpon):
                    raise SGPError(
                        "O GPON não foi confirmado nos lançamentos da OS deste cadastro. "
                        "Nenhuma devolução foi feita.", 409,
                    )
            result = client.return_modem_to_company(
                location, stock_id, gpon,
            )
            return {
                "returned": True, "return_result": result,
                "location": location, "stock": stock,
            }
        if local_reference:
            raise SGPError(
                "Este GPON já foi comprado anteriormente, mas não foi localizado no saldo atual do SGP. "
                "Nenhuma compra duplicada foi criada.",
                409,
            )
        try:
            result = client.purchase_modem(
                int(supplier["id"]), int(company["id"]), stock_id,
                product_id, gpon, mac, validate_catalog=False, check_duplicates=False,
                prefer_admin_form=bool(session.get("modem_purchase_admin_form")),
            )
            if isinstance(result, dict) and result.get("via") == "admin-form":
                session["modem_purchase_admin_form"] = True
        except SGPError as error:
            conflict = purchase_customer_conflict(error)
            if not conflict or conflict[0] != gpon:
                raise
            location = client.locate_customer_modem(gpon, conflict[1])
            if body.get("confirm_return") is not True:
                raise SGPError(
                    f"O GPON {gpon} está com o cliente {conflict[1]}. "
                    "Confirme o retorno ao estoque Empresa.", 409,
                ) from error
            return_result = client.return_modem_to_company(location, stock_id, gpon)
            return {"returned": True, "return_result": return_result,
                    "location": location, "stock": stock}
        purchase_id = str((result or {}).get("id") or (result or {}).get("compra_id") or "") if isinstance(result, dict) else ""
        saved = save_equipment_reference(
            gpon, mac or None, product_id, stock_id, purchase_id,
            photo_hash, str(session["user"].get("usuario") or "Superadmin"),
        )
        return {"purchase": result, "reference": saved, "stock": stock}

    actor = logistics_actor(context[1])
    source = f"purchase:{time.time_ns()}"
    await run_sync(lambda: logistics.audit(source, actor, {"action": "Compra/devolução solicitada", "gpons": gpons}))
    try:
        result = await call_context(context, purchase)
    except Exception:
        await logistics_result(source, actor, {"action": "Operação não confirmada pelo app; conferir o SGP antes de repetir"})
        raise
    else:
        await logistics_result(source, actor, {"action": "Compra/devolução concluída", "gpons": gpons})
    finally:
        # Includes partial batches and timeouts: never reuse pre-operation balances.
        await stock_mutation_finished()
    if "returned_gpons" in result:
        notice = (f"{len(result['returned_gpons'])} modem(ns) devolvido(s), {len(result['purchased_gpons'])} comprado(s)"
                  f" e {len(result['already_company_gpons'])} já no saldo da Empresa.")
    else:
        notice = (f"Modem {gpons[0]} estornado e devolvido ao estoque Empresa." if result.get("returned")
                  else f"{len(gpons)} modem(ns) comprado(s) e lançado(s) no estoque Empresa.")
    return {
        "ok": True,
        "notice": notice,
        **result,
    }


@app.get("/api/admin/transfer")
async def transfer_options(request: Request, context=Depends(session_context)):
    try:
        origin = int(request.query_params["origin"])
        destination = int(request.query_params["destination"])
    except (KeyError, ValueError):
        raise HTTPException(400, "Informe os estoques de origem e destino.")
    return await cached(
        context[1], f"transfer-options-v1:{origin}:{destination}", 120,
        lambda: services._admin_transfer_options(context[2], context[1], origin, destination),
        stale_ttl=900, persistent_ttl=3600,
    )


@app.get("/api/logistics/cases")
async def logistics_cases(context=Depends(session_context)):
    actor = logistics_actor(context[1])
    supervisor = context[1]["user"]["mode"] == "superadmin"
    return {"items": await run_sync(lambda: logistics.list_cases(None if supervisor else actor))}


@app.get("/api/logistics/audit")
async def logistics_audit(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    try:
        offset = max(0, int(request.query_params.get("offset", "0")))
    except ValueError:
        raise HTTPException(400, "Página inválida.")
    return await run_sync(lambda: logistics.audit_history(offset))


@app.post("/api/logistics/cases")
async def logistics_create(request: Request, context=Depends(session_context)):
    actor = logistics_actor(context[1])
    body = await request.json()
    subject = str(body.get("subject") or "").strip()
    message = str(body.get("message") or "").strip()
    if not 3 <= len(subject) <= 160 or not 5 <= len(message) <= 2000:
        raise HTTPException(400, "Informe o material/OS e descreva a divergência (até 2.000 caracteres).")
    identifier = await run_sync(lambda: logistics.create(actor, actor, subject, message))
    return {"id": identifier, "notice": "Divergência registrada para análise. Nenhum saldo foi alterado."}


@app.post("/api/logistics/cases/{identifier}")
async def logistics_respond(identifier: str, request: Request, context=Depends(session_context)):
    actor = logistics_actor(context[1])
    body = await request.json()
    message = str(body.get("message") or "").strip()
    if not 5 <= len(message) <= 2000:
        raise HTTPException(400, "Descreva a análise ou justificativa (5 a 2.000 caracteres).")
    try:
        await run_sync(lambda: logistics.respond(identifier, actor, message, str(body.get("status") or "review"),
                                                context[1]["user"]["mode"] == "superadmin"))
    except LookupError as error:
        raise HTTPException(404, str(error))
    except ValueError as error:
        raise HTTPException(400, str(error))
    return {"ok": True, "notice": "Histórico atualizado. A resposta não altera o saldo no SGP."}


@app.post("/api/admin/transfer")
async def transfer(request: Request, context=Depends(session_context)):
    body = await request.json()
    require_superadmin_session(context[1])
    actor = logistics_actor(context[1])
    source = f"transfer:{time.time_ns()}"
    await run_sync(lambda: logistics.audit(source, actor, {"action": "Transferência solicitada", "request": body}))
    try:
        result = await call_context(
            context,
            lambda client, session: services._admin_transfer(client, session, body),
        )
    except Exception:
        await logistics_result(source, actor, {"action": "Transferência não confirmada; conferir o SGP"})
        raise
    else:
        await logistics_result(source, actor, {"action": "Transferência processada", "result": result})
    finally:
        await stock_mutation_finished()
    await invalidate_cached(context[1], "")
    return result


@app.get("/api/superadmin/scores")
async def scores(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    force = request.query_params.get("refresh") == "1"
    technician = str(request.query_params.get("technician") or "").strip()[:160]
    general = request.query_params.get("general") == "1"
    if general:
        technician = ""
    month = str(request.query_params.get("month") or "").strip()[:7]
    if not technician and not general:
        raise HTTPException(400, "Escolha o técnico que deseja consultar.")
    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(400, "Escolha um mês válido.")
    def load_scores():
        report = services._superadmin_scores(context[2], context[1], technician, month)
        if general:
            technicians = report.setdefault("technicians", [])
            for user in platform_users():
                if user.get("role") != "technician":
                    continue
                name = str(user.get("name") or user.get("username") or "").strip()
                if name and not any(_same_person(item.get("name"), alias)
                                    for item in technicians for alias in (name, user.get("username")) if alias):
                    technicians.append({"name": name, "closed_orders": 0, "gross_points": 0,
                                        "points": 0, "types": [], "history": [],
                                        "repeated": {"count": 0, "lost_points": 0, "refunded_points": 0, "details": []}})
            technicians.sort(key=lambda item: (-float(item.get("points") or 0), item["name"].casefold()))
        return report
    return await cached(
        context[1],
        f"scores-v7:{technician.casefold() if not general else '__all__'}:{month or 'current'}",
        900,
        load_scores,
        force=force,
        stale_ttl=86400,
    )


@app.post("/api/superadmin/scores/refund")
async def refund_score(request: Request, context=Depends(session_context)):
    body = await request.json()
    requested = body.get("items") if isinstance(body.get("items"), list) else []
    technician = str((requested[0] if requested else {}).get("technician") or "").strip()
    report_technician = str(body.get("report_technician") or technician).strip()
    month = str(body.get("month") or "").strip()
    cached_report = None
    if report_technician:
        report_key = f"cache:data:{cache_identity(context[1])}:scores-v5:{report_technician.casefold()}:{month or 'current'}"
        cached_report = await store.get(report_key)
    result = await call_context(
        context,
        lambda client, session: services._refund_repeat_score(client, session, body, cached_report),
    )
    await store.delete_prefix("cache:data:")
    return result


@app.get("/api/superadmin/users")
async def superadmin_users(context=Depends(session_context)):
    require_superadmin_session(context[1])
    users, members, popup_members = await asyncio.gather(
        run_sync(platform_users), run_sync(route_monitor_members), run_sync(popup_access_members),
    )
    for user in users:
        user["route_monitor"] = any(
            _same_person(user.get("username"), member.get("username"))
            or _same_person(user.get("name"), member.get("display_name"))
            for member in members
        )
        user["popup_enabled"] = any(_same_person(user.get("username"), member.get("username")) for member in popup_members)
    return {"users": users}


@app.get("/api/superadmin/route-monitor")
async def route_monitor(request: Request, context=Depends(session_context)):
    """Load today's route once and summarize it for supervisors."""
    require_office_tools(context[1])
    sao_paulo_hour = datetime.now(ZoneInfo("America/Sao_Paulo")).hour
    automatic_window = 8 <= sao_paulo_hour < 19
    force = request.query_params.get("refresh") == "1" and automatic_window
    member_rows = await run_sync(route_monitor_members)
    technicians = tuple(str(item.get("display_name") or item.get("username") or "").strip() for item in member_rows)
    technicians = tuple(name for name in technicians if name)
    def load_route():
        orders = context[2].route_monitor_orders()
        service_day = _business_today().isoformat()
        cache_day = f"{service_day}:v2"
        candidates = [
            int(order.get("os_id") or 0) for order in orders
            if int(order.get("os_status") or 0) in (1, 2)
            and any(_same_person(order.get("os_tecnico_responsavel"), name) for name in technicians)
        ]
        saved = route_service_times(cache_day, candidates)
        candidate_ids = set(candidates)
        # OS encerradas podem reutilizar o inicio salvo. Uma OS em execucao
        # precisa ser conferida novamente, pois pode ter passado por Pendente
        # e iniciado uma nova sessao depois do primeiro registro em cache.
        refresh = [
            order for order in orders
            if int(order.get("os_id") or 0) in candidate_ids
            and (
                int(order.get("os_status") or 0) == 2
                or int(order.get("os_id") or 0) not in saved
            )
        ]
        if refresh:
            found = context[2].route_monitor_start_times(refresh, technicians=technicians)
            save_route_service_times(cache_day, found)
            saved.update({order_id: started_at for order_id, (_client_id, started_at) in found.items()})
        for order in orders:
            started_at = saved.get(int(order.get("os_id") or 0))
            if started_at:
                order["checkin_data"] = started_at
        result = build_route_monitor(orders, technicians=technicians)
        result["map_points"] = context[2].route_monitor_positions(orders, technicians=technicians)
        return result

    result = await cached(
        context[1], "route-monitor-v2:" + hashlib.sha1("|".join(technicians).encode()).hexdigest()[:10], 45 if automatic_window else 86400,
        load_route,
        force=force, stale_ttl=180,
    )
    service_day = str(result.get("date") or _business_today().isoformat())
    reviews = await run_sync(lambda: route_alert_reviews(service_day))
    review_index = {(int(item["os_id"]), str(item["technician"]).casefold()): item for item in reviews}
    response = {**result, "alerts": [dict(item) for item in result.get("alerts", [])]}
    for alert in response["alerts"]:
        review = review_index.get((int(alert.get("id") or 0), str(alert.get("technician") or "").casefold()))
        alert["review"] = review
    response["reviewed_alerts"] = len([item for item in response["alerts"] if item.get("review")])
    return response


@app.post("/api/superadmin/route-monitor/reviews")
async def save_route_review(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    body = await request.json()
    try:
        order_id = int(body.get("order_id") or 0)
    except (TypeError, ValueError):
        order_id = 0
    technician = str(body.get("technician") or "").strip()
    comment = str(body.get("comment") or "").strip()
    if order_id <= 0 or not technician:
        raise HTTPException(400, "Informe a OS e o técnico.")
    if not comment:
        raise HTTPException(400, "O comentário da análise é obrigatório.")
    reviewer = str((context[1].get("user") or {}).get("nome") or (context[1].get("user") or {}).get("usuario") or "Supervisor")
    try:
        return await run_sync(lambda: save_route_alert_review(_business_today().isoformat(), order_id, technician, comment, reviewer))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def removal_payload(items: list[dict[str, Any]], added: int = 0) -> dict[str, Any]:
    by_technician: dict[str, int] = {}
    today = _business_today().isoformat()
    for item in items:
        name = str(item.get("technician") or "Não identificado")
        by_technician[name] = by_technician.get(name, 0) + 1
    return {
        "items": items,
        "total": len(items),
        "today": sum(str(item.get("evidence_at") or "").startswith(today) for item in items),
        "added": added,
        "retention_days": 14,
        "technicians": [
            {"name": name, "count": count}
            for name, count in sorted(by_technician.items(), key=lambda value: (-value[1], value[0].casefold()))
        ],
    }


def removal_payday(timestamp: float) -> datetime:
    zone = ZoneInfo("America/Sao_Paulo")
    delivered = datetime.fromtimestamp(float(timestamp), zone)
    days_until_friday = (4 - delivered.weekday()) % 7
    payday = datetime.combine((delivered + timedelta(days=days_until_friday)).date(), clock_time(18, 0), zone)
    if delivered >= payday + timedelta(minutes=1):
        payday += timedelta(days=7)
    return payday


def removal_payment_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    withdrawal_value = 30.0
    periods: dict[str, dict[str, Any]] = {}
    for item in items:
        delivered_at = float(item.get("dismissed_at") or 0)
        delivered = datetime.fromtimestamp(delivered_at, ZoneInfo("America/Sao_Paulo"))
        payday = removal_payday(delivered_at)
        key = payday.isoformat()
        period = periods.setdefault(key, {
            "payday": key,
            "starts_at": (payday - timedelta(days=7) + timedelta(minutes=1)).isoformat(),
            "ends_at": payday.isoformat(), "count": 0, "amount": 0.0,
            "technicians": {}, "items": [],
        })
        technician = str(item.get("technician") or "Não identificado")
        return_type = str(item.get("return_type") or "retirada").casefold()
        value = withdrawal_value if return_type == "retirada" else 0.0
        tech = period["technicians"].setdefault(technician, {"name": technician, "count": 0, "amount": 0.0})
        tech["count"] += 1
        tech["amount"] += value
        period["count"] += 1
        period["amount"] += value
        period["items"].append({**item, "delivered_at": delivered.isoformat(), "value": value})
    result = []
    for period in periods.values():
        period["technicians"] = sorted(period["technicians"].values(), key=lambda row: row["name"].casefold())
        result.append(period)
    return {"periods": sorted(result, key=lambda row: row["payday"], reverse=True), "unit_value": withdrawal_value, "retention_days": 14}


def technician_payment(items: list[dict[str, Any]], aliases: list[str], now: float | None = None) -> dict[str, Any]:
    payday = removal_payday(now or time.time())
    selected = [
        item for item in items
        if any(alias and _same_person(alias, item.get("technician")) for alias in aliases)
        and removal_payday(float(item.get("dismissed_at") or 0)) == payday
    ]
    paid = [item for item in selected if str(item.get("return_type") or "retirada").casefold() == "retirada"]
    return {"count": len(paid), "amount": len(paid) * 30.0, "closes_at": payday.isoformat(), "unit_value": 30.0}


@app.get("/api/superadmin/removals")
async def removal_ledger(context=Depends(session_context)):
    require_superadmin_session(context[1])
    # Also migrates manual entries created before the unified ledger existed.
    await promote_manual_modems(await run_sync(return_obligations))
    return removal_payload(await run_sync(confirmed_removals))


@app.post("/api/superadmin/removals/sync-sheet")
async def sync_removals_to_sheet(context=Depends(session_context)):
    require_superadmin_session(context[1])
    if not SHEETS_SYNC_SECRET or not SHEETS_WEBHOOK_URL:
        raise HTTPException(503, "Configure a URL e a chave da planilha no Render.")
    pending, delivered, connectors = await asyncio.gather(
        run_sync(confirmed_removals), run_sync(delivered_removals), run_sync(connector_returns),
    )
    rows = (
        [removal_sheet_row(item) for item in pending]
        + [removal_sheet_row(item, True) for item in delivered]
    )
    removals_synced = await run_sync(lambda: send_sheet_sync("upsert_many", rows)) if rows else True
    connectors_synced = await run_sync(send_connector_summary)
    synced = removals_synced and connectors_synced
    if not synced:
        raise HTTPException(502, "A planilha não confirmou a sincronização.")
    return {"ok": True, "total": len(rows), "notice": f"{len(rows)} aparelho(s) sincronizado(s) com a planilha."}


@app.get("/api/superadmin/returns/summary")
async def return_material_summary(context=Depends(session_context)):
    """Return every saved return obligation without consulting the SGP."""
    require_superadmin_session(context[1])
    removals, connectors, manual, users = await asyncio.gather(
        run_sync(confirmed_removals), run_sync(connector_returns), run_sync(return_obligations), run_sync(platform_users),
    )
    def canonical_name(value: Any) -> str:
        raw = str(value or "Não identificado")
        match = next((
            user for user in users
            if _same_person(raw, user.get("name")) or _same_person(raw, user.get("username"))
        ), None)
        return str((match or {}).get("name") or raw).replace("_", " ").strip()
    removals = [{**item, "technician": canonical_name(item.get("technician"))} for item in removals]
    connectors = [{**item, "technician": canonical_name(item.get("technician"))} for item in connectors]
    manual = [{**item, "technician": canonical_name(item.get("technician"))} for item in manual]
    return {"removals": removals, "connectors": connectors, "manual": manual}


@app.get("/api/superadmin/removals/payments")
async def removal_payments(context=Depends(session_context)):
    require_superadmin_session(context[1])
    return removal_payment_payload(await run_sync(delivered_removals))


@app.post("/api/superadmin/removals/payments")
async def add_removal_payment(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    technician = str(body.get("technician") or "").strip()[:180]
    serial = re.sub(r"[^A-Za-z0-9._ -]", "", str(body.get("serial") or "").strip())[:80]
    try:
        quantity = int(body.get("quantity") or 0)
        delivered = datetime.fromisoformat(str(body.get("delivered_at") or ""))
        if delivered.tzinfo is None:
            delivered = delivered.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
        delivered_at = delivered.timestamp()
    except (TypeError, ValueError):
        raise HTTPException(400, "Informe uma data e hora válidas.")
    now = time.time()
    if not technician or not 1 <= quantity <= 100:
        raise HTTPException(400, "Selecione o técnico e informe de 1 a 100 retiradas.")
    if delivered_at < now - (14 * 86400) or delivered_at > now + 300:
        raise HTTPException(400, "A entrega deve estar dentro dos últimos 14 dias.")
    added = await run_sync(lambda: add_manual_removal_payments(technician, quantity, delivered_at, serial))
    result = removal_payment_payload(await run_sync(delivered_removals))
    result["notice"] = f"{added} retirada(s) adicionada(s) à planilha."
    return result


@app.delete("/api/superadmin/removals/payments")
async def delete_removal_payments(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    try:
        order_ids = list(dict.fromkeys(int(value) for value in (body.get("order_ids") or [])))
    except (TypeError, ValueError):
        raise HTTPException(400, "Selecione somente retiradas válidas.")
    if not order_ids or len(order_ids) > 200:
        raise HTTPException(400, "Selecione de 1 a 200 retiradas.")
    removed = await run_sync(lambda: exclude_removal_payments(order_ids))
    result = removal_payment_payload(await run_sync(delivered_removals))
    result["notice"] = f"{removed} retirada(s) excluída(s) da contagem."
    return result


@app.get("/api/removals/mine")
async def my_removal_count(context=Depends(session_context)):
    user = context[1].get("user") or {}
    aliases = [str(user.get("nome") or "").strip(), str(user.get("usuario") or "").strip()]
    items = await run_sync(confirmed_removals)
    own = [
        item for item in items
        if any(alias and _same_person(alias, item.get("technician")) for alias in aliases)
    ]
    manual = next((
        item for item in await run_sync(return_obligations)
        if any(alias and _same_person(alias, item.get("technician")) for alias in aliases)
    ), None)
    own_serials = {
        re.sub(r"[^A-Z0-9]", "", str(item.get("serial") or "").upper())
        for item in own
    }
    manual_only_modems = [
        modem for modem in (manual or {}).get("modems") or []
        if re.sub(
            r"[^A-Z0-9]", "",
            str((modem.get("gpon") if isinstance(modem, dict) else modem) or "").upper(),
        ) not in own_serials
    ]
    visible_manual = {
        **(manual or {"connectors": 0, "modems": [], "note": ""}),
        "modems": manual_only_modems,
    }
    own_connectors = [
        item for item in await run_sync(connector_returns)
        if any(alias and _same_person(alias, item.get("technician")) for alias in aliases)
    ]
    own_delivered = [
        item for item in await run_sync(delivered_removals)
        if any(alias and _same_person(alias, item.get("technician")) for alias in aliases)
    ]
    return {
        "count": len(own),
        "total_modems": len(own) + len(manual_only_modems),
        "technician": aliases[0] or aliases[1],
        "modems": [
            {
                "gpon": str(item.get("serial") or "Não identificado"),
                "os_id": item.get("os_id"),
                "client": item.get("client"),
                "unlinked_at": item.get("evidence_at"),
                "return_type": item.get("return_type") or "retirada",
            }
            for item in own
        ],
        "manual": visible_manual,
        "connector_returns": own_connectors,
        "connector_total": round(sum(float(item.get("quantity") or 0) for item in own_connectors), 2),
        "payment": technician_payment(own_delivered, aliases),
        "message": (
            f"Você tem {len(own) + len(manual_only_modems)} modem(ns) e {round(sum(float(item.get('quantity') or 0) for item in own_connectors) + int((manual or {}).get('connectors') or 0), 2)} conector(es) para entregar."
            if own or manual or own_connectors else "Você não possui materiais pendentes para entregar."
        ),
    }


def connector_return_payload(items: list[dict[str, Any]], added: int = 0) -> dict[str, Any]:
    totals: dict[str, float] = {}
    for item in items:
        name = str(item.get("technician") or "Não identificado")
        totals[name] = totals.get(name, 0) + float(item.get("quantity") or 0)
    return {"items": items, "added": added, "total": round(sum(totals.values()), 2), "technicians": [
        {"name": name, "quantity": round(quantity, 2)} for name, quantity in sorted(totals.items())
    ]}


@app.get("/api/superadmin/connector-returns")
async def list_connector_returns(context=Depends(session_context)):
    require_superadmin_session(context[1])
    return connector_return_payload(await run_sync(connector_returns))


@app.post("/api/superadmin/connector-returns/scan")
async def scan_connector_returns(context=Depends(session_context)):
    require_superadmin_session(context[1])
    lock_key = "operation:connector-return-scan"
    if not await store.acquire(lock_key, 180):
        raise HTTPException(409, "A consulta de conectores já está em andamento.")
    try:
        found = await call_context(context, lambda client, session: client.connector_returns_for_day(_business_today()))
        added = await run_sync(lambda: save_connector_returns(found))
        sheet_synced = await run_sync(send_connector_summary)
    finally:
        await store.release(lock_key)
    result = connector_return_payload(await run_sync(connector_returns), added)
    result["notice"] = f"{added} novo(s) lançamento(s) de conectores adicionado(s)."
    result["sheet_synced"] = sheet_synced
    return result


@app.post("/api/superadmin/connector-returns/sync-sheet")
async def sync_connectors_to_sheet(context=Depends(session_context)):
    require_superadmin_session(context[1])
    if not SHEETS_SYNC_SECRET or not SHEETS_WEBHOOK_URL:
        raise HTTPException(503, "Configure a URL e a chave da planilha no Render.")
    items = await run_sync(connector_returns)
    rows = connector_summary_rows(items)
    synced = await run_sync(send_connector_summary)
    if not synced:
        raise HTTPException(502, "A planilha não confirmou a sincronização dos conectores.")
    return {"ok": True, "total": len(rows), "notice": f"{len(rows)} lançamento(s) de conectores atualizado(s) na planilha."}


@app.post("/api/superadmin/connector-returns/update")
async def edit_connector_return(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    source_id = str(body.get("source_id") or "").strip()[:160]
    note = str(body.get("note") or "").strip()[:500]
    try: quantity = float(body.get("quantity"))
    except (TypeError, ValueError): raise HTTPException(400, "Informe uma quantidade válida.")
    if not source_id or not 0 <= quantity <= 1000: raise HTTPException(400, "Confira a pendência selecionada.")
    before = next((item for item in await run_sync(connector_returns) if str(item.get("source_id")) == source_id), None)
    await run_sync(lambda: logistics.audit(f"connector:{source_id}", logistics_actor(context[1]),
        {"action": "correction_requested", "before": before, "quantity": quantity, "reason": note}))
    if quantity == 0:
        changed = await run_sync(lambda: dismiss_connector_returns([source_id]))
    else:
        changed = await run_sync(lambda: update_connector_return(source_id, quantity, note))
    sheet_synced = await run_sync(send_connector_summary) if changed else False
    await logistics_result(f"connector:{source_id}", logistics_actor(context[1]),
        {"action": "Resultado da correção", "changed": bool(changed), "sheet_synced": sheet_synced})
    result = connector_return_payload(await run_sync(connector_returns))
    result["notice"] = "Pendência atualizada." if changed else "A pendência não foi localizada."
    result["sheet_synced"] = sheet_synced
    return result


@app.post("/api/superadmin/connector-returns/dismiss")
async def remove_connector_returns(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    source_ids = [str(value) for value in (body.get("source_ids") or []) if str(value).strip()]
    if not source_ids or len(source_ids) > 200: raise HTTPException(400, "Selecione os conectores entregues.")
    await run_sync(lambda: logistics.audit(f"connector-delivery:{time.time_ns()}", logistics_actor(context[1]),
        {"action": "Recebimento físico de conectores confirmado pelo supervisor", "source_ids": source_ids}))
    removed = await run_sync(lambda: dismiss_connector_returns(source_ids))
    sheet_synced = await run_sync(send_connector_summary) if removed else False
    result = connector_return_payload(await run_sync(connector_returns))
    result["notice"] = f"{removed} pendência(s) removida(s)."
    result["sheet_synced"] = sheet_synced
    return result


@app.get("/api/superadmin/return-obligations")
async def list_return_obligations(context=Depends(session_context)):
    require_superadmin_session(context[1])
    return {"items": await run_sync(return_obligations)}


@app.post("/api/superadmin/return-obligations")
async def upsert_return_obligation(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    technician = str(body.get("technician") or "").strip()[:160]
    note = str(body.get("note") or "").strip()[:500]
    try:
        connectors = int(body.get("connectors") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Informe uma quantidade válida de conectores.")
    raw_modems = body.get("modems") if isinstance(body.get("modems"), list) else []
    modems = []
    for value in raw_modems:
        source = value if isinstance(value, dict) else {"gpon": value, "type": "troca"}
        gpon = "".join(character for character in str(source.get("gpon") or "").upper() if character.isalnum())
        kind = str(source.get("type") or "troca").casefold()
        modems.append({"gpon": gpon, "type": kind})
    if connectors > 1000 or len(modems) > 100 or any(not 8 <= len(value["gpon"]) <= 32 or value["type"] not in ("troca", "retirada") for value in modems):
        raise HTTPException(400, "Confira as quantidades e numerações informadas.")
    try:
        saved = await run_sync(lambda: save_return_obligation(
            technician, connectors, modems, note, str(context[1]["user"].get("usuario") or "Supervisor"),
        ))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    promoted, sheet_synced = await promote_manual_modems([saved])
    notice = f"Materiais de {saved['technician']} salvos."
    if promoted:
        notice += f" {len(promoted)} modem(ns) adicionado(s) à lista geral"
        notice += " e à planilha." if sheet_synced else "; a planilha não confirmou o envio."
    return {
        "notice": notice, "item": saved, "items": await run_sync(return_obligations),
        "promoted_modems": len(promoted), "sheet_synced": sheet_synced,
    }


@app.delete("/api/superadmin/return-obligations")
async def remove_return_obligation(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    technician = str(request.query_params.get("technician") or "").strip()[:160]
    if not technician:
        raise HTTPException(400, "Informe o técnico que realizou a entrega.")
    removed = await run_sync(lambda: delete_return_obligation(technician))
    return {"removed": removed, "notice": "Entrega confirmada e materiais removidos da pendência.", "items": await run_sync(return_obligations)}


@app.post("/api/superadmin/removals/scan")
async def scan_removals(context=Depends(session_context)):
    require_superadmin_session(context[1])
    lock_key = "operation:removal-scan"
    if not await store.acquire(lock_key, 180):
        raise HTTPException(409, "Uma nova contagem já está em andamento. Aguarde a conclusão.")
    try:
        found = await call_context(
            context, lambda client, session: client.confirmed_removals_for_day(_business_today()),
        )
        added = await run_sync(lambda: save_confirmed_removals(found))
        sheet_rows = [removal_sheet_row(item) for item in found]
        try:
            sheet_synced = await run_sync(lambda: send_sheet_sync("upsert_many", sheet_rows)) if sheet_rows else True
        except (OSError, TimeoutError, ValueError):
            # O resultado já está salvo no banco. Uma lentidão do Google não
            # deve transformar a consulta do SGP em erro 500.
            sheet_synced = False
    finally:
        await store.release(lock_key)
    items = await run_sync(confirmed_removals)
    result = removal_payload(items, added)
    result["notice"] = (
        f"{added} nova(s) retirada(s) confirmada(s) e adicionada(s)."
        if added else "Nenhuma retirada nova foi encontrada. As já salvas não foram duplicadas."
    )
    result["checked"] = len(found)
    result["sheet_synced"] = sheet_synced
    if not sheet_synced:
        result["sheet_notice"] = "A consulta foi salva no app, mas a planilha demorou para responder. Use o botão de atualização depois."
    return result


@app.get("/api/superadmin/installations-without-materials")
async def list_installations_without_materials(context=Depends(session_context)):
    require_superadmin_session(context[1])
    items = await run_sync(installation_material_alerts)
    return {"items": items, "total": len(items), "preloaded": True}


@app.delete("/api/superadmin/installations-without-materials")
async def delete_installations_without_materials(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    try:
        order_ids = list(dict.fromkeys(int(value) for value in (body.get("order_ids") or [])))
    except (TypeError, ValueError):
        raise HTTPException(400, "Selecione somente ordens válidas.")
    if not order_ids or len(order_ids) > 200 or any(value <= 0 for value in order_ids):
        raise HTTPException(400, "Selecione de 1 a 200 ordens.")
    before = [item for item in await run_sync(installation_material_alerts) if int(item.get("os_id") or 0) in order_ids]
    await run_sync(lambda: logistics.audit(f"installation-alert:{time.time_ns()}", logistics_actor(context[1]),
        {"action": "Alerta de instalação validado pelo supervisor", "before": before, "order_ids": order_ids}))
    removed = await run_sync(lambda: dismiss_installation_material_alerts(order_ids))
    items = await run_sync(installation_material_alerts)
    return {"items": items, "total": len(items), "removed": removed, "notice": f"{removed} instalação(ões) removida(s) da lista."}


@app.post("/api/superadmin/material-audit/scan")
async def scan_all_daily_materials(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        selected_day = datetime.strptime(str(body.get("date") or _business_today().isoformat()), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Escolha uma data válida.")
    today = _business_today()
    if selected_day > today:
        raise HTTPException(400, "Não é possível consultar uma data futura.")
    if selected_day < today - timedelta(days=13):
        raise HTTPException(400, "Escolha uma data dentro dos últimos 14 dias.")
    lock_key = f"operation:general-material-audit:{selected_day.isoformat()}"
    if not await store.acquire(lock_key, 300):
        raise HTTPException(409, "A consulta desta data já está em andamento.")
    try:
        audit = await call_context(context, lambda client, _session: client.general_daily_material_audit(selected_day))
        removal_added = await run_sync(lambda: save_confirmed_removals(audit["removals"]))
        connector_added = await run_sync(lambda: save_connector_returns(audit["connectors"]))
        installation_total = await run_sync(lambda: replace_installation_material_alerts(selected_day, audit["installations"]))
        for item in audit["installations"]:
            await run_sync(lambda item=item: logistics.alert(
                f"installation:{item['os_id']}:{selected_day.isoformat()}",
                f"Conferir materiais · OS {item['os_id']}",
                f"{item.get('technician') or 'Técnico não identificado'} · {item.get('client') or ''} · {item.get('reason') or 'Conferir materiais'}"))
        sheet_rows = [removal_sheet_row(item) for item in audit["removals"]]
        removals_synced = await run_sync(lambda: send_sheet_sync("upsert_many", sheet_rows)) if sheet_rows else True
        sheet_synced = removals_synced and await run_sync(send_connector_summary)
    finally:
        await store.release(lock_key)
    return {
        "closed_orders": audit["closed_orders"], "removals_found": len(audit["removals"]),
        "removals_added": removal_added, "connectors_found": len(audit["connectors"]),
        "connectors_added": connector_added, "installations_without_materials": installation_total,
        "selected_date": selected_day.isoformat(),
        "sheet_synced": sheet_synced,
        "notice": (
            f"Consulta de {selected_day.strftime('%d/%m/%Y')} concluída. "
            f"Foram adicionados {removal_added} modem(ns) e {connector_added} lançamento(s) novo(s); registros já existentes não foram duplicados."
        ),
    }


@app.post("/api/superadmin/removals/dismiss")
async def dismiss_removals(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    try:
        order_ids = list(dict.fromkeys(int(value) for value in (body.get("order_ids") or [])))
    except (TypeError, ValueError):
        raise HTTPException(400, "Selecione somente retiradas válidas.")
    if not order_ids or len(order_ids) > 200 or any(value == 0 for value in order_ids):
        raise HTTPException(400, "Selecione de 1 a 200 retiradas entregues.")
    before = await run_sync(confirmed_removals)
    delivered = [item for item in before if int(item.get("os_id") or 0) in order_ids]
    await run_sync(lambda: logistics.audit(f"modem-delivery:{time.time_ns()}", logistics_actor(context[1]),
        {"action": "Recebimento físico de modens confirmado pelo supervisor", "before": delivered}))
    removed = await run_sync(lambda: dismiss_confirmed_removals(order_ids))
    sheet_rows = [removal_sheet_row(item, True) for item in delivered]
    sheet_synced = await run_sync(lambda: send_sheet_sync("deliver_many", sheet_rows)) if sheet_rows else False
    items = await run_sync(confirmed_removals)
    result = removal_payload(items)
    result["removed"] = removed
    result["sheet_synced"] = sheet_synced
    result["notice"] = f"{removed} retirada(s) entregue(s) e enviada(s) para a planilha semanal."
    return result


@app.post("/api/superadmin/users/role")
async def superadmin_user_role(request: Request, context=Depends(session_context)):
    require_superadmin_session(context[1])
    body = await request.json()
    username = str(body.get("username") or "").strip()[:120]
    name = str(body.get("name") or username).strip()[:180]
    role = str(body.get("role") or "").strip()
    route_monitor = body.get("route_monitor")
    popup_enabled = body.get("popup_enabled")
    current = str(context[1]["user"].get("usuario") or "Supervisor")
    if not username:
        raise HTTPException(400, "Informe o usuário do SGP.")
    if username.casefold() == "gustavo" and role != "superadmin":
        raise HTTPException(409, "Gustavo é o supervisor principal e não pode perder esse acesso.")
    if username.casefold() == current.casefold() and role != "superadmin":
        raise HTTPException(409, "Você não pode bloquear ou reduzir o próprio acesso.")
    try:
        await run_sync(lambda: set_user_role(username, role, current))
        await run_sync(lambda: record_user(username, name, login=False))
        if isinstance(route_monitor, bool):
            members = await run_sync(route_monitor_members)
            matches = [member for member in members if _same_person(username, member.get("username")) or _same_person(name, member.get("display_name"))]
            for member in matches:
                await run_sync(lambda value=member: set_route_monitor_member(str(value.get("username")), str(value.get("display_name")), False, current))
            if route_monitor:
                await run_sync(lambda: set_route_monitor_member(username, name or username, True, current))
        if isinstance(popup_enabled, bool):
            await run_sync(lambda: set_popup_access_member(username, popup_enabled, current))
        await store.delete_prefix("cache:data:")
    except ValueError as exc:
        raise HTTPException(400, "Escolha um perfil válido.") from exc
    return {"notice": f"Acesso de {username} atualizado com sucesso."}


@app.delete("/api/superadmin/users/{username}")
async def superadmin_delete_user(username: str, context=Depends(session_context)):
    require_superadmin_session(context[1])
    clean = str(username or "").strip()[:120]
    current = str(context[1]["user"].get("usuario") or "")
    if not clean:
        raise HTTPException(400, "Informe o usuário que será excluído.")
    if clean.casefold() == "gustavo" or clean.casefold() == current.casefold():
        raise HTTPException(409, "O supervisor principal não pode excluir o próprio perfil.")
    users = await run_sync(platform_users)
    target = next((user for user in users if str(user.get("username") or "").casefold() == clean.casefold()), None)
    if not target:
        raise HTTPException(404, "Perfil não encontrado no app.")
    members = await run_sync(route_monitor_members)
    for member in members:
        if _same_person(clean, member.get("username")) or _same_person(target.get("name"), member.get("display_name")):
            await run_sync(lambda value=member: set_route_monitor_member(str(value.get("username")), str(value.get("display_name")), False, current))
    await run_sync(lambda: delete_platform_user(clean))
    for session in await store.active_sessions():
        session_user = str((session.get("user") or {}).get("usuario") or session.get("credential_username") or "")
        if session_user.casefold() == clean.casefold() and session.get("session_id"):
            await store.delete_session(str(session["session_id"]))
    await store.delete_prefix("cache:data:")
    return {"notice": f"Perfil de {target.get('name') or clean} excluído do app."}


_cto_tasks: set[asyncio.Task] = set()

async def exclusive_cto_query(function, key="cto-upstream-global-v1", busy_message=None):
    """Serialize CTO upstream work across users; disconnects do not unlock it."""
    token = os.urandom(16).hex()
    if not await store.acquire_owned(key, token, 120):
        raise SGPError(busy_message or "Já existe uma consulta de CTO em andamento. Aguarde a conclusão para consultar novamente.", 409)

    async def renew():
        while True:
            await asyncio.sleep(20)
            try:
                await store.maintain_owned(key, token, 120)
            except Exception:
                logging.getLogger(__name__).exception("Unable to renew CTO query lease")

    async def execute():
        heartbeat = asyncio.create_task(renew())
        try:
            return await function()
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            try:
                await store.maintain_owned(key, token)
            except Exception:
                logging.getLogger(__name__).exception("Unable to release CTO query lease; it will expire")

    task = asyncio.create_task(execute())
    _cto_tasks.add(task)
    def finished(done):
        _cto_tasks.discard(done)
        if not done.cancelled():
            done.exception()  # Consume failures if the requesting browser disconnected.
    task.add_done_callback(finished)
    return await asyncio.shield(task)


@app.get("/api/cto/verify")
async def verify_cto(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    try:
        values = request.query_params.get("contracts", "").replace("\n", ",").split(",")
        ids = list(dict.fromkeys(int(value) for value in values if value.strip()))
    except ValueError:
        raise HTTPException(400, "Informe somente IDs de contrato.")
    if not 1 <= len(ids) <= 40:
        raise HTTPException(400, "Informe entre 1 e 40 contratos.")
    try:
        cache_ids = ",".join(str(value) for value in sorted(ids))
        deep = request.query_params.get("deep") == "1"
        async def query():
            def collect():
                result = context[2].verify_cto_contracts(ids, include_history=False)
                return {**result, "consulted_at": time.time(), "history_loaded": False}
            fingerprint = hashlib.sha256(cache_ids.encode()).hexdigest()[:20]
            base = await cached(
                context[1], f"cto-verify-v2:False:{fingerprint}", 120,
                collect, force=request.query_params.get("refresh") == "1",
                stale_ttl=120, persistent_ttl=86400, allow_stale_on_error=False,
            )
            if not deep:
                return base
            def evidence():
                result = copy.deepcopy(base)
                reference = result.get("reference")
                group = tuple(str(reference.get(key) or "") for key in ("olt_id", "slot", "pon")) if reference else None
                context[2]._enrich_cto_disconnect_analysis(result["contracts"], group)
                result["history_loaded"] = True
                return result
            return await cached(
                context[1], f"cto-evidence-v2:{fingerprint}:{base.get('consulted_at')}", 120,
                evidence, stale_ttl=120, persistent_ttl=86400, allow_stale_on_error=False,
            )
        result = await exclusive_cto_query(query)
    except SGPError:
        raise
    except Exception as exc:
        raise SGPError(
            "O SGP não concluiu a consulta da CTO. Aguarde alguns instantes e tente novamente.",
            503,
        ) from exc
    reference = result.get("reference") or {}
    rows = result.get("contracts") or []
    username = str((context[1].get("user") or {}).get("usuario") or "Supervisor")
    summary = {
        "olt": reference.get("olt_name") or reference.get("olt_id"), "slot": reference.get("slot"),
        "pon": reference.get("pon"), "cto": reference.get("label"), "found": sum(bool(row.get("found")) for row in rows),
        "divergent": sum(row.get("belongs") is False for row in rows), "total": len(rows),
    }
    try:
        await run_sync(lambda: record_cto_query("Contratos e portas", username, ids, summary))
    except Exception:
        # The audit trail is secondary and may never invalidate the analysis.
        pass
    return result


@app.get("/api/superadmin/cto/history")
async def list_cto_query_history(context=Depends(session_context)):
    require_office_tools(context[1])
    return {"items": await run_sync(cto_query_history), "retention_days": 14}


@app.get("/api/cto/pon-status")
async def pon_status(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    try:
        olt_id, slot, pon = (
            int(request.query_params[key]) for key in ("olt_id", "slot", "pon")
        )
    except (KeyError, ValueError):
        raise HTTPException(400, "OLT, slot ou PON inválido.")
    # Manual one-shot check. The frontend has no timer or polling.
    try:
        result = await exclusive_cto_query(lambda: call_context(
            context, lambda client, _session: client.pon_connection_status(olt_id, slot, pon),
        ))
        if request.query_params.get("offline_only") == "1":
            all_onus = result.get("onus") or []
            offline = [onu for onu in all_onus if not onu.get("online")]
            result = {
                **result,
                "onus": offline,
                "offline": len(offline),
                "total_on_pon": len(all_onus),
            }
        return result
    except SGPError:
        raise
    except Exception as exc:
        raise SGPError(
            "Não foi possível consultar esta PON agora. Confirme se a OLT está online e tente novamente.",
            503,
        ) from exc


@app.get("/api/cto/attenuation")
async def cto_attenuation(request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    try:
        contract_id = int(request.query_params["contract"])
    except (KeyError, ValueError):
        raise HTTPException(400, "Informe um ID de contrato válido.")
    if contract_id <= 0:
        raise HTTPException(400, "Informe um ID de contrato válido.")
    result = await exclusive_cto_query(lambda: cached(
        context[1], f"cto-attenuation:{contract_id}", 120,
        lambda: context[2].cto_attenuation(contract_id), stale_ttl=120, allow_stale_on_error=False,
    ))
    reference = result.get("reference") or {}
    username = str((context[1].get("user") or {}).get("usuario") or "Supervisor")
    summary = {"olt": reference.get("olt_name") or reference.get("olt_id"), "slot": reference.get("slot"), "pon": reference.get("pon"), "cto": reference.get("label"), "attenuated": result.get("attenuated"), "total": len(result.get("clients") or [])}
    try:
        await run_sync(lambda: record_cto_query("Atenuação", username, [contract_id], summary))
    except Exception:
        pass
    return result


def _tracking_key(token: str) -> str:
    return "customer-tracking-v1:" + hashlib.sha256(token.encode()).hexdigest()


def _live_location_key(technician: str) -> str:
    normalized = re.sub(r"\s+", " ", str(technician or "").strip().casefold())
    return "technician-live-location-v1:" + hashlib.sha256(normalized.encode()).hexdigest()


def _technician_shift_key(technician: str) -> str:
    normalized = re.sub(r"\s+", " ", str(technician or "").strip().casefold())
    return "technician-shift-v1:" + hashlib.sha256(normalized.encode()).hexdigest()


def _technician_identity(session: dict) -> str:
    user = session.get("user") or {}
    return str(user.get("nome") or user.get("usuario") or "").strip()


def _shift_is_active(snapshot: dict | None, now: datetime | None = None) -> bool:
    local_now = now or datetime.now(ZoneInfo("America/Sao_Paulo"))
    return bool(
        snapshot
        and snapshot.get("active") is True
        and snapshot.get("date") == local_now.date().isoformat()
        and local_now.hour < 19
    )


def _active_tracking_key(technician: str) -> str:
    normalized = re.sub(r"\s+", " ", str(technician or "").strip().casefold())
    return "technician-active-tracking-v1:" + hashlib.sha256(normalized.encode()).hexdigest()


def _os_tracking_cache_key(token_hash: str) -> str:
    return "os-tracking-v1:" + token_hash


async def _tracking_for_token(token: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", token):
        raise HTTPException(404, "Acompanhamento não encontrado.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    session = await run_sync(lambda: postgres_operations.tracking_session(token_hash))
    if not session or float(session.get("expires_at") or 0) <= time.time():
        raise HTTPException(410, "Este link expirou.")
    if session.get("state") == "cancelled":
        raise HTTPException(410, "Este link foi revogado.")
    return session


async def _tracking_for_technician(order_id: int, context) -> dict[str, Any]:
    order = await run_sync(lambda: context[2].order(order_id, include_onu=False, include_signal_context=False))
    if not order or not technician_owns_order(order, context[1]):
        raise HTTPException(403, "Esta OS não pertence ao técnico logado.")
    session = await run_sync(lambda: postgres_operations.tracking_session_for_order(order_id))
    if not session:
        return await _prepare_order_tracking(order_id, context)
    # Reconcile with SGP on every technician operation so a close/pause done
    # outside this app also stops GPS writes and customer chat.
    current_status = int(order.get("os_status") or -1)
    if current_status in (1, 3) and session.get("state") not in ("finished", "paused", "cancelled"):
        ended_state = "finished" if current_status == 1 else "paused"
        await _end_order_tracking(order_id, ended_state)
        session["state"] = ended_state
        session["last_location"] = None
    return session


async def _prepare_order_tracking(order_id: int, context) -> dict[str, Any]:
    order = await run_sync(lambda: context[2].order(order_id, include_onu=False, include_signal_context=False))
    if not order or int(order.get("os_status") or -1) != 2 or not technician_owns_order(order, context[1]):
        raise HTTPException(403, "O acompanhamento exige uma OS em execução atribuída ao técnico logado.")
    existing = await run_sync(lambda: postgres_operations.tracking_session_for_order(order_id))
    if existing and existing.get("state") in ("traveling", "arrived"):
        return existing
    if existing and existing.get("state") == "ready" and existing.get("ready_token"):
        return existing
    destination = _coordinates(order.get("endereco_ll") or order.get("contrato_endereco_ll"))
    if not destination:
        raise HTTPException(422, "A OS não possui coordenadas válidas para o acompanhamento.")
    existing = await run_sync(lambda: postgres_operations.tracking_session_for_order(order_id))
    if existing and existing.get("state") in ("traveling", "arrived", "finished"):
        raise HTTPException(409, "Esta OS já possui acompanhamento associado.")
    if existing:
        await _end_order_tracking(order_id, "cancelled")
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    technician = _technician_identity(context[1])
    expires = time.time() + 12 * 3600
    value = {"token_hash": token_hash, "order_id": int(order_id), "technician": technician,
             "client": str(order.get("cliente") or "Cliente").strip().split()[0],
             "destination": {"latitude": destination[0], "longitude": destination[1]},
             "state": "ready", "created_at": time.time(), "expires_at": expires,
             "messages": [], "last_location": None, "unread_client": 0, "unread_technician": 0,
             "tracking_url": None, "ready_token": token}
    await run_sync(lambda: postgres_operations.save_tracking_session(token_hash, value, 12 * 3600))
    await run_sync(lambda: postgres_operations.save_tracking_session(f"os-index-{order_id}", {"token_hash": token_hash}, 12 * 3600))
    return value


async def _end_order_tracking(order_id: int, state: str) -> None:
    index = await run_sync(lambda: postgres_operations.tracking_session(f"os-index-{order_id}"))
    if not index:
        return
    session = await run_sync(lambda: postgres_operations.tracking_session(index.get("token_hash", "")))
    if not session:
        return
    session["state"] = state
    session["ended_at"] = time.time()
    session["last_location"] = None
    await run_sync(lambda: postgres_operations.save_tracking_session(index["token_hash"], session, 12 * 3600))
    if state in ("paused", "finished", "cancelled"):
        await run_sync(lambda: postgres_operations.delete_tracking_session(f"os-index-{order_id}"))


@app.get("/api/technician/shift")
async def technician_shift_status(context=Depends(session_context)):
    require_technician(context[1])
    technician = _technician_identity(context[1])
    snapshot = await run_sync(lambda: cache_snapshot(_technician_shift_key(technician), retention_seconds=2 * 86400))
    return {"active": _shift_is_active(snapshot), "ends_at": "19:00"}


@app.post("/api/technician/shift")
async def technician_shift_update(request: Request, context=Depends(session_context)):
    require_technician(context[1])
    body = await request.json()
    active = body.get("active") is True
    now = datetime.now(ZoneInfo("America/Sao_Paulo"))
    if active and now.hour >= 19:
        return {"active": False, "ends_at": "19:00"}
    technician = _technician_identity(context[1])
    await run_sync(lambda: save_cache_snapshot(_technician_shift_key(technician), {
        "active": active, "date": now.date().isoformat(), "updated_at": time.time(),
    }, 2 * 86400))
    if not active:
        await run_sync(lambda: delete_cache_snapshots_prefix(_live_location_key(technician)))
    return {"active": active, "ends_at": "19:00"}


@app.post("/api/technician/live-location")
async def technician_live_location(request: Request, context=Depends(session_context)):
    require_technician(context[1])
    technician = _technician_identity(context[1])
    shift = await run_sync(lambda: cache_snapshot(_technician_shift_key(technician), retention_seconds=2 * 86400))
    if not _shift_is_active(shift):
        return {"ok": True, "active": False}
    body = await request.json()
    try:
        latitude, longitude = float(body["latitude"]), float(body["longitude"])
        accuracy = max(0.0, min(float(body.get("accuracy") or 0), 5000.0))
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Localização inválida.")
    await run_sync(lambda: save_cache_snapshot(_live_location_key(technician), {
        "latitude": latitude, "longitude": longitude, "accuracy": accuracy,
        "updated_at": time.time(), "technician": technician,
    }, 12 * 3600))
    return {"ok": True, "updated_at": time.time()}


@app.post("/api/office/orders/{order_id}/tracking-link")
async def create_tracking_link(order_id: int, request: Request, context=Depends(session_context)):
    require_office_tools(context[1])
    order = await run_sync(lambda: context[2].order(order_id, include_onu=False, include_signal_context=False))
    if not order or int(order.get("os_id") or 0) != int(order_id):
        raise HTTPException(404, "OS não encontrada.")
    reason = str(order.get("os_motivo_descricao") or "")
    if "instala" not in reason.casefold() and "ativa" not in reason.casefold():
        raise HTTPException(422, "O acompanhamento público está disponível somente para instalações.")
    technician = str(order.get("os_tecnico_responsavel") or "").strip()
    if not technician:
        raise HTTPException(422, "Direcione a instalação para um técnico antes de gerar o link.")
    destination = _coordinates(order.get("endereco_ll") or order.get("contrato_endereco_ll"))
    if not destination:
        raise HTTPException(422, "A instalação não possui coordenadas válidas para o mapa.")
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + 8 * 3600
    client_name = str(order.get("cliente") or "Cliente").strip().split()[0]
    payload = {"os_id": int(order_id), "technician": technician, "client": client_name,
               "destination": {"latitude": destination[0], "longitude": destination[1]},
               "scheduled_at": order.get("os_data_agendamento"), "expires_at": expires_at,
               "created_by": str((context[1].get("user") or {}).get("usuario") or "Escritório")}
    await run_sync(lambda: save_cache_snapshot(_tracking_key(token), payload, 8 * 3600))
    await run_sync(lambda: save_cache_snapshot(_active_tracking_key(technician), {
        "token_hash": hashlib.sha256(token.encode()).hexdigest(), "os_id": int(order_id),
        "expires_at": expires_at,
    }, 8 * 3600))
    return {"url": str(request.base_url).rstrip("/") + f"/acompanhar/{token}", "expires_at": expires_at}


@app.get("/api/public/tracking/{token}")
async def public_tracking(token: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,80}", token):
        raise HTTPException(404, "Acompanhamento não encontrado.")
    tracking = await run_sync(lambda: cache_snapshot(_tracking_key(token), retention_seconds=8 * 3600))
    if not tracking or float(tracking.get("expires_at") or 0) <= time.time():
        raise HTTPException(410, "Este acompanhamento expirou.")
    active = await run_sync(lambda: cache_snapshot(_active_tracking_key(tracking["technician"]), retention_seconds=8 * 3600))
    if not active or not hmac.compare_digest(str(active.get("token_hash") or ""), hashlib.sha256(token.encode()).hexdigest()):
        raise HTTPException(410, "Este acompanhamento foi encerrado ou substituído por outro serviço.")
    shift = await run_sync(lambda: cache_snapshot(_technician_shift_key(tracking["technician"]), retention_seconds=2 * 86400))
    location = await run_sync(lambda: cache_snapshot(_live_location_key(tracking["technician"]), retention_seconds=12 * 3600))
    fresh = bool(_shift_is_active(shift) and location and time.time() - float(location.get("updated_at") or 0) <= 180)
    public_location = ({key: location.get(key) for key in ("latitude", "longitude", "accuracy", "updated_at")}
                       if fresh else None)
    return {"client": tracking["client"], "technician": tracking["technician"].split()[0],
            "scheduled_at": tracking.get("scheduled_at"), "destination": tracking["destination"],
            "location": public_location, "status": "on_the_way" if fresh else "waiting_location",
            "expires_at": tracking["expires_at"]}


@app.get("/acompanhar/{token}", include_in_schema=False)
async def public_tracking_page(token: str):
    return FileResponse(STATIC / "tracking.html", headers={"Cache-Control": "no-store"})


app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
app.mount("/tutorial-media", StaticFiles(directory=TUTORIAL_UPLOADS), name="tutorial-media")


@app.get("/{path:path}", include_in_schema=False)
async def frontend(path: str):
    candidate = (STATIC / path).resolve()
    if STATIC.resolve() in candidate.parents and candidate.is_file():
        return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
    return FileResponse(
        STATIC / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
