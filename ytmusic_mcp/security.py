"""Password hashing, token hashing, and encryption of stored YouTube cookies.

* Passwords and OAuth tokens are only ever stored as one-way hashes.
* YouTube cookies must be sent back to YouTube, so they cannot be hashed. They
  are encrypted at rest with a Fernet key that lives in the server environment,
  never in the database.
"""

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


class VaultError(Exception):
    """Stored data could not be decrypted (wrong or changed secret key)."""


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    """Return a self-describing salted scrypt hash: ``scrypt$n$r$p$salt$digest``."""
    salt = os.urandom(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(candidate, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


# Verified against when a username does not exist, so a login attempt takes the
# same time whether or not the account is real.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def hash_token(token: str) -> str:
    """Hash a high-entropy random token for storage (SHA-256 is enough for these)."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_secret_key() -> str:
    return Fernet.generate_key().decode()


class CookieVault:
    """Encrypts and decrypts the per-user YouTube cookie headers."""

    def __init__(self, secret_key: str):
        try:
            self._fernet = Fernet(secret_key.encode())
        except ValueError as exc:
            raise VaultError(
                "YTMUSIC_SECRET_KEY is not a valid Fernet key; "
                "generate one with `python -m ytmusic_mcp.cli genkey`."
            ) from exc

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, blob: bytes) -> str:
        try:
            return self._fernet.decrypt(blob).decode()
        except InvalidToken as exc:
            raise VaultError(
                "Stored cookies cannot be decrypted; YTMUSIC_SECRET_KEY has changed. "
                "Re-add the cookies with `python -m ytmusic_mcp.cli set-cookies`."
            ) from exc
