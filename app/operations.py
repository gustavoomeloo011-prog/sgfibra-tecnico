from __future__ import annotations

import sqlite3
import time
import os
import json
import hashlib
import re
import threading
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.data_crypto import decrypted_json, encrypted_json

DB = Path(os.getenv("EBF_DB_PATH", str(Path(__file__).resolve().parents[1] / "operations.db")))
_DB_INITIALIZED = False
_DB_INIT_LOCK = threading.Lock()


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB, timeout=10, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _initialize_db() -> None:
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    with _DB_INIT_LOCK:
        if _DB_INITIALIZED:
            return
        with _connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
              CREATE TABLE IF NOT EXISTS logistics_cases(id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS logistics_events(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS logistics_events_case ON logistics_events(case_id,created_at);
              CREATE TABLE IF NOT EXISTS logistics_revision(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
              INSERT OR IGNORE INTO logistics_revision(id,revision) VALUES(1,0);
              CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, os_id INTEGER, technician TEXT, kind TEXT, detail TEXT, created_at REAL);
              CREATE TABLE IF NOT EXISTS locations(technician TEXT PRIMARY KEY, latitude REAL, longitude REAL, accuracy REAL, updated_at REAL);
              CREATE TABLE IF NOT EXISTS assignments(os_id INTEGER PRIMARY KEY, technician TEXT, assigned_by TEXT, created_at REAL);
              CREATE TABLE IF NOT EXISTS observed_orders(os_id INTEGER PRIMARY KEY, status INTEGER, technician TEXT, updated_at REAL);
              CREATE TABLE IF NOT EXISTS completed_orders(os_id INTEGER PRIMARY KEY, technician TEXT, client TEXT, reason TEXT, opened_at TEXT, scheduled_at TEXT, closed_at TEXT, imported_at REAL);
              CREATE TABLE IF NOT EXISTS platform_users(username TEXT PRIMARY KEY COLLATE NOCASE, name TEXT, first_login REAL, last_login REAL, last_activity REAL);
              CREATE TABLE IF NOT EXISTS route_monitor_members(username TEXT PRIMARY KEY COLLATE NOCASE, display_name TEXT NOT NULL, granted_by TEXT, updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS popup_access_members(username TEXT PRIMARY KEY COLLATE NOCASE, granted_by TEXT, updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS v2_migrations(name TEXT PRIMARY KEY, applied_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS platform_roles(username TEXT PRIMARY KEY COLLATE NOCASE, role TEXT NOT NULL, granted_by TEXT, updated_at REAL);
              CREATE TABLE IF NOT EXISTS repeat_refunds(original_order_id INTEGER, repeat_order_id INTEGER, technician TEXT COLLATE NOCASE, points REAL, refunded_by TEXT, refunded_at REAL, PRIMARY KEY(original_order_id,repeat_order_id,technician));
              CREATE TABLE IF NOT EXISTS onu_relink_drafts(serial TEXT PRIMARY KEY COLLATE NOCASE, order_id INTEGER, values_json TEXT NOT NULL, saved_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS covered_streets(cep TEXT PRIMARY KEY, street TEXT NOT NULL, city TEXT, state TEXT, confirmed_by TEXT, confirmed_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS equipment_references(gpon TEXT PRIMARY KEY COLLATE NOCASE, mac TEXT UNIQUE COLLATE NOCASE, product_id INTEGER NOT NULL, stock_id INTEGER NOT NULL, purchase_id TEXT, photo_hash TEXT, created_by TEXT, created_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS confirmed_removals(os_id INTEGER PRIMARY KEY, technician TEXT, client_id INTEGER, client TEXT, cpf TEXT, serial TEXT, return_type TEXT, closed_at TEXT, evidence_at TEXT, history_user TEXT, evidence TEXT, counted_at REAL NOT NULL, dismissed_at REAL);
              CREATE TABLE IF NOT EXISTS return_obligations(technician TEXT PRIMARY KEY COLLATE NOCASE, connectors INTEGER NOT NULL DEFAULT 0, modems_json TEXT NOT NULL DEFAULT '[]', note TEXT, updated_by TEXT, updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS connector_returns(source_id TEXT PRIMARY KEY, os_id INTEGER, technician TEXT, client TEXT, product TEXT, quantity REAL NOT NULL, original_quantity REAL NOT NULL, used_at TEXT, reason TEXT, note TEXT, counted_at REAL NOT NULL, dismissed_at REAL);
              CREATE TABLE IF NOT EXISTS installation_material_alerts(os_id INTEGER PRIMARY KEY, technician TEXT, client TEXT, contract_id INTEGER, closed_at TEXT, reason TEXT, checked_at REAL NOT NULL, dismissed_at REAL);
              CREATE TABLE IF NOT EXISTS cto_query_history(id INTEGER PRIMARY KEY AUTOINCREMENT, query_type TEXT NOT NULL, username TEXT NOT NULL, contracts_json TEXT NOT NULL, summary_json TEXT NOT NULL, queried_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS query_leases(name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS persistent_cache(cache_key TEXT PRIMARY KEY, value_json TEXT NOT NULL, saved_at REAL NOT NULL, accessed_at REAL NOT NULL, expires_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS order_text_drafts(os_id INTEGER NOT NULL, username TEXT NOT NULL, text_value TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(os_id,username));
              CREATE TABLE IF NOT EXISTS stock_balance_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, location_id INTEGER NOT NULL, captured_at REAL NOT NULL, items_json TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS stock_balance_snapshots_location ON stock_balance_snapshots(location_id,captured_at);
              CREATE TABLE IF NOT EXISTS route_service_times(service_day TEXT NOT NULL, os_id INTEGER NOT NULL, client_id INTEGER, started_at TEXT, checked_at REAL NOT NULL, PRIMARY KEY(service_day,os_id));
              CREATE TABLE IF NOT EXISTS route_alert_reviews(service_day TEXT NOT NULL, os_id INTEGER NOT NULL, technician TEXT NOT NULL COLLATE NOCASE, comment TEXT NOT NULL, reviewed_by TEXT NOT NULL, reviewed_at REAL NOT NULL, PRIMARY KEY(service_day,os_id,technician));
              CREATE TABLE IF NOT EXISTS coverage_buildings(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', latitude REAL NOT NULL, longitude REAL NOT NULL, updated_by TEXT NOT NULL, updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS tutorials(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, video_url TEXT NOT NULL DEFAULT '', thumbnail_url TEXT NOT NULL DEFAULT '', duration TEXT NOT NULL DEFAULT '', keywords TEXT NOT NULL DEFAULT '', steps TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'draft', created_by TEXT NOT NULL, updated_at REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS idx_tutorials_status_category ON tutorials(status,category);
              CREATE INDEX IF NOT EXISTS idx_connector_returns_used_at ON connector_returns(used_at);
              CREATE INDEX IF NOT EXISTS idx_persistent_cache_expires ON persistent_cache(expires_at);
              CREATE INDEX IF NOT EXISTS idx_route_service_day ON route_service_times(service_day);
              CREATE INDEX IF NOT EXISTS idx_cto_query_history_queried_at ON cto_query_history(queried_at);
              CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
              CREATE INDEX IF NOT EXISTS idx_events_order_kind ON events(os_id,kind,created_at);
              CREATE INDEX IF NOT EXISTS idx_assignments_technician ON assignments(technician COLLATE NOCASE);
              CREATE INDEX IF NOT EXISTS idx_completed_closed_at ON completed_orders(closed_at);
              CREATE INDEX IF NOT EXISTS idx_removals_closed_at ON confirmed_removals(closed_at);
              INSERT OR IGNORE INTO platform_roles(username,role,granted_by,updated_at) VALUES('Gustavo','superadmin','Sistema',0);
              INSERT OR IGNORE INTO platform_roles(username,role,granted_by,updated_at) VALUES('GON','superadmin','Sistema',0);
              INSERT OR IGNORE INTO platform_roles(username,role,granted_by,updated_at) VALUES('Kaionunes','admin','Gustavo',0);
              INSERT OR IGNORE INTO platform_roles(username,role,granted_by,updated_at) VALUES('DavidR','technician','Sistema',0);
              INSERT OR IGNORE INTO platform_roles(username,role,granted_by,updated_at) SELECT username,'technician','Migração',0 FROM platform_users;
              INSERT OR REPLACE INTO platform_roles(username,role,granted_by,updated_at)
                SELECT username,'disabled','Sistema',strftime('%s','now') FROM platform_users
                WHERE lower(username) IN ('alex','alexo') OR lower(name) LIKE 'alex%';
            """)
            route_seed = connection.execute("SELECT 1 FROM v2_migrations WHERE name='route-monitor-members-seed'").fetchone()
            if not route_seed:
                connection.execute("INSERT INTO v2_migrations VALUES('route-monitor-members-seed',?)", (time.time(),))
            cleanup_seed = connection.execute("SELECT 1 FROM v2_migrations WHERE name='sgfibra-route-members-cleanup'").fetchone()
            if not cleanup_seed:
                connection.execute("DELETE FROM route_monitor_members WHERE lower(username) IN (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("arthur", "anderson crepaldi", "anderson neis", "bruno", "clayton", "davidr", "eduardo", "evertton", "kaique", "leonardo", "paulo", "silvando"))
                connection.execute("INSERT INTO v2_migrations VALUES('sgfibra-route-members-cleanup',?)", (time.time(),))
            popup_seed = connection.execute("SELECT 1 FROM v2_migrations WHERE name='popup-supervisor-seed'").fetchone()
            if not popup_seed:
                connection.execute(
                    "INSERT OR IGNORE INTO popup_access_members VALUES('Gustavo','Sistema',?)",
                    (time.time(),),
                )
                connection.execute("INSERT INTO v2_migrations VALUES('popup-supervisor-seed',?)", (time.time(),))
            tutorial_seed = connection.execute("SELECT 1 FROM v2_migrations WHERE name='tutorial-examples-seed'").fetchone()
            if not tutorial_seed:
                examples = [
                    ('Como acessar a plataforma Zapping', 'Exemplo de tutorial aguardando validação interna.', 'Substitua este texto pelas instruções oficiais da empresa.', 'Zapping'),
                    ('Como navegar pelos canais e localizar conteúdos', 'Exemplo de tutorial aguardando validação interna.', 'Substitua este texto pelas instruções oficiais da empresa.', 'Zapping'),
                    ('Orientar cliente sem reprodução da TV', 'Exemplo de tutorial aguardando validação interna.', 'Substitua este texto pelas instruções oficiais da empresa.', 'Atendimento'),
                    ('Verificações iniciais de conexão', 'Exemplo de tutorial aguardando validação interna.', 'Substitua este texto pelas instruções oficiais da empresa.', 'Rede'),
                    ('Utilizar os sistemas internos da empresa', 'Exemplo de tutorial aguardando validação interna.', 'Substitua este texto pelas instruções oficiais da empresa.', 'Sistemas'),
                ]
                connection.executemany("INSERT INTO tutorials(title,summary,content,category,status,created_by,updated_at) VALUES(?,?,?,?,?,?,?)", [(a,b,c,d,'draft','Sistema',time.time()) for a,b,c,d in examples])
                connection.execute("INSERT INTO v2_migrations VALUES('tutorial-examples-seed',?)", (time.time(),))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(repeat_refunds)")}
            if "restored_order_id" not in columns:
                connection.execute("ALTER TABLE repeat_refunds ADD COLUMN restored_order_id INTEGER")
            removal_columns = {row[1] for row in connection.execute("PRAGMA table_info(confirmed_removals)")}
            if "dismissed_at" not in removal_columns:
                connection.execute("ALTER TABLE confirmed_removals ADD COLUMN dismissed_at REAL")
            if "return_type" not in removal_columns:
                connection.execute("ALTER TABLE confirmed_removals ADD COLUMN return_type TEXT")
            if "cpf" not in removal_columns:
                connection.execute("ALTER TABLE confirmed_removals ADD COLUMN cpf TEXT")
            installation_alert_columns = {row[1] for row in connection.execute("PRAGMA table_info(installation_material_alerts)")}
            if "dismissed_at" not in installation_alert_columns:
                connection.execute("ALTER TABLE installation_material_alerts ADD COLUMN dismissed_at REAL")
            connection.execute("""UPDATE confirmed_removals SET return_type=CASE
                WHEN lower(COALESCE(evidence,'')) LIKE '%substitu%' OR lower(COALESCE(evidence,'')) LIKE '%troca:%' THEN 'troca'
                ELSE 'retirada' END WHERE return_type IS NULL OR return_type=''""")
        _DB_INITIALIZED = True


def _db() -> sqlite3.Connection:
    _initialize_db()
    return _connect()


def cache_snapshot(cache_key: str, max_age: int | None = None, retention_seconds: int = 14 * 86400) -> dict[str, Any] | None:
    now = time.time()
    with _db() as db:
        db.execute("DELETE FROM persistent_cache WHERE expires_at < ? AND cache_key NOT IN ('pon-health-scan-v1', 'application-maintenance-v1')", (now,))
        row = db.execute(
            "SELECT value_json,saved_at FROM persistent_cache WHERE cache_key=?", (str(cache_key),),
        ).fetchone()
        if not row or (max_age is not None and now - float(row["saved_at"]) > max_age):
            return None
        db.execute(
            "UPDATE persistent_cache SET accessed_at=?,expires_at=? WHERE cache_key=?",
            (now, now + max(60, int(retention_seconds)), str(cache_key)),
        )
        return decrypted_json(row["value_json"])


def save_cache_snapshot(cache_key: str, value: Any, retention_seconds: int = 14 * 86400) -> None:
    now = time.time()
    encoded = encrypted_json(value)
    with _db() as db:
        db.execute(
            """INSERT OR REPLACE INTO persistent_cache(cache_key,value_json,saved_at,accessed_at,expires_at)
               VALUES(?,?,?,?,?)""",
            (str(cache_key), encoded, now, now, now + max(60, int(retention_seconds))),
        )


def coverage_buildings() -> list[dict[str, Any]]:
    with _db() as db:
        return [dict(row) for row in db.execute("SELECT * FROM coverage_buildings ORDER BY lower(name),id")]


def save_coverage_building(building_id: int | None, name: str, note: str, latitude: float, longitude: float, actor: str) -> dict[str, Any]:
    clean_name = str(name or "").strip()[:160]
    if not clean_name:
        raise ValueError("Informe o nome do prédio.")
    latitude, longitude = float(latitude), float(longitude)
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Latitude ou longitude inválida.")
    now = time.time()
    clean_note = str(note or "").strip()[:1000]
    with _db() as db:
        if building_id:
            db.execute(
                "UPDATE coverage_buildings SET name=?,note=?,latitude=?,longitude=?,updated_by=?,updated_at=? WHERE id=?",
                (clean_name, clean_note, latitude, longitude, actor, now, int(building_id)),
            )
            row = db.execute("SELECT * FROM coverage_buildings WHERE id=?", (int(building_id),)).fetchone()
            if not row:
                raise ValueError("Prédio não encontrado.")
            return dict(row)
        cursor = db.execute(
            "INSERT INTO coverage_buildings(name,note,latitude,longitude,updated_by,updated_at) VALUES(?,?,?,?,?,?)",
            (clean_name, clean_note, latitude, longitude, actor, now),
        )
        return dict(db.execute("SELECT * FROM coverage_buildings WHERE id=?", (cursor.lastrowid,)).fetchone())


def delete_coverage_building(building_id: int) -> None:
    with _db() as db:
        db.execute("DELETE FROM coverage_buildings WHERE id=?", (int(building_id),))


def coverage_pon_areas() -> list[dict[str, Any]]:
    """Return manually saved PON coverage polygons from the local store."""
    with _db() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM coverage_pon_areas ORDER BY lower(olt_name),slot,pon,id"
        )]
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
    coords = geometry.get("coordinates")
    if not isinstance(coords, (list, tuple)) or not coords or not isinstance(coords[0], (list, tuple)) or len(coords[0]) < 4:
        raise ValueError("A área precisa ter pelo menos três pontos.")
    payload = json.dumps(geometry, ensure_ascii=False, separators=(",", ":"))
    now = time.time()
    values = (str(olt_id or "").strip()[:80], str(olt_name or "").strip()[:180], clean_slot, clean_pon,
              str(note or "").strip()[:1000], payload, actor, now)
    with _db() as db:
        if area_id:
            db.execute("UPDATE coverage_pon_areas SET olt_id=?,olt_name=?,slot=?,pon=?,note=?,geometry_json=?,updated_by=?,updated_at=? WHERE id=?", (*values, int(area_id)))
            row = db.execute("SELECT * FROM coverage_pon_areas WHERE id=?", (int(area_id),)).fetchone()
        else:
            cur = db.execute("INSERT INTO coverage_pon_areas(olt_id,olt_name,slot,pon,note,geometry_json,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?)", values)
            row = db.execute("SELECT * FROM coverage_pon_areas WHERE id=?", (cur.lastrowid,)).fetchone()
    if not row:
        raise ValueError("Área não encontrada.")
    result = dict(row)
    result["geometry"] = json.loads(result.pop("geometry_json"))
    return result


def delete_coverage_pon_area(area_id: int) -> None:
    with _db() as db:
        db.execute("DELETE FROM coverage_pon_areas WHERE id=?", (int(area_id),))


def latest_stock_balance_snapshot(location_id: int) -> dict[str, Any] | None:
    with _db() as db:
        row = db.execute(
            "SELECT captured_at,items_json FROM stock_balance_snapshots WHERE location_id=? ORDER BY captured_at DESC,id DESC LIMIT 1",
            (int(location_id),),
        ).fetchone()
    return {"captured_at": float(row["captured_at"]), "items": decrypted_json(row["items_json"])} if row else None


def stock_balance_snapshot_before(location_id: int, cutoff: float) -> dict[str, Any] | None:
    with _db() as db:
        row = db.execute(
            "SELECT captured_at,items_json FROM stock_balance_snapshots "
            "WHERE location_id=? AND captured_at<? ORDER BY captured_at DESC,id DESC LIMIT 1",
            (int(location_id), float(cutoff)),
        ).fetchone()
    return {"captured_at": float(row["captured_at"]), "items": decrypted_json(row["items_json"])} if row else None


def save_stock_balance_snapshot(location_id: int, items: list[dict[str, Any]], captured_at: float) -> None:
    with _db() as db:
        db.execute(
            "INSERT INTO stock_balance_snapshots(location_id,captured_at,items_json) VALUES(?,?,?)",
            (int(location_id), float(captured_at), encrypted_json(items)),
        )
        db.execute("DELETE FROM stock_balance_snapshots WHERE location_id=? AND captured_at<?",
                   (int(location_id), float(captured_at) - 31 * 86400))


def delete_cache_snapshots_except(prefix: str, keep_key: str) -> None:
    """Remove snapshots from previous business days while keeping today's key."""
    with _db() as db:
        db.execute(
            "DELETE FROM persistent_cache WHERE cache_key LIKE ? AND cache_key<>?",
            (f"{prefix}%", str(keep_key)),
        )


def delete_cache_snapshots_prefix(prefix: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM persistent_cache WHERE cache_key LIKE ?", (f"{prefix}%",))


def tracking_identity_secret() -> str:
    """Create once, atomically, in the same store as the protected links."""
    key, now = "tracking-identity-secret-v1", time.time()
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO persistent_cache VALUES(?,?,?,?,?)",
                   (key, encrypted_json({"secret": secrets.token_hex(32)}), now, now, now + 100 * 365 * 86400))
        row = db.execute("SELECT value_json FROM persistent_cache WHERE cache_key=?", (key,)).fetchone()
        value = decrypted_json(row["value_json"])
        if not isinstance(value, dict) or not value.get("secret"):
            raise RuntimeError("Não foi possível ler a chave de acompanhamento.")
        return str(value["secret"])


def tracking_session(token_hash: str) -> dict[str, Any] | None:
    cache_key = f"os-tracking-v1:{token_hash}"
    with _db() as db:
        row = db.execute("SELECT value_json,expires_at FROM persistent_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if not row or float(row["expires_at"]) <= time.time():
            return None
        return decrypted_json(row["value_json"])


def tracking_sessions() -> list[dict[str, Any]]:
    now = time.time()
    with _db() as db:
        rows = db.execute(
            "SELECT value_json FROM persistent_cache WHERE cache_key LIKE 'os-tracking-v1:%' AND expires_at>? ORDER BY saved_at DESC LIMIT 500",
            (now,),
        ).fetchall()
    result = []
    for row in rows:
        value = decrypted_json(row["value_json"])
        if isinstance(value, dict) and value.get("order_id"):
            result.append(value)
    return result


def tracking_session_for_order(order_id: int) -> dict[str, Any] | None:
    index = tracking_session(f"os-index-{int(order_id)}")
    return tracking_session(index.get("token_hash", "")) if index else None


def order_text_draft(os_id: int, username: str) -> dict[str, Any] | None:
    with _db() as db:
        row = db.execute("SELECT text_value,updated_at FROM order_text_drafts WHERE os_id=? AND username=?",
                         (int(os_id), str(username).strip().casefold())).fetchone()
        return dict(row) if row else None


def save_order_text_draft(os_id: int, username: str, text_value: str) -> dict[str, Any]:
    value, updated_at = str(text_value or "")[:4000], time.time()
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO order_text_drafts VALUES(?,?,?,?)",
                   (int(os_id), str(username).strip().casefold(), value, updated_at))
    return {"text_value": value, "updated_at": updated_at}


def delete_order_text_draft(os_id: int, username: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM order_text_drafts WHERE os_id=? AND username=?", (int(os_id), str(username).strip().casefold()))


def save_tracking_session(token_hash: str, value: dict[str, Any], retention_seconds: int = 14 * 86400) -> None:
    save_cache_snapshot(f"os-tracking-v1:{token_hash}", value, retention_seconds)


def delete_tracking_session(token_hash: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM persistent_cache WHERE cache_key=?", (f"os-tracking-v1:{token_hash}",))


def route_service_times(service_day: str, order_ids: list[int]) -> dict[int, str | None]:
    """Return today's cached starts and remove route data from older days."""
    clean = list(dict.fromkeys(int(value) for value in order_ids if int(value) > 0))
    with _db() as db:
        db.execute("DELETE FROM route_service_times WHERE service_day<>?", (service_day,))
        if not clean:
            return {}
        placeholders = ",".join("?" for _ in clean)
        rows = db.execute(
            f"SELECT os_id,started_at FROM route_service_times WHERE service_day=? AND os_id IN ({placeholders})",
            (service_day, *clean),
        )
        return {int(row["os_id"]): row["started_at"] for row in rows}


def save_route_service_times(service_day: str, values: dict[int, tuple[int | None, str | None]]) -> None:
    """Cache both found and absent transitions so the same history is read once per day."""
    now = time.time()
    with _db() as db:
        db.execute("DELETE FROM route_service_times WHERE service_day<>?", (service_day,))
        db.executemany(
            """INSERT OR REPLACE INTO route_service_times(service_day,os_id,client_id,started_at,checked_at)
               VALUES(?,?,?,?,?)""",
            [(service_day, int(os_id), client_id, started_at, now) for os_id, (client_id, started_at) in values.items()],
        )


def route_alert_reviews(service_day: str) -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM route_alert_reviews WHERE service_day=? ORDER BY reviewed_at DESC", (service_day,),
        )
        return [dict(row) for row in rows]


def save_route_alert_review(service_day: str, os_id: int, technician: str, comment: str, reviewed_by: str) -> dict[str, Any]:
    clean_comment = str(comment or "").strip()
    if not clean_comment:
        raise ValueError("O comentário da análise é obrigatório.")
    reviewed_at = time.time()
    with _db() as db:
        db.execute(
            """INSERT OR REPLACE INTO route_alert_reviews(service_day,os_id,technician,comment,reviewed_by,reviewed_at)
               VALUES(?,?,?,?,?,?)""",
            (service_day, int(os_id), technician.strip(), clean_comment, reviewed_by.strip(), reviewed_at),
        )
    return {"service_day": service_day, "os_id": int(os_id), "technician": technician.strip(), "comment": clean_comment, "reviewed_by": reviewed_by.strip(), "reviewed_at": reviewed_at}


def confirmed_removals() -> list[dict[str, Any]]:
    """Return the rolling removal ledger; expiry is coordinated with Google Sheets."""
    with _db() as db:
        rows = db.execute("SELECT * FROM confirmed_removals WHERE dismissed_at IS NULL ORDER BY closed_at DESC, os_id DESC")
        return [dict(row) for row in rows]


def expired_removals() -> list[dict[str, Any]]:
    cutoff = (date.today() - timedelta(days=13)).isoformat()
    delivered_cutoff = time.time() - (14 * 86400)
    with _db() as db:
        rows = db.execute(
            """SELECT * FROM confirmed_removals WHERE
               (dismissed_at > 0 AND dismissed_at < ?) OR
               (dismissed_at < 0 AND -dismissed_at < ?) OR
               (dismissed_at IS NULL AND substr(closed_at,1,10) < ?)""",
            (delivered_cutoff, delivered_cutoff, cutoff),
        )
        return [dict(row) for row in rows]


def delete_expired_removals(order_ids: list[int]) -> int:
    clean = list(dict.fromkeys(int(value) for value in order_ids))
    if not clean:
        return 0
    placeholders = ",".join("?" for _ in clean)
    with _db() as db:
        cursor = db.execute(f"DELETE FROM confirmed_removals WHERE os_id IN ({placeholders})", clean)
        return cursor.rowcount


def delivered_removals() -> list[dict[str, Any]]:
    """Return delivered removals retained for the weekly payment sheets."""
    with _db() as db:
        rows = db.execute("SELECT * FROM confirmed_removals WHERE dismissed_at > 0 ORDER BY dismissed_at DESC, os_id DESC")
        return [dict(row) for row in rows]


def add_manual_removal_payments(technician: str, quantity: int, delivered_at: float, serial: str = "") -> int:
    now = time.time()
    inserted = 0
    with _db() as db:
        for index in range(int(quantity)):
            os_id = -int(now * 1_000_000) - index
            cursor = db.execute(
                """INSERT OR IGNORE INTO confirmed_removals
                   (os_id,technician,client,serial,return_type,closed_at,evidence_at,history_user,evidence,counted_at,dismissed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (os_id, technician, "Lançamento manual", serial or "Manual", "retirada", datetime.fromtimestamp(delivered_at).isoformat(),
                 datetime.fromtimestamp(delivered_at).isoformat(), "Supervisor", "Ajuste manual da planilha", now, delivered_at),
            )
            was_inserted = cursor.rowcount > 0
            inserted += int(was_inserted)
            if not was_inserted:
                db.execute(
                    """UPDATE confirmed_removals SET technician=?,client_id=?,client=?,serial=?,return_type=?,closed_at=?,
                       evidence_at=?,history_user=?,evidence=? WHERE os_id=? AND dismissed_at IS NULL""",
                    (item.get("technician"), item.get("client_id"), item.get("client"), item.get("serial"),
                     item.get("return_type") or "retirada", item.get("closed_at"), item.get("evidence_at"),
                     item.get("history_user"), item.get("evidence"), int(item.get("os_id") or 0)),
                )
    return inserted


def exclude_removal_payments(order_ids: list[int]) -> int:
    clean = list(dict.fromkeys(int(value) for value in order_ids if int(value) != 0))
    if not clean:
        return 0
    placeholders = ",".join("?" for _ in clean)
    with _db() as db:
        cursor = db.execute(
            f"UPDATE confirmed_removals SET dismissed_at=? WHERE os_id IN ({placeholders}) AND dismissed_at > 0",
            (-time.time(), *clean),
        )
        return max(0, cursor.rowcount)


def save_confirmed_removals(items: list[dict[str, Any]]) -> int:
    inserted = 0
    with _db() as db:
        for item in items:
            cursor = db.execute(
                """INSERT OR IGNORE INTO confirmed_removals
                   (os_id,technician,client_id,client,cpf,serial,return_type,closed_at,evidence_at,history_user,evidence,counted_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(item.get("os_id") or 0), item.get("technician"), item.get("client_id"),
                 item.get("client"), item.get("cpf"), item.get("serial"), item.get("return_type") or "retirada", item.get("closed_at"),
                 item.get("evidence_at"), item.get("history_user"), item.get("evidence"), time.time()),
            )
            inserted += int(cursor.rowcount > 0)
    confirmed_removals()
    return inserted


def dismiss_confirmed_removals(order_ids: list[int]) -> int:
    clean = list(dict.fromkeys(int(value) for value in order_ids if int(value) != 0))
    if not clean:
        return 0
    placeholders = ",".join("?" for _ in clean)
    with _db() as db:
        cursor = db.execute(
            f"UPDATE confirmed_removals SET dismissed_at=? WHERE os_id IN ({placeholders}) AND dismissed_at IS NULL",
            (time.time(), *clean),
        )
        return max(0, cursor.rowcount)


def upsert_sheet_removal(item: dict[str, Any]) -> dict[str, Any]:
    sync_id = str(item.get("sync_id") or "").strip()
    serial = str(item.get("serial") or "").strip().upper()
    if not sync_id or not serial:
        raise ValueError("ID_SYNC e GPON são obrigatórios.")
    match = re.fullmatch(r"os-(-?\d+)", sync_id, re.I)
    os_id = int(match.group(1)) if match else -int(hashlib.sha256(sync_id.encode("utf-8")).hexdigest()[:14], 16)
    closed_at = str(item.get("date") or date.today().isoformat())[:10]
    delivered = bool(item.get("delivered"))
    with _db() as db:
        db.execute(
            """INSERT INTO confirmed_removals(os_id,technician,client_id,client,cpf,serial,return_type,closed_at,evidence_at,history_user,evidence,counted_at,dismissed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(os_id) DO UPDATE SET technician=excluded.technician,client=excluded.client,
               cpf=excluded.cpf,serial=excluded.serial,return_type=excluded.return_type,closed_at=excluded.closed_at,
               evidence_at=excluded.evidence_at,dismissed_at=excluded.dismissed_at""",
            (os_id, item.get("technician"), None, item.get("client"), item.get("cpf"), serial,
             str(item.get("return_type") or "retirada").casefold(), closed_at, closed_at, "Google Planilhas",
             f"Sincronizado da planilha: {sync_id}", time.time(), time.time() if delivered else None),
        )
    return {"os_id": os_id, "sync_id": sync_id, "serial": serial}


def return_obligations() -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute("SELECT * FROM return_obligations ORDER BY technician COLLATE NOCASE")
        result = []
        for row in rows:
            item = dict(row)
            try:
                raw = json.loads(item.pop("modems_json") or "[]")
                item["modems"] = [
                    value if isinstance(value, dict) else {"gpon": str(value), "type": "troca"}
                    for value in raw
                ]
            except (TypeError, json.JSONDecodeError):
                item["modems"] = []
            result.append(item)
        return result


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
    with _db() as db:
        db.execute(
            """INSERT OR REPLACE INTO return_obligations(technician,connectors,modems_json,note,updated_by,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (name, int(connectors), json.dumps(clean_modems, ensure_ascii=False), str(note or "").strip(), updated_by, now),
        )
    return {"technician": name, "connectors": int(connectors), "modems": clean_modems, "note": str(note or "").strip(), "updated_by": updated_by, "updated_at": now}


def delete_return_obligation(technician: str) -> int:
    with _db() as db:
        cursor = db.execute("DELETE FROM return_obligations WHERE technician=?", (str(technician or "").strip(),))
        return max(0, cursor.rowcount)


def connector_returns() -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute("SELECT * FROM connector_returns WHERE dismissed_at IS NULL ORDER BY used_at DESC, source_id")
        return [dict(row) for row in rows]


def expired_connector_returns() -> list[dict[str, Any]]:
    cutoff = (date.today() - timedelta(days=13)).isoformat()
    with _db() as db:
        rows = db.execute("SELECT * FROM connector_returns WHERE substr(used_at,1,10) < ?", (cutoff,))
        return [dict(row) for row in rows]


def delete_expired_connector_returns(source_ids: list[str]) -> int:
    clean = list(dict.fromkeys(str(value) for value in source_ids if str(value).strip()))
    if not clean:
        return 0
    placeholders = ",".join("?" for _ in clean)
    with _db() as db:
        cursor = db.execute(f"DELETE FROM connector_returns WHERE source_id IN ({placeholders})", clean)
        return cursor.rowcount


def save_connector_returns(items: list[dict[str, Any]]) -> int:
    inserted = 0
    with _db() as db:
        for item in items:
            quantity = float(item.get("quantity") or 0)
            cursor = db.execute(
                """INSERT OR IGNORE INTO connector_returns(source_id,os_id,technician,client,product,quantity,original_quantity,used_at,reason,note,counted_at,dismissed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (str(item.get("source_id")), item.get("os_id"), item.get("technician"), item.get("client"), item.get("product"), quantity, quantity, item.get("used_at"), item.get("reason"), "", time.time()),
            )
            inserted += int(cursor.rowcount > 0)
    return inserted


def upsert_sheet_connector(item: dict[str, Any]) -> dict[str, Any]:
    sync_id = str(item.get("sync_id") or "").strip()
    source_id = sync_id.removeprefix("connector-").strip()
    if not source_id:
        raise ValueError("ID_SYNC do conector é obrigatório.")
    try:
        quantity = max(0.0, float(item.get("quantity") or 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("Quantidade inválida para o conector.") from exc
    delivered = bool(item.get("delivered")) or quantity == 0
    used_at = str(item.get("date") or date.today().isoformat())[:10]
    with _db() as db:
        db.execute(
            """INSERT INTO connector_returns(source_id,os_id,technician,client,product,quantity,original_quantity,used_at,reason,note,counted_at,dismissed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
               os_id=excluded.os_id,technician=excluded.technician,client=excluded.client,product=excluded.product,
               quantity=excluded.quantity,used_at=excluded.used_at,reason=excluded.reason,note=excluded.note,
               dismissed_at=excluded.dismissed_at""",
            (source_id, item.get("os_id"), item.get("technician"), item.get("client"), item.get("product"),
             quantity, quantity, used_at, item.get("reason"), item.get("note"), time.time(), time.time() if delivered else None),
        )
    return {"source_id": source_id, "sync_id": sync_id, "quantity": quantity}


def set_sheet_connector_total(technician: str, quantity: float) -> dict[str, Any]:
    name = str(technician or "").strip()
    target = max(0.0, float(quantity or 0))
    if not name:
        raise ValueError("Técnico do resumo de conectores é obrigatório.")
    with _db() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT * FROM connector_returns WHERE dismissed_at IS NULL AND lower(technician)=lower(?) ORDER BY used_at,source_id", (name,)
        )]
        remaining = target
        for row in rows:
            current = float(row.get("quantity") or 0)
            kept = min(current, remaining)
            db.execute("UPDATE connector_returns SET quantity=?,dismissed_at=? WHERE source_id=?", (kept, None if kept > 0 else time.time(), row["source_id"]))
            remaining -= kept
        if remaining > 0:
            source_id = "sheet-summary-" + hashlib.sha256(name.casefold().encode()).hexdigest()[:20]
            db.execute(
                """INSERT INTO connector_returns(source_id,technician,client,product,quantity,original_quantity,used_at,reason,note,counted_at,dismissed_at)
                   VALUES(?,?,'Resumo da planilha','Conectores',?,?,?,'Ajuste do total','',?,NULL)
                   ON CONFLICT(source_id) DO UPDATE SET quantity=excluded.quantity,dismissed_at=NULL""",
                (source_id, name, remaining, remaining, date.today().isoformat(), time.time()),
            )
    return {"technician": name, "quantity": target}


def update_connector_return(source_id: str, quantity: float, note: str) -> int:
    with _db() as db:
        cursor = db.execute("UPDATE connector_returns SET quantity=?,note=? WHERE source_id=? AND dismissed_at IS NULL", (float(quantity), str(note or "").strip(), str(source_id)))
        return max(0, cursor.rowcount)


def dismiss_connector_returns(source_ids: list[str]) -> int:
    clean = list(dict.fromkeys(str(value) for value in source_ids if str(value).strip()))
    if not clean: return 0
    placeholders = ",".join("?" for _ in clean)
    with _db() as db:
        cursor = db.execute(f"UPDATE connector_returns SET dismissed_at=? WHERE source_id IN ({placeholders}) AND dismissed_at IS NULL", (time.time(), *clean))
        return max(0, cursor.rowcount)


def installation_material_alerts() -> list[dict[str, Any]]:
    with _db() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM installation_material_alerts WHERE dismissed_at IS NULL ORDER BY closed_at DESC, os_id DESC"
        )]


def dismiss_installation_material_alerts(order_ids: list[int]) -> int:
    clean = list(dict.fromkeys(int(value) for value in order_ids if int(value) > 0))
    if not clean:
        return 0
    placeholders = ",".join("?" for _ in clean)
    with _db() as db:
        cursor = db.execute(
            f"UPDATE installation_material_alerts SET dismissed_at=? WHERE os_id IN ({placeholders}) AND dismissed_at IS NULL",
            (time.time(), *clean),
        )
        return max(0, cursor.rowcount)


def record_cto_query(query_type: str, username: str, contracts: list[int], summary: dict[str, Any]) -> None:
    now = time.time()
    with _db() as db:
        db.execute("DELETE FROM cto_query_history WHERE queried_at < ?", (now - 14 * 86400,))
        db.execute(
            "INSERT INTO cto_query_history(query_type,username,contracts_json,summary_json,queried_at) VALUES(?,?,?,?,?)",
            (str(query_type), str(username), json.dumps(contracts), json.dumps(summary, ensure_ascii=False, default=str), now),
        )


def cto_query_history() -> list[dict[str, Any]]:
    cutoff = time.time() - 14 * 86400
    with _db() as db:
        db.execute("DELETE FROM cto_query_history WHERE queried_at < ?", (cutoff,))
        rows = db.execute("SELECT * FROM cto_query_history ORDER BY queried_at DESC, id DESC")
        result = []
        for row in rows:
            item = dict(row)
            try: item["contracts"] = json.loads(item.pop("contracts_json") or "[]")
            except (TypeError, json.JSONDecodeError): item["contracts"] = []
            try: item["summary"] = json.loads(item.pop("summary_json") or "{}")
            except (TypeError, json.JSONDecodeError): item["summary"] = {}
            result.append(item)
        return result


def replace_installation_material_alerts(selected_day: date, items: list[dict[str, Any]]) -> int:
    prefix = selected_day.isoformat()
    with _db() as db:
        # Keep validated rows as tombstones so a later SGP refresh does not
        # recreate an alert the supervisor has already checked.
        db.execute(
            "DELETE FROM installation_material_alerts WHERE substr(closed_at,1,10)=? AND dismissed_at IS NULL",
            (prefix,),
        )
        for item in items:
            db.execute(
                """INSERT OR IGNORE INTO installation_material_alerts
                   (os_id,technician,client,contract_id,closed_at,reason,checked_at,dismissed_at)
                   VALUES(?,?,?,?,?,?,?,NULL)""",
                (int(item["os_id"]), item.get("technician"), item.get("client"), item.get("contract_id"), item.get("closed_at"), item.get("reason"), time.time()),
            )
        row = db.execute(
            "SELECT COUNT(*) AS total FROM installation_material_alerts WHERE substr(closed_at,1,10)=? AND dismissed_at IS NULL",
            (prefix,),
        ).fetchone()
        return int(row["total"] or 0)


def save_onu_relink_draft(serial: str, order_id: int, values: dict[str, Any]) -> None:
    serial = str(serial or "").strip()
    if not serial:
        return
    safe_values = {
        str(key): str(value)
        for key, value in values.items()
        if value is not None and str(value).strip()
    }
    with _db() as db:
        db.execute(
            "INSERT OR REPLACE INTO onu_relink_drafts(serial,order_id,values_json,saved_at) VALUES(?,?,?,?)",
            (serial, int(order_id or 0), json.dumps(safe_values, ensure_ascii=False), time.time()),
        )


def onu_relink_drafts() -> dict[str, dict[str, Any]]:
    cutoff = time.time() - 30 * 24 * 60 * 60
    with _db() as db:
        db.execute("DELETE FROM onu_relink_drafts WHERE saved_at < ?", (cutoff,))
        rows = db.execute("SELECT serial,order_id,values_json,saved_at FROM onu_relink_drafts")
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                values = json.loads(row["values_json"])
            except (TypeError, json.JSONDecodeError):
                values = {}
            result[str(row["serial"]).casefold()] = {
                "order_id": row["order_id"], "values": values, "saved_at": row["saved_at"]
            }
        return result


def delete_onu_relink_draft(serial: str) -> None:
    with _db() as db:
        db.execute("DELETE FROM onu_relink_drafts WHERE serial=?", (str(serial or "").strip(),))


def covered_street(cep: str) -> dict[str, Any] | None:
    with _db() as db:
        row = db.execute(
            "SELECT cep,street,city,state,confirmed_by,confirmed_at FROM covered_streets WHERE cep=?",
            ("".join(character for character in str(cep) if character.isdigit()),),
        ).fetchone()
        return dict(row) if row else None


def confirm_covered_street(
    cep: str, street: str, city: str, state: str, confirmed_by: str
) -> dict[str, Any]:
    digits = "".join(character for character in str(cep) if character.isdigit())
    if len(digits) != 8 or not str(street).strip():
        raise ValueError("Rua inválida para confirmação de cobertura")
    now = time.time()
    with _db() as db:
        db.execute(
            "INSERT OR REPLACE INTO covered_streets(cep,street,city,state,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?)",
            (digits, str(street).strip(), str(city).strip(), str(state).strip(), confirmed_by, now),
        )
    return {"cep": digits, "street": str(street).strip(), "covered": True}


def record_user(username: str, name: str, login: bool = False) -> None:
    username = str(username or "").strip()
    if not username:
        return
    now = time.time()
    with _db() as db:
        existing = db.execute(
            "SELECT first_login,last_login FROM platform_users WHERE username=?", (username,)
        ).fetchone()
        first = float(existing["first_login"]) if existing and existing["first_login"] else now
        last_login = now if login else (
            float(existing["last_login"]) if existing and existing["last_login"] else now
        )
        db.execute(
            "INSERT OR REPLACE INTO platform_users(username,name,first_login,last_login,last_activity) VALUES(?,?,?,?,?)",
            (username, name or username, first, last_login, now),
        )


def role_for(username: str) -> str:
    with _db() as db:
        row = db.execute("SELECT role FROM platform_roles WHERE username=?", (username,)).fetchone()
        return str(row[0]) if row else ""


def list_tutorials(include_drafts: bool = False) -> list[dict[str, Any]]:
    with _db() as db:
        query = "SELECT * FROM tutorials"
        params: tuple[Any, ...] = ()
        if not include_drafts:
            query += " WHERE status='published'"
        query += " ORDER BY lower(category), lower(title), id"
        rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def save_tutorial(item: dict[str, Any], tutorial_id: int | None, actor: str) -> dict[str, Any]:
    title = str(item.get('title') or '').strip()[:180]
    category = str(item.get('category') or '').strip()[:80]
    if not title or not category:
        raise ValueError('Título e categoria são obrigatórios.')
    status = str(item.get('status') or 'draft').strip().lower()
    if status not in ('draft', 'published'):
        raise ValueError('Status inválido.')
    values = (title, str(item.get('summary') or '').strip()[:500], str(item.get('content') or '').strip()[:12000], category,
              str(item.get('video_url') or '').strip()[:1000], str(item.get('thumbnail_url') or '').strip()[:1000],
              str(item.get('duration') or '').strip()[:30], str(item.get('keywords') or '').strip()[:500],
              str(item.get('steps') or '').strip()[:8000], str(item.get('notes') or '').strip()[:3000], status)
    now = time.time()
    with _db() as db:
        if tutorial_id:
            db.execute("UPDATE tutorials SET title=?,summary=?,content=?,category=?,video_url=?,thumbnail_url=?,duration=?,keywords=?,steps=?,notes=?,status=?,updated_at=? WHERE id=?", (*values, now, int(tutorial_id)))
            row = db.execute("SELECT * FROM tutorials WHERE id=?", (int(tutorial_id),)).fetchone()
        else:
            cur = db.execute("INSERT INTO tutorials(title,summary,content,category,video_url,thumbnail_url,duration,keywords,steps,notes,status,created_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, actor, now))
            row = db.execute("SELECT * FROM tutorials WHERE id=?", (cur.lastrowid,)).fetchone()
        if not row:
            raise ValueError('Tutorial não encontrado.')
        return dict(row)


def delete_tutorial(tutorial_id: int) -> None:
    with _db() as db:
        db.execute('DELETE FROM tutorials WHERE id=?', (int(tutorial_id),))


def set_user_role(username: str, role: str, granted_by: str) -> None:
    if role not in ("technician", "office", "admin", "superadmin", "disabled"): raise ValueError("Perfil inválido")
    with _db() as db:
        db.execute(
            "INSERT OR REPLACE INTO platform_roles VALUES(?,?,?,?)",
            (username, role, granted_by, time.time()),
        )
    record_user(username, username, login=False)


def delete_platform_user(username: str) -> None:
    clean = str(username or "").strip()
    if not clean:
        raise ValueError("Usuário inválido")
    with _db() as db:
        db.execute("DELETE FROM route_monitor_members WHERE username=?", (clean,))
        db.execute("DELETE FROM popup_access_members WHERE username=?", (clean,))
        db.execute("DELETE FROM platform_roles WHERE username=?", (clean,))
        db.execute("DELETE FROM platform_users WHERE username=?", (clean,))


def platform_users() -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute("SELECT u.*,COALESCE(r.role,'technician') role,r.granted_by,r.updated_at FROM platform_users u LEFT JOIN platform_roles r ON lower(r.username)=lower(u.username) ORDER BY u.last_activity DESC")
        return [dict(row) for row in rows]


def route_monitor_members() -> list[dict[str, Any]]:
    with _db() as db:
        return [dict(row) for row in db.execute(
            "SELECT username,display_name,granted_by,updated_at FROM route_monitor_members ORDER BY display_name COLLATE NOCASE"
        )]


def set_route_monitor_member(username: str, display_name: str, enabled: bool, granted_by: str) -> None:
    clean_username = str(username or "").strip()
    if not clean_username:
        raise ValueError("Usuário inválido")
    with _db() as db:
        if enabled:
            db.execute(
                "INSERT OR REPLACE INTO route_monitor_members VALUES(?,?,?,?)",
                (clean_username, str(display_name or clean_username).strip(), granted_by, time.time()),
            )
        else:
            db.execute("DELETE FROM route_monitor_members WHERE username=?", (clean_username,))


def popup_access_members() -> list[dict[str, Any]]:
    with _db() as db:
        return [dict(row) for row in db.execute(
            "SELECT username,granted_by,updated_at FROM popup_access_members ORDER BY username COLLATE NOCASE"
        )]


def set_popup_access_member(username: str, enabled: bool, granted_by: str) -> None:
    clean = str(username or "").strip()
    if not clean:
        raise ValueError("Usuário inválido")
    with _db() as db:
        if enabled:
            db.execute("INSERT OR REPLACE INTO popup_access_members VALUES(?,?,?)", (clean, granted_by, time.time()))
        else:
            db.execute("DELETE FROM popup_access_members WHERE username=?", (clean,))


def repeat_refunds() -> list[dict[str, Any]]:
    cutoff = time.time() - 14 * 24 * 60 * 60
    with _db() as db:
        db.execute("DELETE FROM repeat_refunds WHERE refunded_at < ?", (cutoff,))
        rows = db.execute(
            "SELECT original_order_id,repeat_order_id,technician,points,refunded_by,refunded_at,restored_order_id FROM repeat_refunds ORDER BY refunded_at DESC"
        )
        return [dict(row) for row in rows]

def equipment_reference(gpon: str = "", mac: str = "") -> dict[str, Any] | None:
    with _db() as db:
        row = db.execute(
            "SELECT * FROM equipment_references WHERE gpon=? OR mac=? LIMIT 1",
            (str(gpon or "").strip(), str(mac or "").strip()),
        ).fetchone()
        return dict(row) if row else None


def save_equipment_reference(
    gpon: str, mac: str | None, product_id: int, stock_id: int,
    purchase_id: str, photo_hash: str, created_by: str,
) -> dict[str, Any]:
    now = time.time()
    with _db() as db:
        db.execute(
            "INSERT INTO equipment_references(gpon,mac,product_id,stock_id,purchase_id,photo_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (gpon, mac, int(product_id), int(stock_id), purchase_id, photo_hash, created_by, now),
        )
    return {"gpon": gpon, "mac": mac, "product_id": int(product_id), "stock_id": int(stock_id), "purchase_id": purchase_id, "created_at": now}


def refund_repeat(original_order_id: int, repeat_order_id: int, technician: str, points: float, refunded_by: str, restored_order_id: int | None = None) -> dict[str, Any]:
    if original_order_id <= 0 or repeat_order_id <= 0 or not technician.strip() or points < 0:
        raise ValueError("Repetida inválida para estorno")
    now = time.time()
    with _db() as db:
        db.execute(
            "INSERT OR REPLACE INTO repeat_refunds(original_order_id,repeat_order_id,technician,points,refunded_by,refunded_at,restored_order_id) VALUES(?,?,?,?,?,?,?)",
            (original_order_id, repeat_order_id, technician.strip(), points, refunded_by, now, restored_order_id),
        )
    return {
        "original_order_id": original_order_id,
        "repeat_order_id": repeat_order_id,
        "technician": technician.strip(),
        "points": points,
        "refunded_by": refunded_by,
        "refunded_at": now,
        "restored_order_id": restored_order_id,
    }


def event(os_id: int, technician: str, kind: str, detail: str = "") -> None:
    with _db() as db:
        recent = db.execute("SELECT 1 FROM events WHERE os_id=? AND kind=? AND created_at>?", (os_id, kind, time.time() - 30)).fetchone()
        if recent: return
        db.execute("INSERT INTO events(os_id,technician,kind,detail,created_at) VALUES(?,?,?,?,?)", (os_id, technician, kind, detail, time.time()))


def sync_orders(orders: list[dict[str, Any]]) -> None:
    kinds = {1: "closed", 2: "running", 3: "pending"}
    now = time.time()
    with _db() as db:
        for order in orders:
            order_id = int(order.get("os_id") or 0); status = int(order.get("os_status") or 0)
            if not order_id: continue
            technician = str(order.get("os_tecnico_responsavel") or "Sem técnico")
            previous = db.execute("SELECT status FROM observed_orders WHERE os_id=?", (order_id,)).fetchone()
            if previous and int(previous[0]) != status and status in kinds:
                db.execute("INSERT INTO events(os_id,technician,kind,detail,created_at) VALUES(?,?,?,?,?)", (order_id, technician, kinds[status], str(order.get("cliente") or ""), now))
            elif not previous and status == 2:
                db.execute("INSERT INTO events(os_id,technician,kind,detail,created_at) VALUES(?,?,?,?,?)", (order_id, technician, "running", str(order.get("cliente") or ""), now))
            db.execute("INSERT OR REPLACE INTO observed_orders VALUES(?,?,?,?)", (order_id, status, technician, now))


def save_completed_orders(orders: list[dict[str, Any]]) -> None:
    with _db() as db:
        for item in orders:
            db.execute("INSERT OR REPLACE INTO completed_orders VALUES(?,?,?,?,?,?,?,?)", (
                int(item.get("os_id") or 0), item.get("technician"), item.get("client"), item.get("reason"),
                item.get("opened_at"), item.get("started_at"), item.get("closed_at"), time.time(),
            ))


def completed_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    with _db() as db:
        rows = db.execute("SELECT os_id,technician,client,reason,opened_at,scheduled_at AS started_at,closed_at,1 AS estimated FROM completed_orders WHERE substr(closed_at,1,10)>=? AND substr(closed_at,1,10)<=? ORDER BY closed_at", (start_date, end_date))
        return [dict(row) for row in rows]


def save_location(technician: str, latitude: float, longitude: float, accuracy: float) -> None:
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO locations VALUES(?,?,?,?,?)", (technician, latitude, longitude, accuracy, time.time()))


def assign(os_id: int, technician: str, assigned_by: str) -> None:
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO assignments VALUES(?,?,?,?)", (os_id, technician, assigned_by, time.time()))
    event(os_id, technician, "assigned", f"Direcionada por {assigned_by}")


def assignments_for(technician: str) -> list[int]:
    with _db() as db:
        return [int(row[0]) for row in db.execute("SELECT os_id FROM assignments WHERE lower(technician)=lower(?)", (technician,))]


def snapshot(selected_date: str | None = None) -> dict[str, list[dict[str, Any]]]:
    with _db() as db:
        if selected_date:
            day = date.fromisoformat(selected_date)
            start = datetime.combine(day, datetime.min.time()).timestamp()
            end = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
            events = [dict(row) for row in db.execute("SELECT * FROM events WHERE created_at>=? AND created_at<? ORDER BY created_at DESC", (start, end))]
        else:
            events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 1000")]
        locations = [dict(row) for row in db.execute("SELECT * FROM locations ORDER BY updated_at DESC")]
        assignments = [dict(row) for row in db.execute("SELECT * FROM assignments ORDER BY created_at DESC")]
    started: dict[int, float] = {}
    durations: dict[int, float] = {}
    for item in reversed(events):
        if item["kind"] == "running": started[item["os_id"]] = item["created_at"]
        elif item["kind"] == "closed" and item["os_id"] in started:
            durations[item["os_id"]] = item["created_at"] - started[item["os_id"]]
    return {"events": events, "locations": locations, "assignments": assignments, "durations": [{"os_id": key, "seconds": value} for key, value in durations.items()]}
