from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.data_crypto import decrypted_json, encrypted_json


POSTGRES_ACTIVE = False
_pool: ConnectionPool | None = None

RLS_TABLES = (
    "app_sessions",
    "query_leases",
    "events", "locations", "assignments", "observed_orders", "completed_orders",
    "platform_users", "route_monitor_members", "popup_access_members", "platform_roles", "repeat_refunds",
    "onu_relink_drafts", "covered_streets", "equipment_references", "confirmed_removals",
    "return_obligations", "connector_returns", "installation_material_alerts",
    "cto_query_history", "persistent_cache", "route_alert_reviews", "order_text_drafts", "coverage_buildings", "coverage_pon_areas",
    "v2_migrations", "stock_balance_snapshots", "logistics_cases", "logistics_events", "logistics_revision",
)


def _configure_rls(connection: psycopg.Connection) -> None:
    """Restrict every application table to the configured database role.

    The app is single-tenant and connects through one least-privilege role, so
    isolation belongs at the database-role boundary rather than pretending each
    web user has a PostgreSQL login.
    """
    role = str(connection.execute("SELECT current_user").fetchone()[0])
    for table in RLS_TABLES:
        identifier = sql.Identifier(table)
        policy = sql.Identifier(f"ebf_{table}_app_role")
        connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(identifier))
        connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(identifier))
        connection.execute(sql.SQL("REVOKE ALL ON TABLE {} FROM PUBLIC").format(identifier))
        connection.execute(sql.SQL("DROP POLICY IF EXISTS {} ON {}").format(policy, identifier))
        connection.execute(
            sql.SQL("CREATE POLICY {} ON {} FOR ALL TO {} USING (true) WITH CHECK (true)").format(
                policy, identifier, sql.Identifier(role),
            )
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_sessions(session_key TEXT PRIMARY KEY, payload BYTEA NOT NULL, expires_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS app_sessions_expiry ON app_sessions(expires_at);
CREATE TABLE IF NOT EXISTS query_leases(name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS logistics_cases(id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS logistics_events(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS logistics_events_case ON logistics_events(case_id,created_at);
CREATE TABLE IF NOT EXISTS logistics_revision(id INTEGER PRIMARY KEY, revision BIGINT NOT NULL);
INSERT INTO logistics_revision(id,revision) VALUES(1,0) ON CONFLICT(id) DO NOTHING;
CREATE TABLE IF NOT EXISTS events(id BIGSERIAL PRIMARY KEY, os_id BIGINT, technician TEXT, kind TEXT, detail TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS locations(technician TEXT PRIMARY KEY, latitude DOUBLE PRECISION, longitude DOUBLE PRECISION, accuracy DOUBLE PRECISION, updated_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS assignments(os_id BIGINT PRIMARY KEY, technician TEXT, assigned_by TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS observed_orders(os_id BIGINT PRIMARY KEY, status INTEGER, technician TEXT, updated_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS completed_orders(os_id BIGINT PRIMARY KEY, technician TEXT, client TEXT, reason TEXT, opened_at TEXT, scheduled_at TEXT, closed_at TEXT, imported_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS platform_users(username TEXT PRIMARY KEY, name TEXT, first_login DOUBLE PRECISION, last_login DOUBLE PRECISION, last_activity DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS route_monitor_members(username TEXT PRIMARY KEY, display_name TEXT NOT NULL, granted_by TEXT, updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS popup_access_members(username TEXT PRIMARY KEY, granted_by TEXT, updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS platform_roles(username TEXT PRIMARY KEY, role TEXT NOT NULL, granted_by TEXT, updated_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS repeat_refunds(original_order_id BIGINT, repeat_order_id BIGINT, technician TEXT, points DOUBLE PRECISION, refunded_by TEXT, refunded_at DOUBLE PRECISION, PRIMARY KEY(original_order_id,repeat_order_id,technician));
ALTER TABLE repeat_refunds ADD COLUMN IF NOT EXISTS restored_order_id BIGINT;
CREATE TABLE IF NOT EXISTS onu_relink_drafts(serial TEXT PRIMARY KEY, order_id BIGINT, values_json TEXT NOT NULL, saved_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS covered_streets(cep TEXT PRIMARY KEY, street TEXT NOT NULL, city TEXT, state TEXT, confirmed_by TEXT, confirmed_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS equipment_references(gpon TEXT PRIMARY KEY, mac TEXT UNIQUE, product_id BIGINT NOT NULL, stock_id BIGINT NOT NULL, purchase_id TEXT, photo_hash TEXT, created_by TEXT, created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS confirmed_removals(os_id BIGINT PRIMARY KEY, technician TEXT, client_id BIGINT, client TEXT, cpf TEXT, serial TEXT, return_type TEXT, closed_at TEXT, evidence_at TEXT, history_user TEXT, evidence TEXT, counted_at DOUBLE PRECISION NOT NULL, dismissed_at DOUBLE PRECISION);
ALTER TABLE confirmed_removals ADD COLUMN IF NOT EXISTS dismissed_at DOUBLE PRECISION;
ALTER TABLE confirmed_removals ADD COLUMN IF NOT EXISTS return_type TEXT;
ALTER TABLE confirmed_removals ADD COLUMN IF NOT EXISTS cpf TEXT;
UPDATE confirmed_removals SET return_type=CASE WHEN lower(COALESCE(evidence,'')) LIKE '%substitu%' OR lower(COALESCE(evidence,'')) LIKE '%troca:%' THEN 'troca' ELSE 'retirada' END WHERE return_type IS NULL OR return_type='';
CREATE TABLE IF NOT EXISTS return_obligations(technician TEXT PRIMARY KEY, connectors INTEGER NOT NULL DEFAULT 0, modems_json TEXT NOT NULL DEFAULT '[]', note TEXT, updated_by TEXT, updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS connector_returns(source_id TEXT PRIMARY KEY, os_id BIGINT, technician TEXT, client TEXT, product TEXT, quantity DOUBLE PRECISION NOT NULL, original_quantity DOUBLE PRECISION NOT NULL, used_at TEXT, reason TEXT, note TEXT, counted_at DOUBLE PRECISION NOT NULL, dismissed_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS installation_material_alerts(os_id BIGINT PRIMARY KEY, technician TEXT, client TEXT, contract_id BIGINT, closed_at TEXT, reason TEXT, checked_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS cto_query_history(id BIGSERIAL PRIMARY KEY, query_type TEXT NOT NULL, username TEXT NOT NULL, contracts_json TEXT NOT NULL, summary_json TEXT NOT NULL, queried_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cto_query_history_queried_at ON cto_query_history(queried_at);
CREATE INDEX IF NOT EXISTS idx_connector_returns_used_at ON connector_returns(used_at);
CREATE TABLE IF NOT EXISTS persistent_cache(cache_key TEXT PRIMARY KEY, value_json TEXT NOT NULL, saved_at DOUBLE PRECISION NOT NULL, accessed_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS stock_balance_snapshots(id BIGSERIAL PRIMARY KEY, location_id BIGINT NOT NULL, captured_at DOUBLE PRECISION NOT NULL, items_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS stock_balance_snapshots_location ON stock_balance_snapshots(location_id,captured_at);
CREATE TABLE IF NOT EXISTS route_alert_reviews(service_day TEXT NOT NULL, os_id BIGINT NOT NULL, technician TEXT NOT NULL, comment TEXT NOT NULL, reviewed_by TEXT NOT NULL, reviewed_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(service_day,os_id,technician));
CREATE TABLE IF NOT EXISTS order_text_drafts(os_id BIGINT NOT NULL, username TEXT NOT NULL, text_value TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(os_id,username));
CREATE TABLE IF NOT EXISTS coverage_buildings(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', latitude DOUBLE PRECISION NOT NULL, longitude DOUBLE PRECISION NOT NULL, updated_by TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS coverage_pon_areas(id BIGSERIAL PRIMARY KEY, olt_id TEXT NOT NULL DEFAULT '', olt_name TEXT NOT NULL DEFAULT '', slot TEXT NOT NULL, pon TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', geometry_json TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS idx_coverage_pon_areas_port ON coverage_pon_areas(olt_id,slot,pon);
CREATE INDEX IF NOT EXISTS idx_persistent_cache_expires ON persistent_cache(expires_at);
CREATE TABLE IF NOT EXISTS v2_migrations(name TEXT PRIMARY KEY, applied_at DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_order_kind ON events(os_id,kind,created_at);
CREATE INDEX IF NOT EXISTS idx_assignments_technician_lower ON assignments(lower(technician));
CREATE INDEX IF NOT EXISTS idx_completed_closed_at ON completed_orders(closed_at);
CREATE INDEX IF NOT EXISTS idx_removals_closed_at ON confirmed_removals(closed_at);
INSERT INTO platform_roles(username,role,granted_by,updated_at) VALUES('Gustavo','superadmin','Sistema',0) ON CONFLICT(username) DO NOTHING;
INSERT INTO platform_roles(username,role,granted_by,updated_at) VALUES('DavidR','technician','Sistema',0) ON CONFLICT(username) DO NOTHING;
INSERT INTO platform_roles(username,role,granted_by,updated_at) VALUES('Kaionunes','admin','Gustavo',0) ON CONFLICT(username) DO NOTHING;
"""


def _connection():
    if not _pool:
        raise RuntimeError("PostgreSQL nÃ£o inicializado")
    return _pool.connection()


def session_record(session_key):
    return _one("SELECT payload,expires_at FROM app_sessions WHERE session_key=%s", (session_key,))


def save_session_record(session_key, payload, expires_at):
    with _connection() as connection:
        connection.execute("DELETE FROM app_sessions WHERE expires_at < %s", (time.time() - 31 * 86400,))
        connection.execute("""INSERT INTO app_sessions VALUES(%s,%s,%s)
            ON CONFLICT(session_key) DO UPDATE SET payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at
            WHERE octet_length(app_sessions.payload)>0""", (session_key, payload, expires_at))


def revoke_session_record(session_key):
    # A tombstone prevents an old Redis copy or concurrent save reviving logout.
    _execute("""INSERT INTO app_sessions VALUES(%s,%s,%s)
        ON CONFLICT(session_key) DO UPDATE SET payload=EXCLUDED.payload,expires_at=EXCLUDED.expires_at""",
        (session_key, b"", time.time() + 31 * 86400))


def active_session_records():
    return _rows("SELECT payload,expires_at FROM app_sessions WHERE expires_at>%s AND octet_length(payload)>0", (time.time(),))


def _rows(query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with _connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, parameters)
            return [dict(row) for row in cursor.fetchall()]


def _one(query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    values = _rows(query, parameters)
    return values[0] if values else None


def _execute(query: str, parameters: tuple[Any, ...] = ()) -> None:
    with _connection() as connection:
        connection.execute(query, parameters)


def cache_snapshot(cache_key: str, max_age: int | None = None, retention_seconds: int = 14 * 86400) -> dict[str, Any] | None:
    now = time.time()
    _execute("DELETE FROM persistent_cache WHERE expires_at < %s AND cache_key NOT IN ('pon-health-scan-v1', 'application-maintenance-v1')", (now,))
    row = _one("SELECT value_json,saved_at FROM persistent_cache WHERE cache_key=%s", (str(cache_key),))
    if not row or (max_age is not None and now - float(row["saved_at"]) > max_age):
        return None
    _execute(
        "UPDATE persistent_cache SET accessed_at=%s,expires_at=%s WHERE cache_key=%s",
        (now, now + max(60, int(retention_seconds)), str(cache_key)),
    )
    return decrypted_json(row["value_json"])


def save_cache_snapshot(cache_key: str, value: Any, retention_seconds: int = 14 * 86400) -> None:
    now = time.time()
    encoded = encrypted_json(value)
    _execute(
        """INSERT INTO persistent_cache(cache_key,value_json,saved_at,accessed_at,expires_at)
           VALUES(%s,%s,%s,%s,%s) ON CONFLICT(cache_key) DO UPDATE SET
           value_json=EXCLUDED.value_json,saved_at=EXCLUDED.saved_at,
           accessed_at=EXCLUDED.accessed_at,expires_at=EXCLUDED.expires_at""",
        (str(cache_key), encoded, now, now, now + max(60, int(retention_seconds))),
    )


def stock_balance_snapshot_before(location_id: int, cutoff: float) -> dict[str, Any] | None:
    row = _one(
        "SELECT captured_at,items_json FROM stock_balance_snapshots "
        "WHERE location_id=%s AND captured_at<%s ORDER BY captured_at DESC,id DESC LIMIT 1",
        (int(location_id), float(cutoff)),
    )
    return {"captured_at": float(row["captured_at"]), "items": decrypted_json(row["items_json"])} if row else None


def save_stock_balance_snapshot(location_id: int, items: list[dict[str, Any]], captured_at: float) -> None:
    with _connection() as connection:
        connection.execute(
            "INSERT INTO stock_balance_snapshots(location_id,captured_at,items_json) VALUES(%s,%s,%s)",
            (int(location_id), float(captured_at), encrypted_json(items)),
        )
        connection.execute(
            "DELETE FROM stock_balance_snapshots WHERE location_id=%s AND captured_at<%s",
            (int(location_id), float(captured_at) - 31 * 86400),
        )


def delete_cache_snapshots_except(prefix: str, keep_key: str) -> None:
    _execute(
        "DELETE FROM persistent_cache WHERE cache_key LIKE %s AND cache_key<>%s",
        (f"{prefix}%", str(keep_key)),
    )


def delete_cache_snapshots_prefix(prefix: str) -> None:
    _execute("DELETE FROM persistent_cache WHERE cache_key LIKE %s", (f"{prefix}%",))


def route_alert_reviews(service_day: str) -> list[dict[str, Any]]:
    return _rows(
        "SELECT * FROM route_alert_reviews WHERE service_day=%s ORDER BY reviewed_at DESC", (service_day,),
    )


def save_route_alert_review(service_day: str, os_id: int, technician: str, comment: str, reviewed_by: str) -> dict[str, Any]:
    clean_comment = str(comment or "").strip()
    if not clean_comment:
        raise ValueError("O comentário da análise é obrigatório.")
    reviewed_at = time.time()
    _execute(
        """INSERT INTO route_alert_reviews(service_day,os_id,technician,comment,reviewed_by,reviewed_at)
           VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(service_day,os_id,technician) DO UPDATE SET
           comment=EXCLUDED.comment,reviewed_by=EXCLUDED.reviewed_by,reviewed_at=EXCLUDED.reviewed_at""",
        (service_day, int(os_id), technician.strip(), clean_comment, reviewed_by.strip(), reviewed_at),
    )
    return {"service_day": service_day, "os_id": int(os_id), "technician": technician.strip(), "comment": clean_comment, "reviewed_by": reviewed_by.strip(), "reviewed_at": reviewed_at}


def coverage_buildings() -> list[dict[str, Any]]:
    return _rows("SELECT * FROM coverage_buildings ORDER BY lower(name),id")


def save_coverage_building(building_id: int | None, name: str, note: str, latitude: float, longitude: float, actor: str) -> dict[str, Any]:
    clean_name = str(name or "").strip()[:160]
    if not clean_name:
        raise ValueError("Informe o nome do prédio.")
    latitude, longitude = float(latitude), float(longitude)
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Latitude ou longitude inválida.")
    now = time.time()
    clean_note = str(note or "").strip()[:1000]
    if building_id:
        _execute(
            "UPDATE coverage_buildings SET name=%s,note=%s,latitude=%s,longitude=%s,updated_by=%s,updated_at=%s WHERE id=%s",
            (clean_name, clean_note, latitude, longitude, actor, now, int(building_id)),
        )
        row = _one("SELECT * FROM coverage_buildings WHERE id=%s", (int(building_id),))
        if not row:
            raise ValueError("Prédio não encontrado.")
        return row
    return _one(
        "INSERT INTO coverage_buildings(name,note,latitude,longitude,updated_by,updated_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
        (clean_name, clean_note, latitude, longitude, actor, now),
    ) or {}


def delete_coverage_building(building_id: int) -> None:
    _execute("DELETE FROM coverage_buildings WHERE id=%s", (int(building_id),))


def tracking_session(token_hash: str) -> dict[str, Any] | None:
    return cache_snapshot(f"os-tracking-v1:{token_hash}", None, 14 * 86400)


def tracking_session_for_order(order_id: int) -> dict[str, Any] | None:
    index = tracking_session(f"os-index-{int(order_id)}")
    return tracking_session(index.get("token_hash", "")) if index else None


def save_tracking_session(token_hash: str, value: dict[str, Any], retention_seconds: int = 14 * 86400) -> None:
    save_cache_snapshot(f"os-tracking-v1:{token_hash}", value, retention_seconds)


def delete_tracking_session(token_hash: str) -> None:
    _execute("DELETE FROM persistent_cache WHERE cache_key=%s", (f"os-tracking-v1:{token_hash}",))


def coverage_pon_areas() -> list[dict[str, Any]]:
    rows = _rows("SELECT * FROM coverage_pon_areas ORDER BY lower(olt_name),slot,pon,id")
    for row in rows:
        try:
            row["geometry"] = json.loads(row.pop("geometry_json"))
        except (TypeError, ValueError):
            row["geometry"] = None
    return rows


def save_coverage_pon_area(area_id: int | None, olt_id: str, olt_name: str, slot: str, pon: str, note: str, geometry: Any, actor: str) -> dict[str, Any]:
    clean_slot, clean_pon = str(slot or "").strip()[:30], str(pon or "").strip()[:30]
    if not clean_slot or not clean_pon:
        raise ValueError("Informe o Slot e a PON.")
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        raise ValueError("Desenhe uma área válida no mapa.")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, (list, tuple)) or not coordinates or not isinstance(coordinates[0], (list, tuple)) or len(coordinates[0]) < 4:
        raise ValueError("A área precisa ter pelo menos três pontos.")
    for point in coordinates[0]:
        if not isinstance(point, (list, tuple)) or len(point) < 2 or not -180 <= float(point[0]) <= 180 or not -90 <= float(point[1]) <= 90:
            raise ValueError("A área contém uma coordenada inválida.")
    payload = json.dumps(geometry, ensure_ascii=False, separators=(",", ":"))
    now = time.time()
    values = (str(olt_id or "").strip()[:80], str(olt_name or "").strip()[:180], clean_slot, clean_pon,
              str(note or "").strip()[:1000], payload, actor, now)
    if area_id:
        _execute("UPDATE coverage_pon_areas SET olt_id=%s,olt_name=%s,slot=%s,pon=%s,note=%s,geometry_json=%s,updated_by=%s,updated_at=%s WHERE id=%s", (*values, int(area_id)))
        row = _one("SELECT * FROM coverage_pon_areas WHERE id=%s", (int(area_id),))
        if not row:
            raise ValueError("Área não encontrada.")
    else:
        row = _one("INSERT INTO coverage_pon_areas(olt_id,olt_name,slot,pon,note,geometry_json,updated_by,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *", values)
    row["geometry"] = json.loads(row.pop("geometry_json"))
    return row


def delete_coverage_pon_area(area_id: int) -> None:
    _execute("DELETE FROM coverage_pon_areas WHERE id=%s", (int(area_id),))


def order_text_draft(os_id: int, username: str) -> dict[str, Any] | None:
    return _one(
        "SELECT text_value,updated_at FROM order_text_drafts WHERE os_id=%s AND username=%s",
        (int(os_id), str(username).strip().casefold()),
    )


def save_order_text_draft(os_id: int, username: str, text_value: str) -> dict[str, Any]:
    value = str(text_value or "")[:4000]
    updated_at = time.time()
    _execute(
        """INSERT INTO order_text_drafts(os_id,username,text_value,updated_at) VALUES(%s,%s,%s,%s)
           ON CONFLICT(os_id,username) DO UPDATE SET text_value=EXCLUDED.text_value,updated_at=EXCLUDED.updated_at""",
        (int(os_id), str(username).strip().casefold(), value, updated_at),
    )
    return {"text_value": value, "updated_at": updated_at}


def delete_order_text_draft(os_id: int, username: str) -> None:
    _execute(
        "DELETE FROM order_text_drafts WHERE os_id=%s AND username=%s",
        (int(os_id), str(username).strip().casefold()),
    )


def confirmed_removals() -> list[dict[str, Any]]:
    return _rows("SELECT * FROM confirmed_removals WHERE dismissed_at IS NULL ORDER BY closed_at DESC, os_id DESC")


def expired_removals() -> list[dict[str, Any]]:
    cutoff = (date.today() - timedelta(days=13)).isoformat()
    delivered_cutoff = time.time() - (14 * 86400)
    return _rows(
        """SELECT * FROM confirmed_removals WHERE
           (dismissed_at > 0 AND dismissed_at < %s) OR
           (dismissed_at < 0 AND -dismissed_at < %s) OR
           (dismissed_at IS NULL AND substring(closed_at,1,10) < %s)""",
        (delivered_cutoff, delivered_cutoff, cutoff),
    )


def delete_expired_removals(order_ids: list[int]) -> int:
    clean = list(dict.fromkeys(int(value) for value in order_ids))
    if not clean:
        return 0
    with _connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM confirmed_removals WHERE os_id=ANY(%s)", (clean,))
            return cursor.rowcount


def delivered_removals() -> list[dict[str, Any]]:
    return _rows("SELECT * FROM confirmed_removals WHERE dismissed_at > 0 ORDER BY dismissed_at DESC, os_id DESC")


def add_manual_removal_payments(technician: str, quantity: int, delivered_at: float, serial: str = "") -> int:
    now = time.time()
    inserted = 0
    with _connection() as connection:
        with connection.cursor() as cursor:
            for index in range(int(quantity)):
                os_id = -int(now * 1_000_000) - index
                cursor.execute(
                    """INSERT INTO confirmed_removals
                       (os_id,technician,client,serial,return_type,closed_at,evidence_at,history_user,evidence,counted_at,dismissed_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(os_id) DO NOTHING""",
                    (os_id, technician, "Lançamento manual", serial or "Manual", "retirada", datetime.fromtimestamp(delivered_at).isoformat(),
                     datetime.fromtimestamp(delivered_at).isoformat(), "Supervisor", "Ajuste manual da planilha", now, delivered_at),
                )
                inserted += int(cursor.rowcount > 0)
    return inserted


def exclude_removal_payments(order_ids: list[int]) -> int:
    clean = list(dict.fromkeys(int(value) for value in order_ids if int(value) != 0))
    if not clean:
        return 0
    with _connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE confirmed_removals SET dismissed_at=%s WHERE os_id=ANY(%s) AND dismissed_at > 0", (-time.time(), clean))
            return max(0, cursor.rowcount)


def save_confirmed_removals(items: list[dict[str, Any]]) -> int:
    before = len(confirmed_removals())
    with _connection() as connection:
        for item in items:
            connection.execute(
                """INSERT INTO confirmed_removals
                   (os_id,technician,client_id,client,cpf,serial,return_type,closed_at,evidence_at,history_user,evidence,counted_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(os_id) DO UPDATE SET
                   technician=EXCLUDED.technician,client_id=EXCLUDED.client_id,client=EXCLUDED.client,
                   cpf=EXCLUDED.cpf,serial=EXCLUDED.serial,return_type=EXCLUDED.return_type,closed_at=EXCLUDED.closed_at,
                   evidence_at=EXCLUDED.evidence_at,history_user=EXCLUDED.history_user,evidence=EXCLUDED.evidence
                   WHERE confirmed_removals.dismissed_at IS NULL""",
                (int(item.get("os_id") or 0), item.get("technician"), item.get("client_id"),
                 item.get("client"), item.get("cpf"), item.get("serial"), item.get("return_type") or "retirada", item.get("closed_at"), item.get("evidence_at"),
                 item.get("history_user"), item.get("evidence"), time.time()),
            )
    return max(0, len(confirmed_removals()) - before)


def dismiss_confirmed_removals(order_ids: list[int]) -> int:
    clean = list(dict.fromkeys(int(value) for value in order_ids if int(value) != 0))
    if not clean:
        return 0
    with _connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE confirmed_removals SET dismissed_at=%s WHERE os_id=ANY(%s) AND dismissed_at IS NULL",
                (time.time(), clean),
            )
            return max(0, cursor.rowcount)


def upsert_sheet_removal(item: dict[str, Any]) -> dict[str, Any]:
    import hashlib
    import re
    sync_id = str(item.get("sync_id") or "").strip()
    serial = str(item.get("serial") or "").strip().upper()
    if not sync_id or not serial: raise ValueError("ID_SYNC e GPON são obrigatórios.")
    match = re.fullmatch(r"os-(-?\d+)", sync_id, re.I)
    os_id = int(match.group(1)) if match else -int(hashlib.sha256(sync_id.encode("utf-8")).hexdigest()[:14], 16)
    closed_at = str(item.get("date") or date.today().isoformat())[:10]
    _execute(
        """INSERT INTO confirmed_removals(os_id,technician,client_id,client,cpf,serial,return_type,closed_at,evidence_at,history_user,evidence,counted_at,dismissed_at)
           VALUES(%s,%s,NULL,%s,%s,%s,%s,%s,%s,'Google Planilhas',%s,%s,%s) ON CONFLICT(os_id) DO UPDATE SET
           technician=EXCLUDED.technician,client=EXCLUDED.client,cpf=EXCLUDED.cpf,serial=EXCLUDED.serial,
           return_type=EXCLUDED.return_type,closed_at=EXCLUDED.closed_at,evidence_at=EXCLUDED.evidence_at,dismissed_at=EXCLUDED.dismissed_at""",
        (os_id, item.get("technician"), item.get("client"), item.get("cpf"), serial,
         str(item.get("return_type") or "retirada").casefold(), closed_at, closed_at,
         f"Sincronizado da planilha: {sync_id}", time.time(), time.time() if item.get("delivered") else None),
    )
    return {"os_id": os_id, "sync_id": sync_id, "serial": serial}


def return_obligations() -> list[dict[str, Any]]:
    rows = _rows("SELECT * FROM return_obligations ORDER BY lower(technician)")
    for item in rows:
        try:
            raw = json.loads(item.pop("modems_json") or "[]")
            item["modems"] = [
                value if isinstance(value, dict) else {"gpon": str(value), "type": "troca"}
                for value in raw
            ]
        except (TypeError, json.JSONDecodeError):
            item["modems"] = []
    return rows


def save_return_obligation(technician: str, connectors: int, modems: list[Any], note: str, updated_by: str) -> dict[str, Any]:
    name = str(technician or "").strip()
    clean_modems = []
    seen = set()
    for value in modems:
        source = value if isinstance(value, dict) else {"gpon": value, "type": "troca"}
        gpon = str(source.get("gpon") or "").strip().upper()
        kind = str(source.get("type") or "troca").strip().casefold()
        if gpon and gpon not in seen:
            clean_modems.append({"gpon": gpon, "type": kind if kind in ("troca", "retirada") else "troca"})
            seen.add(gpon)
    if not name or connectors < 0 or (connectors == 0 and not clean_modems):
        raise ValueError("Informe o técnico e pelo menos um material para devolver.")
    now = time.time()
    _execute(
        """INSERT INTO return_obligations(technician,connectors,modems_json,note,updated_by,updated_at)
           VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(technician) DO UPDATE SET connectors=EXCLUDED.connectors,
           modems_json=EXCLUDED.modems_json,note=EXCLUDED.note,updated_by=EXCLUDED.updated_by,updated_at=EXCLUDED.updated_at""",
        (name, int(connectors), json.dumps(clean_modems, ensure_ascii=False), str(note or "").strip(), updated_by, now),
    )
    return {"technician": name, "connectors": int(connectors), "modems": clean_modems, "note": str(note or "").strip(), "updated_by": updated_by, "updated_at": now}


def delete_return_obligation(technician: str) -> int:
    with _connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM return_obligations WHERE lower(technician)=lower(%s)", (str(technician or "").strip(),))
            return max(0, cursor.rowcount)


def connector_returns() -> list[dict[str, Any]]:
    return _rows("SELECT * FROM connector_returns WHERE dismissed_at IS NULL ORDER BY used_at DESC, source_id")


def expired_connector_returns() -> list[dict[str, Any]]:
    cutoff = (date.today() - timedelta(days=13)).isoformat()
    return _rows("SELECT * FROM connector_returns WHERE substring(used_at,1,10) < %s", (cutoff,))


def delete_expired_connector_returns(source_ids: list[str]) -> int:
    clean = list(dict.fromkeys(str(value) for value in source_ids if str(value).strip()))
    if not clean:
        return 0
    with _connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM connector_returns WHERE source_id=ANY(%s)", (clean,))
            return cursor.rowcount


def save_connector_returns(items: list[dict[str, Any]]) -> int:
    inserted = 0
    with _connection() as connection:
        with connection.cursor() as cursor:
            for item in items:
                quantity = float(item.get("quantity") or 0)
                cursor.execute(
                    """INSERT INTO connector_returns(source_id,os_id,technician,client,product,quantity,original_quantity,used_at,reason,note,counted_at,dismissed_at)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL) ON CONFLICT(source_id) DO NOTHING""",
                    (str(item.get("source_id")), item.get("os_id"), item.get("technician"), item.get("client"), item.get("product"), quantity, quantity, item.get("used_at"), item.get("reason"), "", time.time()),
                )
                inserted += int(cursor.rowcount > 0)
    return inserted


def upsert_sheet_connector(item: dict[str, Any]) -> dict[str, Any]:
    sync_id = str(item.get("sync_id") or "").strip()
    source_id = sync_id.removeprefix("connector-").strip()
    if not source_id: raise ValueError("ID_SYNC do conector é obrigatório.")
    try: quantity = max(0.0, float(item.get("quantity") or 0))
    except (TypeError, ValueError) as exc: raise ValueError("Quantidade inválida para o conector.") from exc
    delivered = bool(item.get("delivered")) or quantity == 0
    used_at = str(item.get("date") or date.today().isoformat())[:10]
    _execute(
        """INSERT INTO connector_returns(source_id,os_id,technician,client,product,quantity,original_quantity,used_at,reason,note,counted_at,dismissed_at)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id) DO UPDATE SET
           os_id=EXCLUDED.os_id,technician=EXCLUDED.technician,client=EXCLUDED.client,product=EXCLUDED.product,
           quantity=EXCLUDED.quantity,used_at=EXCLUDED.used_at,reason=EXCLUDED.reason,note=EXCLUDED.note,
           dismissed_at=EXCLUDED.dismissed_at""",
        (source_id, item.get("os_id"), item.get("technician"), item.get("client"), item.get("product"),
         quantity, quantity, used_at, item.get("reason"), item.get("note"), time.time(), time.time() if delivered else None),
    )
    return {"source_id": source_id, "sync_id": sync_id, "quantity": quantity}


def set_sheet_connector_total(technician: str, quantity: float) -> dict[str, Any]:
    name = str(technician or "").strip()
    target = max(0.0, float(quantity or 0))
    if not name: raise ValueError("Técnico do resumo de conectores é obrigatório.")
    rows = _rows("SELECT * FROM connector_returns WHERE dismissed_at IS NULL AND lower(technician)=lower(%s) ORDER BY used_at,source_id", (name,))
    remaining = target
    with _connection() as connection:
        with connection.cursor() as cursor:
            for row in rows:
                current = float(row.get("quantity") or 0)
                kept = min(current, remaining)
                cursor.execute("UPDATE connector_returns SET quantity=%s,dismissed_at=%s WHERE source_id=%s", (kept, None if kept > 0 else time.time(), row["source_id"]))
                remaining -= kept
            if remaining > 0:
                source_id = "sheet-summary-" + hashlib.sha256(name.casefold().encode()).hexdigest()[:20]
                cursor.execute(
                    """INSERT INTO connector_returns(source_id,technician,client,product,quantity,original_quantity,used_at,reason,note,counted_at,dismissed_at)
                       VALUES(%s,%s,'Resumo da planilha','Conectores',%s,%s,%s,'Ajuste do total','',%s,NULL)
                       ON CONFLICT(source_id) DO UPDATE SET quantity=EXCLUDED.quantity,dismissed_at=NULL""",
                    (source_id, name, remaining, remaining, date.today().isoformat(), time.time()),
                )
    return {"technician": name, "quantity": target}


def update_connector_return(source_id: str, quantity: float, note: str) -> int:
    with _connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE connector_returns SET quantity=%s,note=%s WHERE source_id=%s AND dismissed_at IS NULL", (float(quantity), str(note or "").strip(), str(source_id)))
            return max(0, cursor.rowcount)


def dismiss_connector_returns(source_ids: list[str]) -> int:
    clean = list(dict.fromkeys(str(value) for value in source_ids if str(value).strip()))
    if not clean: return 0
    with _connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE connector_returns SET dismissed_at=%s WHERE source_id=ANY(%s) AND dismissed_at IS NULL", (time.time(), clean))
            return max(0, cursor.rowcount)


def installation_material_alerts() -> list[dict[str, Any]]:
    return _rows("SELECT * FROM installation_material_alerts ORDER BY closed_at DESC, os_id DESC")


def dismiss_installation_material_alerts(order_ids: list[int]) -> int:
    clean = list(dict.fromkeys(int(value) for value in order_ids if int(value) > 0))
    if not clean: return 0
    with _connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM installation_material_alerts WHERE os_id=ANY(%s)", (clean,))
            return max(0, cursor.rowcount)


def record_cto_query(query_type: str, username: str, contracts: list[int], summary: dict[str, Any]) -> None:
    now = time.time()
    with _connection() as connection:
        connection.execute("DELETE FROM cto_query_history WHERE queried_at<%s", (now - 14 * 86400,))
        connection.execute(
            "INSERT INTO cto_query_history(query_type,username,contracts_json,summary_json,queried_at) VALUES(%s,%s,%s,%s,%s)",
            (str(query_type), str(username), json.dumps(contracts), json.dumps(summary, ensure_ascii=False, default=str), now),
        )


def cto_query_history() -> list[dict[str, Any]]:
    _execute("DELETE FROM cto_query_history WHERE queried_at<%s", (time.time() - 14 * 86400,))
    result = []
    for row in _rows("SELECT * FROM cto_query_history ORDER BY queried_at DESC,id DESC"):
        try: row["contracts"] = json.loads(row.pop("contracts_json") or "[]")
        except (TypeError, json.JSONDecodeError): row["contracts"] = []
        try: row["summary"] = json.loads(row.pop("summary_json") or "{}")
        except (TypeError, json.JSONDecodeError): row["summary"] = {}
        result.append(row)
    return result


def replace_installation_material_alerts(selected_day: date, items: list[dict[str, Any]]) -> int:
    prefix = selected_day.isoformat()
    with _connection() as connection:
        connection.execute("DELETE FROM installation_material_alerts WHERE substring(closed_at,1,10)=%s", (prefix,))
        for item in items:
            connection.execute(
                """INSERT INTO installation_material_alerts(os_id,technician,client,contract_id,closed_at,reason,checked_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(os_id) DO UPDATE SET technician=EXCLUDED.technician,
                   client=EXCLUDED.client,contract_id=EXCLUDED.contract_id,closed_at=EXCLUDED.closed_at,
                   reason=EXCLUDED.reason,checked_at=EXCLUDED.checked_at""",
                (int(item["os_id"]), item.get("technician"), item.get("client"), item.get("contract_id"), item.get("closed_at"), item.get("reason"), time.time()),
            )
    return len(items)


def save_onu_relink_draft(serial: str, order_id: int, values: dict[str, Any]) -> None:
    serial = str(serial or "").strip()
    if not serial:
        return
    safe = {str(key): str(value) for key, value in values.items() if value is not None and str(value).strip()}
    _execute(
        """INSERT INTO onu_relink_drafts(serial,order_id,values_json,saved_at) VALUES(%s,%s,%s,%s)
           ON CONFLICT(serial) DO UPDATE SET order_id=EXCLUDED.order_id,values_json=EXCLUDED.values_json,saved_at=EXCLUDED.saved_at""",
        (serial, int(order_id or 0), json.dumps(safe, ensure_ascii=False), time.time()),
    )


def onu_relink_drafts() -> dict[str, dict[str, Any]]:
    _execute("DELETE FROM onu_relink_drafts WHERE saved_at<%s", (time.time() - 30 * 86400,))
    result = {}
    for row in _rows("SELECT serial,order_id,values_json,saved_at FROM onu_relink_drafts"):
        try:
            values = json.loads(row["values_json"])
        except (TypeError, json.JSONDecodeError):
            values = {}
        result[str(row["serial"]).casefold()] = {"order_id": row["order_id"], "values": values, "saved_at": row["saved_at"]}
    return result


def delete_onu_relink_draft(serial: str) -> None:
    _execute("DELETE FROM onu_relink_drafts WHERE serial=%s", (str(serial or "").strip(),))


def covered_street(cep: str) -> dict[str, Any] | None:
    digits = "".join(character for character in str(cep) if character.isdigit())
    return _one("SELECT cep,street,city,state,confirmed_by,confirmed_at FROM covered_streets WHERE cep=%s", (digits,))


def confirm_covered_street(cep: str, street: str, city: str, state: str, confirmed_by: str) -> dict[str, Any]:
    digits = "".join(character for character in str(cep) if character.isdigit())
    if len(digits) != 8 or not str(street).strip():
        raise ValueError("Rua invÃ¡lida para confirmaÃ§Ã£o de cobertura")
    _execute(
        """INSERT INTO covered_streets VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT(cep) DO UPDATE SET street=EXCLUDED.street,city=EXCLUDED.city,state=EXCLUDED.state,confirmed_by=EXCLUDED.confirmed_by,confirmed_at=EXCLUDED.confirmed_at""",
        (digits, str(street).strip(), str(city).strip(), str(state).strip(), confirmed_by, time.time()),
    )
    return {"cep": digits, "street": str(street).strip(), "covered": True}


def record_user(username: str, name: str, login: bool = False) -> None:
    username = str(username or "").strip()
    if not username:
        return
    now = time.time()
    current = _one("SELECT first_login,last_login FROM platform_users WHERE lower(username)=lower(%s)", (username,))
    first = float(current["first_login"]) if current and current.get("first_login") else now
    last_login = now if login else float(current["last_login"]) if current and current.get("last_login") else now
    _execute(
        """INSERT INTO platform_users VALUES(%s,%s,%s,%s,%s)
           ON CONFLICT(username) DO UPDATE SET name=EXCLUDED.name,last_login=EXCLUDED.last_login,last_activity=EXCLUDED.last_activity""",
        (username, name or username, first, last_login, now),
    )


def role_for(username: str) -> str:
    row = _one("SELECT role FROM platform_roles WHERE lower(username)=lower(%s)", (username,))
    return str(row["role"]) if row else ""


def set_user_role(username: str, role: str, granted_by: str) -> None:
    if role not in ("technician", "office", "admin", "superadmin", "disabled"):
        raise ValueError("Perfil invÃ¡lido")
    _execute(
        """INSERT INTO platform_roles VALUES(%s,%s,%s,%s)
           ON CONFLICT(username) DO UPDATE SET role=EXCLUDED.role,granted_by=EXCLUDED.granted_by,updated_at=EXCLUDED.updated_at""",
        (username, role, granted_by, time.time()),
    )
    record_user(username, username)


def delete_platform_user(username: str) -> None:
    clean = str(username or "").strip()
    if not clean:
        raise ValueError("Usuário inválido")
    _execute("DELETE FROM route_monitor_members WHERE lower(username)=lower(%s)", (clean,))
    _execute("DELETE FROM popup_access_members WHERE lower(username)=lower(%s)", (clean,))
    _execute("DELETE FROM platform_roles WHERE lower(username)=lower(%s)", (clean,))
    _execute("DELETE FROM platform_users WHERE lower(username)=lower(%s)", (clean,))


def platform_users() -> list[dict[str, Any]]:
    return _rows("""SELECT u.*,COALESCE(r.role,'technician') role,r.granted_by,r.updated_at
                    FROM platform_users u LEFT JOIN platform_roles r ON lower(r.username)=lower(u.username)
                    ORDER BY u.last_activity DESC""")


def route_monitor_members() -> list[dict[str, Any]]:
    return _rows("SELECT username,display_name,granted_by,updated_at FROM route_monitor_members ORDER BY lower(display_name)")


def set_route_monitor_member(username: str, display_name: str, enabled: bool, granted_by: str) -> None:
    clean_username = str(username or "").strip()
    if not clean_username:
        raise ValueError("Usuário inválido")
    if enabled:
        _execute(
            """INSERT INTO route_monitor_members VALUES(%s,%s,%s,%s)
               ON CONFLICT(username) DO UPDATE SET display_name=EXCLUDED.display_name,granted_by=EXCLUDED.granted_by,updated_at=EXCLUDED.updated_at""",
            (clean_username, str(display_name or clean_username).strip(), granted_by, time.time()),
        )
    else:
        _execute("DELETE FROM route_monitor_members WHERE lower(username)=lower(%s)", (clean_username,))


def popup_access_members() -> list[dict[str, Any]]:
    return _rows("SELECT username,granted_by,updated_at FROM popup_access_members ORDER BY lower(username)")


def set_popup_access_member(username: str, enabled: bool, granted_by: str) -> None:
    clean = str(username or "").strip()
    if not clean:
        raise ValueError("Usuário inválido")
    if enabled:
        _execute(
            """INSERT INTO popup_access_members VALUES(%s,%s,%s)
               ON CONFLICT(username) DO UPDATE SET granted_by=EXCLUDED.granted_by,updated_at=EXCLUDED.updated_at""",
            (clean, granted_by, time.time()),
        )
    else:
        _execute("DELETE FROM popup_access_members WHERE lower(username)=lower(%s)", (clean,))


def repeat_refunds() -> list[dict[str, Any]]:
    _execute("DELETE FROM repeat_refunds WHERE refunded_at < %s", (time.time() - 14 * 86400,))
    return _rows("SELECT * FROM repeat_refunds ORDER BY refunded_at DESC")

def equipment_reference(gpon: str = "", mac: str = "") -> dict[str, Any] | None:
    return _one(
        "SELECT * FROM equipment_references WHERE lower(gpon)=lower(%s) OR lower(mac)=lower(%s) LIMIT 1",
        (str(gpon or "").strip(), str(mac or "").strip()),
    )


def save_equipment_reference(
    gpon: str, mac: str | None, product_id: int, stock_id: int,
    purchase_id: str, photo_hash: str, created_by: str,
) -> dict[str, Any]:
    now = time.time()
    _execute(
        "INSERT INTO equipment_references(gpon,mac,product_id,stock_id,purchase_id,photo_hash,created_by,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (gpon, mac, int(product_id), int(stock_id), purchase_id, photo_hash, created_by, now),
    )
    return {"gpon": gpon, "mac": mac, "product_id": int(product_id), "stock_id": int(stock_id), "purchase_id": purchase_id, "created_at": now}


def refund_repeat(original_order_id: int, repeat_order_id: int, technician: str, points: float, refunded_by: str, restored_order_id: int | None = None) -> dict[str, Any]:
    if original_order_id <= 0 or repeat_order_id <= 0 or not technician.strip() or points < 0:
        raise ValueError("Repetida invÃ¡lida para estorno")
    now = time.time()
    _execute(
        """INSERT INTO repeat_refunds(original_order_id,repeat_order_id,technician,points,refunded_by,refunded_at,restored_order_id) VALUES(%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(original_order_id,repeat_order_id,technician)
           DO UPDATE SET points=EXCLUDED.points,refunded_by=EXCLUDED.refunded_by,refunded_at=EXCLUDED.refunded_at,restored_order_id=EXCLUDED.restored_order_id""",
        (original_order_id, repeat_order_id, technician.strip(), points, refunded_by, now, restored_order_id),
    )
    return {"original_order_id": original_order_id, "repeat_order_id": repeat_order_id, "technician": technician.strip(), "points": points, "refunded_by": refunded_by, "refunded_at": now, "restored_order_id": restored_order_id}


def event(os_id: int, technician: str, kind: str, detail: str = "") -> None:
    if _one("SELECT 1 AS found FROM events WHERE os_id=%s AND kind=%s AND created_at>%s", (os_id, kind, time.time() - 30)):
        return
    _execute("INSERT INTO events(os_id,technician,kind,detail,created_at) VALUES(%s,%s,%s,%s,%s)", (os_id, technician, kind, detail, time.time()))


def sync_orders(orders: list[dict[str, Any]]) -> None:
    kinds = {1: "closed", 2: "running", 3: "pending"}
    now = time.time()
    with _connection() as connection:
        for order in orders:
            order_id = int(order.get("os_id") or 0)
            status = int(order.get("os_status") or 0)
            if not order_id:
                continue
            technician = str(order.get("os_tecnico_responsavel") or "Sem tÃ©cnico")
            previous = connection.execute("SELECT status FROM observed_orders WHERE os_id=%s", (order_id,)).fetchone()
            if previous and int(previous[0]) != status and status in kinds:
                connection.execute("INSERT INTO events(os_id,technician,kind,detail,created_at) VALUES(%s,%s,%s,%s,%s)", (order_id, technician, kinds[status], str(order.get("cliente") or ""), now))
            elif not previous and status == 2:
                connection.execute("INSERT INTO events(os_id,technician,kind,detail,created_at) VALUES(%s,%s,%s,%s,%s)", (order_id, technician, "running", str(order.get("cliente") or ""), now))
            connection.execute(
                """INSERT INTO observed_orders VALUES(%s,%s,%s,%s)
                   ON CONFLICT(os_id) DO UPDATE SET status=EXCLUDED.status,technician=EXCLUDED.technician,updated_at=EXCLUDED.updated_at""",
                (order_id, status, technician, now),
            )


def save_completed_orders(orders: list[dict[str, Any]]) -> None:
    with _connection() as connection:
        for item in orders:
            connection.execute(
                """INSERT INTO completed_orders VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(os_id) DO UPDATE SET technician=EXCLUDED.technician,client=EXCLUDED.client,reason=EXCLUDED.reason,opened_at=EXCLUDED.opened_at,scheduled_at=EXCLUDED.scheduled_at,closed_at=EXCLUDED.closed_at,imported_at=EXCLUDED.imported_at""",
                (int(item.get("os_id") or 0), item.get("technician"), item.get("client"), item.get("reason"), item.get("opened_at"), item.get("started_at"), item.get("closed_at"), time.time()),
            )


def completed_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    return _rows(
        """SELECT os_id,technician,client,reason,opened_at,scheduled_at AS started_at,closed_at,1 AS estimated
           FROM completed_orders WHERE substring(closed_at from 1 for 10)>=%s AND substring(closed_at from 1 for 10)<=%s ORDER BY closed_at""",
        (start_date, end_date),
    )


def save_location(technician: str, latitude: float, longitude: float, accuracy: float) -> None:
    _execute(
        """INSERT INTO locations VALUES(%s,%s,%s,%s,%s)
           ON CONFLICT(technician) DO UPDATE SET latitude=EXCLUDED.latitude,longitude=EXCLUDED.longitude,accuracy=EXCLUDED.accuracy,updated_at=EXCLUDED.updated_at""",
        (technician, latitude, longitude, accuracy, time.time()),
    )


def assign(os_id: int, technician: str, assigned_by: str) -> None:
    _execute(
        """INSERT INTO assignments VALUES(%s,%s,%s,%s)
           ON CONFLICT(os_id) DO UPDATE SET technician=EXCLUDED.technician,assigned_by=EXCLUDED.assigned_by,created_at=EXCLUDED.created_at""",
        (os_id, technician, assigned_by, time.time()),
    )
    event(os_id, technician, "assigned", f"Direcionada por {assigned_by}")


def assignments_for(technician: str) -> list[int]:
    return [int(row["os_id"]) for row in _rows("SELECT os_id FROM assignments WHERE lower(technician)=lower(%s)", (technician,))]


def snapshot(selected_date: str | None = None) -> dict[str, list[dict[str, Any]]]:
    if selected_date:
        day = date.fromisoformat(selected_date)
        start = datetime.combine(day, datetime.min.time()).timestamp()
        end = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
        events = _rows("SELECT * FROM events WHERE created_at>=%s AND created_at<%s ORDER BY created_at DESC", (start, end))
    else:
        events = _rows("SELECT * FROM events ORDER BY created_at DESC LIMIT 1000")
    locations = _rows("SELECT * FROM locations ORDER BY updated_at DESC")
    assignments = _rows("SELECT * FROM assignments ORDER BY created_at DESC")
    started: dict[int, float] = {}
    durations: dict[int, float] = {}
    for item in reversed(events):
        if item["kind"] == "running":
            started[item["os_id"]] = item["created_at"]
        elif item["kind"] == "closed" and item["os_id"] in started:
            durations[item["os_id"]] = item["created_at"] - started[item["os_id"]]
    return {"events": events, "locations": locations, "assignments": assignments, "durations": [{"os_id": key, "seconds": value} for key, value in durations.items()]}


def _migrate_sqlite(database: Path) -> None:
    if not database.is_file() or _one("SELECT 1 AS found FROM v2_migrations WHERE name='sqlite-import'"):
        return
    source = sqlite3.connect(database)
    source.row_factory = sqlite3.Row
    tables = {
        "events": ("id","os_id","technician","kind","detail","created_at"),
        "locations": ("technician","latitude","longitude","accuracy","updated_at"),
        "assignments": ("os_id","technician","assigned_by","created_at"),
        "observed_orders": ("os_id","status","technician","updated_at"),
        "completed_orders": ("os_id","technician","client","reason","opened_at","scheduled_at","closed_at","imported_at"),
        "platform_users": ("username","name","first_login","last_login","last_activity"),
        "platform_roles": ("username","role","granted_by","updated_at"),
        "popup_access_members": ("username","granted_by","updated_at"),
        "repeat_refunds": ("original_order_id","repeat_order_id","technician","points","refunded_by","refunded_at","restored_order_id"),
        "equipment_references": ("gpon","mac","product_id","stock_id","purchase_id","photo_hash","created_by","created_at"),
        "onu_relink_drafts": ("serial","order_id","values_json","saved_at"),
        "covered_streets": ("cep","street","city","state","confirmed_by","confirmed_at"),
        "confirmed_removals": ("os_id","technician","client_id","client","serial","return_type","closed_at","evidence_at","history_user","evidence","counted_at","dismissed_at"),
        "return_obligations": ("technician","connectors","modems_json","note","updated_by","updated_at"),
        "connector_returns": ("source_id","os_id","technician","client","product","quantity","original_quantity","used_at","reason","note","counted_at","dismissed_at"),
        "persistent_cache": ("cache_key","value_json","saved_at","accessed_at","expires_at"),
    }
    try:
        with _connection() as target:
            for table, columns in tables.items():
                try:
                    rows = source.execute(f"SELECT {','.join(columns)} FROM {table}").fetchall()
                except sqlite3.OperationalError:
                    continue
                if not rows:
                    continue
                placeholders = ",".join(["%s"] * len(columns))
                with target.cursor() as cursor:
                    cursor.executemany(
                        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders}) ON CONFLICT DO NOTHING",
                        [tuple(row) for row in rows],
                    )
            target.execute("INSERT INTO v2_migrations VALUES('sqlite-import',%s) ON CONFLICT(name) DO NOTHING", (time.time(),))
    finally:
        source.close()


def configure_operations(_attempt=0) -> bool:
    global _pool, POSTGRES_ACTIVE
    url = (
        os.getenv("DATABASE_URL")
        or os.getenv("EBF_DATABASE_URL")
        or ""
    ).strip()
    if not url:
        # Render Free does not provide a persistent database by default.  Keep
        # the existing SQLite store available for this deployment mode; when a
        # DATABASE_URL is supplied we transparently use PostgreSQL instead.
        return False
    if not url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError("DATABASE_URL deve apontar para PostgreSQL; fallback local desabilitado.")
    try:
        _pool = ConnectionPool(url, min_size=1, max_size=8, timeout=10, open=True)
        _pool.wait(timeout=15)
        with _connection() as connection:
            # Multiple web workers must not race CREATE TABLE / RLS migrations.
            connection.execute("SELECT pg_advisory_xact_lock(2026092501)")
            connection.execute(SCHEMA)
            _configure_rls(connection)
            seeded = connection.execute("SELECT 1 FROM v2_migrations WHERE name='route-monitor-members-seed'").fetchone()
            if not seeded:
                defaults = (
                    "Arthur", "Anderson Crepaldi", "Anderson Neis", "Bruno", "Clayton", "DavidR",
                    "Eduardo", "Evertton", "Kaique", "Leonardo", "Paulo", "Silvando",
                )
                with connection.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO route_monitor_members VALUES(%s,%s,'Sistema',0) ON CONFLICT(username) DO NOTHING",
                        [(name, name) for name in defaults],
                    )
                connection.execute("INSERT INTO v2_migrations VALUES('route-monitor-members-seed',%s)", (time.time(),))
            popup_seeded = connection.execute("SELECT 1 FROM v2_migrations WHERE name='popup-supervisor-seed'").fetchone()
            if not popup_seeded:
                connection.execute(
                    "INSERT INTO popup_access_members VALUES('Gustavo','Sistema',%s) ON CONFLICT(username) DO NOTHING",
                    (time.time(),),
                )
                connection.execute("INSERT INTO v2_migrations VALUES('popup-supervisor-seed',%s)", (time.time(),))
        _migrate_sqlite(Path(os.getenv("EBF_DB_PATH", Path(__file__).resolve().parents[1] / "operations.db")))
        with _connection() as connection:
            connection.execute(
                """INSERT INTO platform_roles(username,role,granted_by,updated_at)
                   SELECT username,'technician','Migração',0 FROM platform_users
                   ON CONFLICT(username) DO NOTHING"""
            )
            connection.execute(
                """INSERT INTO platform_roles(username,role,granted_by,updated_at)
                   SELECT username,'disabled','Sistema',%s FROM platform_users
                   WHERE lower(username) IN ('alex','alexo') OR lower(name) LIKE 'alex%%'
                   ON CONFLICT(username) DO UPDATE SET role='disabled',granted_by='Sistema',updated_at=EXCLUDED.updated_at""",
                (time.time(),),
            )
    except Exception as exc:
        if _pool:
            _pool.close()
        _pool = None
        POSTGRES_ACTIVE = False
        if _attempt < 2:
            time.sleep(_attempt + 1)
            return configure_operations(_attempt + 1)
        raise RuntimeError("Banco principal indisponível na inicialização. Fallback local bloqueado para preservar os acessos.") from exc
    import app.operations as operations
    for name in (
        "save_onu_relink_draft","onu_relink_drafts","delete_onu_relink_draft","covered_street",
        "confirm_covered_street","record_user","role_for","set_user_role","delete_platform_user","platform_users","route_monitor_members","set_route_monitor_member","popup_access_members","set_popup_access_member",
        "repeat_refunds","refund_repeat","event","sync_orders","save_completed_orders",
        "equipment_reference","save_equipment_reference",
        "confirmed_removals","expired_removals","delete_expired_removals","delivered_removals","add_manual_removal_payments","exclude_removal_payments","save_confirmed_removals","dismiss_confirmed_removals","upsert_sheet_removal",
        "return_obligations","save_return_obligation","delete_return_obligation",
        "connector_returns","expired_connector_returns","delete_expired_connector_returns","save_connector_returns","upsert_sheet_connector","set_sheet_connector_total","update_connector_return","dismiss_connector_returns",
        "installation_material_alerts","replace_installation_material_alerts","dismiss_installation_material_alerts",
        "record_cto_query","cto_query_history",
        "stock_balance_snapshot_before","save_stock_balance_snapshot",
        "cache_snapshot","save_cache_snapshot","delete_cache_snapshots_except","delete_cache_snapshots_prefix","route_alert_reviews","save_route_alert_review",
        "coverage_buildings","save_coverage_building","delete_coverage_building",
        "coverage_pon_areas","save_coverage_pon_area","delete_coverage_pon_area",
        "tracking_session","tracking_session_for_order","save_tracking_session","delete_tracking_session",
        "completed_between","save_location","assign","assignments_for","snapshot",
    ):
        setattr(operations, name, globals()[name])
    POSTGRES_ACTIVE = True
    return True


def close_operations() -> None:
    global _pool
    if _pool:
        _pool.close()
        _pool = None
