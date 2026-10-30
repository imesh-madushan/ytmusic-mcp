"""Runtime configuration, read from environment variables and an optional .env file."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
OAUTH_SCOPE = "ytmusic"
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


class ConfigError(Exception):
    """The environment is missing or contains invalid settings."""


@dataclass(frozen=True)
class StaticClient:
    """A pre-registered OAuth client (an alternative to dynamic client registration)."""

    client_id: str
    client_secret: str
    redirect_uris: list[str]


@dataclass(frozen=True)
class Settings:
    transport: str  # "stdio" (one local user) or "http" (many users behind OAuth)
    host: str
    port: int
    public_url: str  # full public URL of the MCP endpoint, e.g. https://mcp.example.com/ytmusic-mcp
    db_path: Path
    secret_key: str  # Fernet key that encrypts stored YouTube cookies
    local_auth_file: Path  # cookie headers used in stdio mode
    log_level: str
    library_log_level: str
    static_client: StaticClient | None

    @property
    def issuer_url(self) -> str:
        """The OAuth issuer is the origin (scheme + host) of the public URL."""
        parsed = urlparse(self.public_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def require_secret_key(self) -> str:
        if not self.secret_key:
            raise ConfigError(
                "YTMUSIC_SECRET_KEY is not set. Generate one with "
                "`python -m ytmusic_mcp.cli genkey` and add it to .env."
            )
        return self.secret_key

    def validate_for_http(self) -> None:
        """Check everything the multi-user HTTP mode needs before the server starts."""
        self.require_secret_key()
        if not self.public_url:
            raise ConfigError("MCP_PUBLIC_URL is required in HTTP mode.")
        parsed = urlparse(self.public_url)
        if not parsed.netloc or parsed.scheme not in ("http", "https"):
            raise ConfigError(f"MCP_PUBLIC_URL is not a valid URL: {self.public_url!r}")
        if parsed.scheme != "https" and parsed.hostname not in LOCAL_HOSTNAMES:
            raise ConfigError("MCP_PUBLIC_URL must use https unless it points at localhost.")


def _parse_static_client(env: Mapping[str, str]) -> StaticClient | None:
    client_id = env.get("OAUTH_CLIENT_ID", "").strip()
    client_secret = env.get("OAUTH_CLIENT_SECRET", "").strip()
    redirect_uris = [u.strip() for u in env.get("OAUTH_REDIRECT_URIS", "").split(",") if u.strip()]
    if not any((client_id, client_secret, redirect_uris)):
        return None
    if not all((client_id, client_secret, redirect_uris)):
        raise ConfigError(
            "OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET and OAUTH_REDIRECT_URIS "
            "must all be set when using a static OAuth client."
        )
    return StaticClient(client_id, client_secret, redirect_uris)


def settings_from_env(env: Mapping[str, str]) -> Settings:
    try:
        port = int(env.get("MCP_PORT", "8787"))
    except ValueError as exc:
        raise ConfigError("MCP_PORT must be an integer.") from exc
    transport = env.get("MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "http"):
        raise ConfigError(f"MCP_TRANSPORT must be 'stdio' or 'http', not {transport!r}.")
    return Settings(
        transport=transport,
        host=env.get("MCP_HOST", "127.0.0.1"),
        port=port,
        public_url=env.get("MCP_PUBLIC_URL", "").strip().rstrip("/"),
        db_path=Path(env.get("YTMUSIC_DB_PATH", PROJECT_DIR / "data" / "ytmusic-mcp.db")).resolve(),
        secret_key=env.get("YTMUSIC_SECRET_KEY", "").strip(),
        local_auth_file=Path(env.get("YTMUSIC_AUTH_FILE", PROJECT_DIR / "browser.json")).resolve(),
        log_level=env.get("YTMUSIC_LOG_LEVEL", "INFO").upper(),
        library_log_level=env.get("MCP_LIBRARY_LOG_LEVEL", "INFO").upper(),
        static_client=_parse_static_client(env),
    )


def load_settings() -> Settings:
    """Load `.env` (real environment variables win) and build the settings."""
    load_dotenv(PROJECT_DIR / ".env")
    return settings_from_env(os.environ)
