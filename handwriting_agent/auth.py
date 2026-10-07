import base64
import hashlib
import hmac
import json
import os
import secrets
import smtplib
import sqlite3
from contextlib import closing
import time
from email.message import EmailMessage
from typing import Any

from handwriting_agent.database import DatabaseConnection, connect


PASSWORD_ITERATIONS = 310_000


def _connect() -> DatabaseConnection:
    connection = connect()
    if isinstance(connection, sqlite3.Connection):
        connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            verification_hash TEXT,
            verification_expires INTEGER,
            verification_attempts INTEGER NOT NULL DEFAULT 0,
            verification_sent_at INTEGER,
            free_used INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assignment_reservations (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            entitlement TEXT NOT NULL CHECK(entitlement IN ('free', 'paid')),
            created_at INTEGER NOT NULL
        );
        """
    )
    return connection


def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return base64.urlsafe_b64encode(digest).decode("ascii")


def create_user(email: str, password: str, code: str) -> int:
    salt = secrets.token_bytes(16)
    now = int(time.time())
    with closing(_connect()) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users
                (email, salt, password_hash, verification_hash, verification_expires, verification_sent_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email.lower(),
                base64.urlsafe_b64encode(salt).decode("ascii"),
                _hash_password(password, salt),
                _verification_hash(email, code),
                now + 15 * 60,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def delete_unverified_user(user_id: int) -> None:
    with closing(_connect()) as connection:
        connection.execute("DELETE FROM users WHERE id = ? AND verified = 0", (user_id,))


def verify_email(email: str, code: str) -> dict[str, Any] | None:
    now = int(time.time())
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
        if (
            user is None
            or user["verification_expires"] is None
            or user["verification_expires"] < now
            or user["verification_attempts"] >= 5
            or not hmac.compare_digest(
                user["verification_hash"] or "", _verification_hash(email, code)
            )
        ):
            if user is not None and user["verification_expires"] is not None:
                connection.execute(
                    """
                    UPDATE users
                    SET verification_attempts = verification_attempts + 1,
                        verification_hash = CASE WHEN verification_attempts >= 4 THEN NULL ELSE verification_hash END,
                        verification_expires = CASE WHEN verification_attempts >= 4 THEN NULL ELSE verification_expires END
                    WHERE id = ?
                    """,
                    (user["id"],),
                )
                connection.commit()
                return None
            connection.rollback()
            return None
        connection.execute(
            "UPDATE users SET verified = 1, verification_hash = NULL, verification_expires = NULL, verification_attempts = 0 WHERE id = ?",
            (user["id"],),
        )
        connection.commit()
        return dict(user)


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    with closing(_connect()) as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
    if user is None:
        _hash_password(password, secrets.token_bytes(16))
        return None
    salt = base64.urlsafe_b64decode(user["salt"].encode("ascii"))
    password_hash = _hash_password(password, salt)
    if not hmac.compare_digest(user["password_hash"], password_hash):
        return None
    return dict(user)


def _verification_hash(email: str, code: str) -> str:
    secret = _auth_secret()
    return hmac.new(
        secret.encode(), f"{email.lower()}:{code}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def create_verification_code(email: str) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = int(time.time())
    with closing(_connect()) as connection:
        cursor = connection.execute(
            """
            UPDATE users SET verification_hash = ?, verification_expires = ?, verification_attempts = 0, verification_sent_at = ?
            WHERE email = ? COLLATE NOCASE AND verified = 0
                AND (verification_sent_at IS NULL OR verification_sent_at < ?)
            """,
            (_verification_hash(email, code), now + 15 * 60, now, email, now - 60),
        )
    return code if cursor.rowcount else ""


def send_verification_email(email: str, code: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", "").strip()
    if not all((host, username, password, sender)):
        raise RuntimeError("Email delivery is not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and SMTP_FROM.")
    port = int(os.getenv("SMTP_PORT", "587"))
    message = EmailMessage()
    message["Subject"] = "Verify your InkTwin account"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Your InkTwin verification code is {code}. It expires in 15 minutes."
    )
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(message)


def create_token(user_id: int) -> str:
    secret = _auth_secret()
    now = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(
        json.dumps({"sub": user_id, "iat": now, "exp": now + 60 * 60 * 24 * 7}, separators=(",", ":")).encode()
    )
    signing_input = f"{header}.{payload}"
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def decode_token(token: str) -> int | None:
    try:
        secret = _auth_secret()
    except RuntimeError:
        return None
    try:
        header, payload, signature = token.split(".")
        signing_input = f"{header}.{payload}"
        expected = _b64(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(_unb64(payload))
        if claims.get("exp", 0) < time.time():
            return None
        return int(claims["sub"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def get_user(user_id: int) -> dict[str, Any] | None:
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _release_expired_reservations(connection, int(time.time()))
        user = connection.execute(
            "SELECT id, email, verified, free_used FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if user is None:
            connection.commit()
            return None
        connection.commit()
    data = dict(user)
    data["assignments_generated"] = data["free_used"]
    return data


def reserve_assignment(user_id: int) -> tuple[int, str] | None:
    now = int(time.time())
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _release_expired_reservations(connection, now)

        user = connection.execute(
            "SELECT free_used FROM users WHERE id = ? AND verified = 1",
            (user_id,),
        ).fetchone()
        if user is None:
            connection.rollback()
            return None
        entitlement = "free"
        connection.execute("UPDATE users SET free_used = free_used + 1 WHERE id = ?", (user_id,))
        cursor = connection.execute(
            "INSERT INTO assignment_reservations (user_id, entitlement, created_at) VALUES (?, ?, ?)",
            (user_id, entitlement, now),
        )
        connection.commit()
        return int(cursor.lastrowid), entitlement


def _release_expired_reservations(connection: DatabaseConnection, now: int) -> None:
    stale = connection.execute(
        "SELECT id, user_id, entitlement FROM assignment_reservations WHERE created_at < ?",
        (now - 30 * 60,),
    ).fetchall()
    for item in stale:
        if item["entitlement"] == "free":
            connection.execute(
                "UPDATE users SET free_used = MAX(free_used - 1, 0) WHERE id = ?",
                (item["user_id"],),
            )
        connection.execute("DELETE FROM assignment_reservations WHERE id = ?", (item["id"],))


def finish_assignment(reservation_id: int, *, succeeded: bool) -> None:
    with closing(_connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        reservation = connection.execute(
            "SELECT user_id, entitlement FROM assignment_reservations WHERE id = ?",
            (reservation_id,),
        ).fetchone()
        if reservation is not None:
            if not succeeded:
                if reservation["entitlement"] == "free":
                    connection.execute(
                        "UPDATE users SET free_used = MAX(free_used - 1, 0) WHERE id = ?",
                        (reservation["user_id"],),
                    )
            connection.execute("DELETE FROM assignment_reservations WHERE id = ?", (reservation_id,))
        connection.commit()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _auth_secret() -> str:
    secret = os.getenv("AUTH_SECRET_KEY", "")
    if len(secret) < 32:
        raise RuntimeError("Set AUTH_SECRET_KEY to a random secret of at least 32 characters.")
    return secret


def validate_configuration() -> None:
    _auth_secret()
