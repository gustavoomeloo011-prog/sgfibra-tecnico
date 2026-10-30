"""Persistent, encrypted discrepancy conversations, separate from disposable query caches."""
import time
import uuid
import hashlib
from contextlib import contextmanager

from app import operations
from app.data_crypto import encrypted_json, decrypted_json
from app_fastapi import postgres_operations as pg


@contextmanager
def database():
    with (pg._connection() if pg.POSTGRES_ACTIVE else operations._db()) as connection:
        if pg.POSTGRES_ACTIVE:
            with connection.cursor(row_factory=pg.dict_row) as cursor:
                def execute(query, params=()):
                    return cursor.execute(query.replace("?", "%s"), params)
                yield execute
        else:
            yield connection.execute


def revision(bump=False):
    with database() as execute:
        if bump:
            execute("UPDATE logistics_revision SET revision=revision+1 WHERE id=1")
        return int(execute("SELECT revision FROM logistics_revision WHERE id=1").fetchone()["revision"])


def query_lease(key, owner, ttl, acquire=False):
    """Shared database fallback so multiple web workers cannot query CTO together."""
    now = time.time()
    with database() as execute:
        if acquire:
            result = execute("""INSERT INTO query_leases(name,owner,expires_at) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at
                WHERE query_leases.expires_at < ?""", (key, owner, now + ttl, now))
        elif ttl:
            result = execute("UPDATE query_leases SET expires_at=? WHERE name=? AND owner=?", (now + ttl, key, owner))
        else:
            result = execute("DELETE FROM query_leases WHERE name=? AND owner=?", (key, owner))
        return result.rowcount > 0


def record(execute, case_id, actor, value):
    execute("INSERT INTO logistics_events(id,case_id,actor,payload,created_at) VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, case_id, actor, encrypted_json(value), time.time()))


def audit(case_id, actor, value):
    with database() as execute:
        record(execute, case_id, actor, value)


def audit_history(offset=0):
    with database() as execute:
        rows = execute("SELECT case_id,actor,payload,created_at FROM logistics_events ORDER BY created_at DESC LIMIT 101 OFFSET ?",
                       (offset,)).fetchall()
        return {"more": len(rows) > 100, "items": [
            {"source": row["case_id"], "actor": row["actor"], "created_at": row["created_at"],
             "detail": decrypted_json(row["payload"])} for row in rows[:100]]}


def create(owner, actor, subject, message):
    identifier = uuid.uuid4().hex
    with database() as execute:
        execute("INSERT INTO logistics_cases(id,owner,status,payload,updated_at) VALUES(?,?,?,?,?)",
                (identifier, owner.casefold(), "open", encrypted_json({"subject": subject}), time.time()))
        record(execute, identifier, actor, {"message": message, "status": "open"})
    return identifier


def alert(source, subject, message):
    """Deduplicate automatic findings without deleting supervisor decisions on refresh."""
    identifier = hashlib.sha256(source.encode()).hexdigest()
    with database() as execute:
        inserted = execute("INSERT INTO logistics_cases(id,owner,status,payload,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                           (identifier, "supervisao", "open", encrypted_json({"subject": subject}), time.time()))
        if inserted.rowcount:
            record(execute, identifier, "Sistema", {"message": message, "status": "open", "source": source})
    return identifier


def list_cases(owner=None):
    with database() as execute:
        rows = execute("SELECT * FROM logistics_cases" + (" WHERE owner=?" if owner else "") +
                       " ORDER BY CASE WHEN status='resolved' THEN 1 ELSE 0 END, updated_at DESC LIMIT 300", (owner.casefold(),) if owner else ()).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.update(decrypted_json(item.pop("payload")))
            events = execute("SELECT actor,payload,created_at FROM logistics_events WHERE case_id=? ORDER BY created_at",
                             (item["id"],)).fetchall()
            item["events"] = [{"actor": event["actor"], "created_at": event["created_at"],
                               **decrypted_json(event["payload"])} for event in events]
            result.append(item)
        return result


def respond(identifier, actor, message, status, supervisor=False):
    with database() as execute:
        row = execute("SELECT * FROM logistics_cases WHERE id=?", (identifier,)).fetchone()
        if not row or (not supervisor and row["owner"] != actor.casefold()):
            raise LookupError("Pendência não encontrada.")
        new_status = status if supervisor else "review"
        if new_status not in ("open", "review", "resolved"):
            raise ValueError("Situação inválida.")
        execute("UPDATE logistics_cases SET status=?,updated_at=? WHERE id=?", (new_status, time.time(), identifier))
        record(execute, identifier, actor, {"message": message, "before": row["status"], "status": new_status})
