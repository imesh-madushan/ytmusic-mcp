"""SQLite persistence for users, OAuth clients, and OAuth tokens.

Secrets are never stored in the clear: passwords are scrypt-hashed, tokens and
codes are SHA-256-hashed, and YouTube cookies are Fernet-encrypted before they
reach this module (see security.py).
"""

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    cookies       BLOB,            -- Fernet-encrypted YouTube request headers
    updated_at    REAL NOT NULL    -- bumped whenever the cookies change
);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,      -- OAuthClientInformationFull as JSON
    is_static  INTEGER NOT NULL,   -- 1 = configured by the server owner, never purged
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_logins (
    state_hash TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    params     TEXT NOT NULL,      -- AuthorizationParams as JSON
    username   TEXT,               -- set once the password was accepted
    attempts   INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reconnect_tickets (
    ticket_hash TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash  TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    data       TEXT NOT NULL,      -- AuthorizationCode as JSON
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,      -- 'access' or 'refresh'
    family     TEXT NOT NULL,      -- shared by an access/refresh pair and its refreshed successors
    client_id  TEXT NOT NULL,
    username   TEXT NOT NULL,
    scopes     TEXT NOT NULL,      -- space-separated
    resource   TEXT,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_family ON tokens (family);
CREATE INDEX IF NOT EXISTS tokens_username ON tokens (username);
"""

# Dynamically registered clients that have not been used for this long are removed.
UNUSED_CLIENT_TTL = 30 * 24 * 3600


@dataclass(frozen=True)
class User:
    username: str
    password_hash: str
    cookies: bytes | None
    updated_at: float


@dataclass(frozen=True)
class PendingLogin:
    client_id: str
    params: str
    username: str | None  # None until the password has been accepted
    attempts: int


@dataclass(frozen=True)
class TokenRow:
    family: str
    client_id: str
    username: str
    scopes: list[str]
    resource: str | None
    expires_at: float


class Store:
    """Thin, synchronous wrapper around one SQLite database file."""

    def __init__(self, path: Path):
        self.path = path
        new_directory = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if new_directory:  # never change the permissions of a directory that already existed
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        with self._connect() as db:
            self._refuse_legacy_database(db)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _refuse_legacy_database(self, db: sqlite3.Connection) -> None:
        """The pre-package server used a different layout; do not run on top of it."""
        legacy = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('schema_metadata', 'oauth_users')"
        ).fetchone()
        if legacy:
            raise RuntimeError(
                f"{self.path} was created by an older version of this server and has a different "
                "layout. Point YTMUSIC_DB_PATH at a new file and re-add your users "
                "(python -m ytmusic_mcp.cli add-user)."
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    # -- users ---------------------------------------------------------------

    @staticmethod
    def _user(row: sqlite3.Row | None) -> User | None:
        if row is None:
            return None
        return User(row["username"], row["password_hash"], row["cookies"], row["updated_at"])

    def create_user(self, username: str, password_hash: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO users (username, password_hash, updated_at) VALUES (?, ?, ?)",
                (username, password_hash, time.time()),
            )

    def get_user(self, username: str) -> User | None:
        with self._connect() as db:
            return self._user(db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())

    def list_users(self) -> list[User]:
        with self._connect() as db:
            return [self._user(row) for row in db.execute("SELECT * FROM users ORDER BY username")]

    def set_password(self, username: str, password_hash: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username)
            )
            return cursor.rowcount > 0

    def set_cookies(self, username: str, encrypted: bytes) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE users SET cookies = ?, updated_at = ? WHERE username = ?",
                (encrypted, time.time(), username),
            )
            return cursor.rowcount > 0

    def clear_cookies(self, username: str) -> None:
        """Forget cookies that YouTube rejected, so the next login asks for fresh ones."""
        with self._connect() as db:
            db.execute(
                "UPDATE users SET cookies = NULL, updated_at = ? WHERE username = ?", (time.time(), username)
            )

    def delete_user(self, username: str) -> bool:
        """Delete a user and revoke everything that user was issued."""
        with self._connect() as db:
            db.execute("DELETE FROM tokens WHERE username = ?", (username,))
            db.execute("DELETE FROM reconnect_tickets WHERE username = ?", (username,))
            return db.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount > 0

    # -- OAuth clients ---------------------------------------------------------

    def save_client(self, client_id: str, info: str, is_static: bool = False) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO oauth_clients (client_id, info, is_static, created_at) "
                "VALUES (?, ?, ?, ?)",
                (client_id, info, int(is_static), time.time()),
            )

    def get_client(self, client_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
            return row["info"] if row else None

    def count_dynamic_clients(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM oauth_clients WHERE is_static = 0").fetchone()[0]

    # -- login requests and authorization codes --------------------------------

    def save_pending(self, state_hash: str, client_id: str, params: str, expires_at: float) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO pending_logins (state_hash, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
                (state_hash, client_id, params, expires_at),
            )

    def get_pending(self, state_hash: str) -> PendingLogin | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT client_id, params, username, attempts FROM pending_logins "
                "WHERE state_hash = ? AND expires_at > ?",
                (state_hash, time.time()),
            ).fetchone()
            if row is None:
                return None
            return PendingLogin(row["client_id"], row["params"], row["username"], row["attempts"])

    def mark_pending_authenticated(self, state_hash: str, username: str) -> None:
        """Remember that this login request passed the password check."""
        with self._connect() as db:
            db.execute("UPDATE pending_logins SET username = ? WHERE state_hash = ?", (username, state_hash))

    def record_failed_login(self, state_hash: str) -> int:
        """Count a failed password attempt and return the new total."""
        with self._connect() as db:
            db.execute(
                "UPDATE pending_logins SET attempts = attempts + 1 WHERE state_hash = ?", (state_hash,)
            )
            row = db.execute(
                "SELECT attempts FROM pending_logins WHERE state_hash = ?", (state_hash,)
            ).fetchone()
            return row["attempts"] if row else 0

    def delete_pending(self, state_hash: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM pending_logins WHERE state_hash = ?", (state_hash,))

    def save_auth_code(self, code_hash: str, client_id: str, data: str, expires_at: float) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO auth_codes (code_hash, client_id, data, expires_at) VALUES (?, ?, ?, ?)",
                (code_hash, client_id, data, expires_at),
            )

    def get_auth_code(self, code_hash: str) -> tuple[str, str] | None:
        """Return ``(client_id, data)`` for an unexpired authorization code."""
        with self._connect() as db:
            row = db.execute(
                "SELECT client_id, data FROM auth_codes WHERE code_hash = ? AND expires_at > ?",
                (code_hash, time.time()),
            ).fetchone()
            return (row["client_id"], row["data"]) if row else None

    def delete_auth_code(self, code_hash: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM auth_codes WHERE code_hash = ?", (code_hash,))

    # -- one-time links for replacing expired YouTube cookies --------------------

    def save_reconnect_ticket(self, ticket_hash: str, username: str, expires_at: float) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO reconnect_tickets (ticket_hash, username, expires_at) VALUES (?, ?, ?)",
                (ticket_hash, username, expires_at),
            )

    def get_reconnect_ticket(self, ticket_hash: str) -> str | None:
        """Return the username a ticket belongs to, if it has not expired."""
        with self._connect() as db:
            row = db.execute(
                "SELECT username FROM reconnect_tickets WHERE ticket_hash = ? AND expires_at > ?",
                (ticket_hash, time.time()),
            ).fetchone()
            return row["username"] if row else None

    def delete_reconnect_ticket(self, ticket_hash: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM reconnect_tickets WHERE ticket_hash = ?", (ticket_hash,))

    # -- access and refresh tokens ---------------------------------------------

    def save_token(
        self,
        token_hash: str,
        kind: str,
        family: str,
        client_id: str,
        username: str,
        scopes: list[str],
        resource: str | None,
        expires_at: float,
    ) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO tokens (token_hash, kind, family, client_id, username, scopes, resource, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (token_hash, kind, family, client_id, username, " ".join(scopes), resource, expires_at),
            )

    def get_token(self, token_hash: str, kind: str) -> TokenRow | None:
        """Return an unexpired token of the given kind whose user still exists."""
        with self._connect() as db:
            row = db.execute(
                "SELECT t.* FROM tokens t JOIN users u ON u.username = t.username "
                "WHERE t.token_hash = ? AND t.kind = ? AND t.expires_at > ?",
                (token_hash, kind, time.time()),
            ).fetchone()
            if row is None:
                return None
            return TokenRow(
                row["family"],
                row["client_id"],
                row["username"],
                row["scopes"].split(),
                row["resource"],
                row["expires_at"],
            )

    def delete_token(self, token_hash: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM tokens WHERE token_hash = ?", (token_hash,))

    def delete_token_family(self, token_hash: str) -> None:
        """Revoke a token together with its access/refresh partner and refreshed successors."""
        with self._connect() as db:
            row = db.execute("SELECT family FROM tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            if row:
                db.execute("DELETE FROM tokens WHERE family = ?", (row["family"],))

    # -- housekeeping ----------------------------------------------------------

    def purge_expired(self) -> None:
        """Drop expired rows and dynamic clients that have been unused for a long time."""
        now = time.time()
        with self._connect() as db:
            for table in ("pending_logins", "auth_codes", "tokens", "reconnect_tickets"):
                db.execute(f"DELETE FROM {table} WHERE expires_at <= ?", (now,))
            db.execute(
                "DELETE FROM oauth_clients WHERE is_static = 0 AND created_at < ? "
                "AND client_id NOT IN (SELECT client_id FROM tokens)",
                (now - UNUSED_CLIENT_TTL,),
            )
