"""SQLite persistence for OAuth clients and bearer tokens."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull


class OAuthStoreError(RuntimeError):
    """Raised when persisted OAuth state cannot be read safely."""


class SQLiteOAuthStore:
    """Small transactional store used by the single-process OAuth provider."""

    SCHEMA_VERSION = 3

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS oauth_clients (
                        client_id TEXT PRIMARY KEY,
                        model_json TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS oauth_access_tokens (
                        token_hash TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        model_json TEXT NOT NULL,
                        expires_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS oauth_access_tokens_expiry
                        ON oauth_access_tokens(expires_at);
                    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                        token_hash TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        model_json TEXT NOT NULL,
                        expires_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS oauth_refresh_tokens_expiry
                        ON oauth_refresh_tokens(expires_at);
                    CREATE TABLE IF NOT EXISTS oauth_users (
                        username TEXT PRIMARY KEY,
                        password_salt BLOB NOT NULL,
                        password_hash BLOB NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS youtube_credentials (
                        subject TEXT PRIMARY KEY,
                        encrypted_headers TEXT NOT NULL,
                        validated_at INTEGER NOT NULL,
                        last_used_at INTEGER,
                        invalidated_at INTEGER,
                        validation_error TEXT
                    );
                    """
                )
                row = connection.execute(
                    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?)",
                        (str(self.SCHEMA_VERSION),),
                    )
                elif int(row["value"]) > self.SCHEMA_VERSION:
                    raise OAuthStoreError(
                        f"Unsupported OAuth database schema version {row['value']} "
                        f"(expected {self.SCHEMA_VERSION})"
                    )
                elif int(row["value"]) < self.SCHEMA_VERSION:
                    connection.execute(
                        "UPDATE schema_metadata SET value = ? WHERE key = 'schema_version'",
                        (str(self.SCHEMA_VERSION),),
                    )
            os.chmod(self.path, 0o600)
        except (OSError, sqlite3.Error, ValueError) as exc:
            if isinstance(exc, OAuthStoreError):
                raise
            raise OAuthStoreError(
                f"Unable to initialize OAuth database at {self.path}: {exc}"
            ) from exc

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _token_payload(token: Any) -> str:
        payload = token.model_dump(mode="json", exclude={"token"})
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def save_client(self, client: OAuthClientInformationFull) -> None:
        if not client.client_id:
            raise ValueError("OAuth client registration is missing client_id")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO oauth_clients(client_id, model_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(client_id) DO UPDATE SET
                    model_json = excluded.model_json,
                    updated_at = excluded.updated_at
                """,
                (client.client_id, client.model_dump_json(), int(time.time())),
            )

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT model_json FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(row["model_json"])
        except Exception as exc:
            raise OAuthStoreError(f"Stored OAuth client {client_id!r} is invalid") from exc

    def save_tokens(self, access_token: AccessToken, refresh_token: Any) -> None:
        with self._connect() as connection:
            self._insert_tokens(connection, access_token, refresh_token)

    def _insert_tokens(
        self, connection: sqlite3.Connection, access_token: AccessToken, refresh_token: Any
    ) -> None:
        connection.execute(
            """INSERT INTO oauth_access_tokens(token_hash, client_id, model_json, expires_at)
               VALUES (?, ?, ?, ?)""",
            (self._token_hash(access_token.token), access_token.client_id,
             self._token_payload(access_token), access_token.expires_at),
        )
        connection.execute(
            """INSERT INTO oauth_refresh_tokens(token_hash, client_id, model_json, expires_at)
               VALUES (?, ?, ?, ?)""",
            (self._token_hash(refresh_token.token), refresh_token.client_id,
             self._token_payload(refresh_token), refresh_token.expires_at),
        )

    def get_access_token(self, token: str) -> AccessToken | None:
        token_hash = self._token_hash(token)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT model_json, expires_at FROM oauth_access_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] is not None and row["expires_at"] < time.time():
                connection.execute("DELETE FROM oauth_access_tokens WHERE token_hash = ?", (token_hash,))
                return None
        try:
            return AccessToken.model_validate({"token": token, **json.loads(row["model_json"])})
        except Exception as exc:
            raise OAuthStoreError("Stored OAuth access token is invalid") from exc

    def get_refresh_token(self, token: str, token_type: type[Any]) -> Any | None:
        token_hash = self._token_hash(token)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT model_json, expires_at FROM oauth_refresh_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] is not None and row["expires_at"] < time.time():
                connection.execute("DELETE FROM oauth_refresh_tokens WHERE token_hash = ?", (token_hash,))
                return None
        try:
            return token_type.model_validate({"token": token, **json.loads(row["model_json"])})
        except Exception as exc:
            raise OAuthStoreError("Stored OAuth refresh token is invalid") from exc

    def rotate_refresh_token(
        self, old_token: str, access_token: AccessToken, refresh_token: Any
    ) -> None:
        """Consume a refresh token and save its replacements atomically."""
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM oauth_refresh_tokens WHERE token_hash = ?",
                (self._token_hash(old_token),),
            ).rowcount
            if deleted != 1:
                raise ValueError("Refresh token has already been used or revoked")
            self._insert_tokens(connection, access_token, refresh_token)

    def revoke_token(self, token: str) -> None:
        token_hash = self._token_hash(token)
        with self._connect() as connection:
            connection.execute("DELETE FROM oauth_access_tokens WHERE token_hash = ?", (token_hash,))
            connection.execute("DELETE FROM oauth_refresh_tokens WHERE token_hash = ?", (token_hash,))

    def delete_expired_tokens(self) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM oauth_access_tokens WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
            )
            connection.execute(
                "DELETE FROM oauth_refresh_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )

    def set_oauth_user_password(self, username: str, password: str) -> None:
        salt = os.urandom(16)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO oauth_users(username, password_salt, password_hash, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(username) DO UPDATE SET
                     password_salt = excluded.password_salt,
                     password_hash = excluded.password_hash""",
                (username, salt, password_hash, int(time.time())),
            )

    def verify_oauth_user(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT password_salt, password_hash FROM oauth_users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), row["password_salt"], 600_000
        )
        return hmac.compare_digest(candidate, row["password_hash"])

    def save_youtube_credentials(self, subject: str, encrypted_headers: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO youtube_credentials(subject, encrypted_headers, validated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(subject) DO UPDATE SET
                     encrypted_headers = excluded.encrypted_headers,
                     validated_at = excluded.validated_at,
                     invalidated_at = NULL,
                     validation_error = NULL""",
                (subject, encrypted_headers, now),
            )

    def get_youtube_credentials(self, subject: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT encrypted_headers FROM youtube_credentials
                   WHERE subject = ? AND invalidated_at IS NULL""",
                (subject,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE youtube_credentials SET last_used_at = ? WHERE subject = ?",
                    (int(time.time()), subject),
                )
        return row["encrypted_headers"] if row else None

    def invalidate_youtube_credentials(self, subject: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE youtube_credentials
                   SET invalidated_at = ?, validation_error = ? WHERE subject = ?""",
                (int(time.time()), error[:500], subject),
            )
