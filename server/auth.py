from __future__ import annotations

import hashlib
import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from . import db
from .config import settings

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=2)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, Exception):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def bootstrap_operator() -> None:
    existing = await db.fetchone("SELECT id FROM users WHERE username = ?", ("operator",))
    if existing:
        return
    await db.execute(
        "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
        ("operator", hash_password(settings.operator_password), "operator"),
    )


async def register(username: str, password: str) -> dict[str, Any] | None:
    if len(username) < 3 or len(password) < 6:
        return None
    exists = await db.fetchone("SELECT id FROM users WHERE username = ?", (username,))
    if exists:
        return None
    uid = await db.execute(
        "INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
        (username, hash_password(password), "player"),
    )
    await db.execute(
        "INSERT INTO audit_logs(user_id,action,payload) VALUES(?,?,?)",
        (uid, "register", db.dumps({"username": username})),
    )
    return await db.fetchone("SELECT * FROM users WHERE id = ?", (uid,))


async def login(username: str, password: str) -> str | None:
    user = await db.fetchone("SELECT * FROM users WHERE username = ?", (username,))
    if not user or not verify_password(user["password_hash"], password):
        return None
    token = secrets.token_urlsafe(36)
    await db.execute(
        "INSERT INTO tokens(token_hash,user_id,expires_at) VALUES(?,?,datetime('now','+365 days'))",
        (token_digest(token), user["id"]),
    )
    await db.execute(
        "INSERT INTO audit_logs(user_id,action,payload) VALUES(?,?,?)",
        (user["id"], "login", db.dumps({"username": username})),
    )
    return token


async def user_from_token(token: str) -> dict[str, Any] | None:
    row = await db.fetchone(
        """SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id
           WHERE t.token_hash = ? AND t.expires_at > datetime('now')""",
        (token_digest(token),),
    )
    return row


async def logout(token: str) -> None:
    await db.execute("DELETE FROM tokens WHERE token_hash = ?", (token_digest(token),))
