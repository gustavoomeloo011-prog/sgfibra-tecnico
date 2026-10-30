from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from redis.asyncio import Redis


def _fernet() -> Fernet:
    configured = os.getenv("SESSION_ENCRYPTION_KEY", "").strip()
    if configured:
        key = configured.encode()
    else:
        # Render always supplies DATABASE_URL.  Using it as the last production
        # fallback avoids encrypting persisted sessions with a public constant
        # when SESSION_SECRET was accidentally omitted.
        seed = (
            os.getenv("SESSION_SECRET")
            or os.getenv("DATABASE_URL")
            or os.getenv("EBF_DATABASE_URL")
            or "ebf-v2-local-development"
        ).encode()
        key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
    return Fernet(key)


class StateStore:
    def __init__(self) -> None:
        self.redis_url = (
            os.getenv("VALKEY_URL")
            or os.getenv("REDIS_URL")
            or os.getenv("EBF_REDIS_URL")
        )
        self.redis: Redis | None = None
        self.memory: dict[str, tuple[float, bytes]] = {}
        self.lock = asyncio.Lock()
        self.cipher = _fernet()

    async def connect(self) -> None:
        if not self.redis_url:
            return
        candidate = Redis.from_url(self.redis_url, decode_responses=False, max_connections=100)
        try:
            await candidate.ping()
            self.redis = candidate
        except Exception:
            await candidate.aclose()
            self.redis = None

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()

    async def _disable_redis(self) -> None:
        candidate, self.redis = self.redis, None
        if candidate:
            try:
                await candidate.aclose()
            except Exception:
                pass

    def _encode(self, value: Any) -> bytes:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode()
        return self.cipher.encrypt(raw)

    def _decode(self, value: bytes | None) -> Any | None:
        if not value:
            return None
        try:
            return json.loads(self.cipher.decrypt(value).decode())
        except (InvalidToken, ValueError, json.JSONDecodeError):
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        encoded = self._encode(value)
        if self.redis:
            try:
                await self.redis.set(key, encoded, ex=max(1, ttl))
                return
            except Exception:
                await self._disable_redis()
        async with self.lock:
            self.memory[key] = (time.time() + ttl, encoded)

    async def get(self, key: str) -> Any | None:
        if self.redis:
            try:
                return self._decode(await self.redis.get(key))
            except Exception:
                await self._disable_redis()
        async with self.lock:
            item = self.memory.get(key)
            if not item:
                return None
            if item[0] <= time.time():
                self.memory.pop(key, None)
                return None
            return self._decode(item[1])

    async def delete(self, key: str) -> None:
        if self.redis:
            try:
                await self.redis.delete(key)
                return
            except Exception:
                await self._disable_redis()
        async with self.lock:
            self.memory.pop(key, None)

    async def delete_prefix(self, prefix: str) -> None:
        if self.redis:
            try:
                cursor = 0
                while True:
                    cursor, keys = await self.redis.scan(cursor, match=f"{prefix}*", count=200)
                    if keys:
                        await self.redis.delete(*keys)
                    if cursor == 0:
                        break
                return
            except Exception:
                await self._disable_redis()
        async with self.lock:
            for key in [key for key in self.memory if key.startswith(prefix)]:
                self.memory.pop(key, None)

    async def acquire(self, key: str, ttl: int = 30) -> bool:
        if self.redis:
            try:
                return bool(await self.redis.set(f"lock:{key}", b"1", ex=ttl, nx=True))
            except Exception:
                await self._disable_redis()
        async with self.lock:
            lock_key = f"lock:{key}"
            current = self.memory.get(lock_key)
            if current and current[0] > time.time():
                return False
            self.memory[lock_key] = (time.time() + ttl, b"")
            return True

    async def release(self, key: str) -> None:
        await self.delete(f"lock:{key}")

    async def acquire_owned(self, key: str, token: str, ttl: int) -> bool:
        if self.redis:
            return bool(await self.redis.set(f"lock:{key}", token.encode(), ex=ttl, nx=True))
        from app_fastapi.logistics import query_lease
        return await asyncio.to_thread(query_lease, key, token, ttl, True)

    async def maintain_owned(self, key: str, token: str, ttl: int = 0) -> bool:
        """Renew/release only the lock owned by this operation, never a successor."""
        if self.redis:
            script = "if redis.call('get',KEYS[1]) == ARGV[1] then "
            script += "return redis.call('expire',KEYS[1],ARGV[2])" if ttl else "return redis.call('del',KEYS[1])"
            return bool(await self.redis.eval(script + " else return 0 end", 1, f"lock:{key}", token, ttl))
        from app_fastapi.logistics import query_lease
        return await asyncio.to_thread(query_lease, key, token, ttl, False)

    async def create_session(self, value: dict[str, Any], ttl: int) -> str:
        session_id = secrets.token_urlsafe(32)
        value = {**value, "session_id": session_id, "ttl": ttl, "expires": value.get("expires") or time.time() + ttl}
        await self.save_session(session_id, value)
        return session_id

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        from app_fastapi import postgres_operations as pg
        if pg.POSTGRES_ACTIVE:
            key = hashlib.sha256(session_id.encode()).hexdigest()
            record = await asyncio.to_thread(pg.session_record, key)
            if record is not None:
                if record['expires_at'] <= time.time() or not record['payload']:
                    return None
                value = self._decode(bytes(record['payload']))
                return value if isinstance(value, dict) else None
            # Migrate existing encrypted Redis sessions without forcing login.
            value = await self.get(f"session:{session_id}")
            if not isinstance(value, dict) or float(value.get('expires') or 0) <= time.time():
                return None
            await self.save_session(session_id, value)
            # Re-read in case logout inserted a tombstone during migration.
            return await self.get_session(session_id)
        value = await self.get(f"session:{session_id}")
        return value if isinstance(value, dict) else None

    async def save_session(self, session_id: str, value: dict[str, Any]) -> None:
        from app_fastapi import postgres_operations as pg
        if pg.POSTGRES_ACTIVE:
            expires = float(value.get('expires') or time.time() + int(value.get('ttl') or 43200))
            await asyncio.to_thread(pg.save_session_record, hashlib.sha256(session_id.encode()).hexdigest(), self._encode(value), expires)
            return
        await self.set(f"session:{session_id}", value, int(value.get("ttl") or 43200))

    async def delete_session(self, session_id: str) -> None:
        from app_fastapi import postgres_operations as pg
        if pg.POSTGRES_ACTIVE:
            await asyncio.to_thread(pg.revoke_session_record, hashlib.sha256(session_id.encode()).hexdigest())
        await self.delete(f"session:{session_id}")
        await self.delete_prefix(f"cache:{session_id}:")

    async def active_sessions(self) -> list[dict[str, Any]]:
        from app_fastapi import postgres_operations as pg
        if pg.POSTGRES_ACTIVE:
            rows = await asyncio.to_thread(pg.active_session_records)
            return [value for row in rows if isinstance((value := self._decode(bytes(row['payload']))), dict)]
        if self.redis:
            try:
                result: list[dict[str, Any]] = []
                cursor = 0
                while True:
                    cursor, keys = await self.redis.scan(cursor, match="session:*", count=200)
                    if keys:
                        values = await self.redis.mget(keys)
                        result.extend(item for raw in values if isinstance((item := self._decode(raw)), dict))
                    if cursor == 0:
                        break
                return result
            except Exception:
                await self._disable_redis()
        async with self.lock:
            now = time.time()
            return [
                item for expires, raw in self.memory.values()
                if expires > now and isinstance((item := self._decode(raw)), dict) and item.get("session_id")
            ]


store = StateStore()

