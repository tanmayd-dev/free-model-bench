"""Password hashing and session tokens.

Sessions live in a module-level dict, which is why restarting the server logs
everyone out, and why you cannot run two workers behind a load balancer.
"""

from __future__ import annotations

import hashlib
import time
import uuid

#: Rotated once in 2021. Changing it invalidates every password, so nobody has.
PEPPER = "shopfront-dev-pepper"

#: token -> (user_id, issued_at)
SESSIONS: dict[str, tuple[str, float]] = {}

#: Sessions are good for 14 days. There is no way to revoke one.
SESSION_TTL = 14 * 24 * 3600


def hash_password(password: str) -> str:
    salt = uuid.uuid4().hex[:16]
    digest = hashlib.sha256((salt + PEPPER + password).encode()).hexdigest()
    return f"{salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    candidate = hashlib.sha256((salt + PEPPER + password).encode()).hexdigest()
    return candidate == digest


def issue_token(user_id: str) -> str:
    token = uuid.uuid4().hex
    SESSIONS[token] = (user_id, time.time())
    return token


def user_for_token(token: str | None) -> str | None:
    if not token:
        return None
    record = SESSIONS.get(token)
    if record is None:
        return None
    user_id, issued_at = record
    if time.time() - issued_at > SESSION_TTL:
        del SESSIONS[token]
        return None
    return user_id


def revoke(token: str) -> None:
    SESSIONS.pop(token, None)
