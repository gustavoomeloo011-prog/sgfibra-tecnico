from __future__ import annotations

import base64
import hashlib
import json
import os
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


PREFIX = "enc:v1:"


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    configured = os.getenv("DATA_ENCRYPTION_KEY", "").strip() or os.getenv("SESSION_ENCRYPTION_KEY", "").strip()
    if configured:
        return Fernet(configured.encode())
    seed = (
        os.getenv("SESSION_SECRET")
        or os.getenv("DATABASE_URL")
        or os.getenv("EBF_DATABASE_URL")
        or "ebf-v2-local-development"
    ).encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed).digest()))


def encrypted_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    return PREFIX + _cipher().encrypt(raw).decode()


def decrypted_json(value: Any) -> Any | None:
    text = str(value or "")
    try:
        if text.startswith(PREFIX):
            text = _cipher().decrypt(text[len(PREFIX):].encode()).decode()
        return json.loads(text)
    except (InvalidToken, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
