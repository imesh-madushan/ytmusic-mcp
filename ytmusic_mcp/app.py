"""Builds and runs the MCP server in stdio (local) or HTTP (hosted, multi-user) mode."""

import logging
import sys

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import Response

from .accounts import AccountRegistry
from .config import OAUTH_SCOPE, ConfigError, Settings, load_settings
from .oauth import SQLiteOAuthProvider
from .security import CookieVault
from .store import Store
from .tools import register_tools

LOGGER = logging.getLogger("ytmusic_mcp")
SERVER_NAME = "youtube-music"
NOISY_LIBRARIES = ("mcp", "sse_starlette", "httpx", "httpcore")


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Dependency DEBUG logs contain raw JSON-RPC payloads; enable them separately.
    for name in NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(settings.library_log_level)


def build_stdio_server(settings: Settings) -> MCPServer:
    """Local mode: one user, cookies read from a file, no OAuth."""
    mcp = MCPServer(SERVER_NAME)
    register_tools(mcp, AccountRegistry(settings.local_auth_file))
    return mcp


def build_http_server(settings: Settings) -> MCPServer:
    """Hosted mode: many users, each with their own login and encrypted cookies."""
    settings.validate_for_http()
    store = Store(settings.db_path)
    provider = SQLiteOAuthProvider(store, settings.issuer_url, OAUTH_SCOPE)
    if settings.static_client:
        provider.add_static_client(settings.static_client)

    mcp = MCPServer(
        SERVER_NAME,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer_url),
            resource_server_url=AnyHttpUrl(settings.public_url),
            required_scopes=[OAUTH_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[OAUTH_SCOPE], default_scopes=[OAUTH_SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    @mcp.custom_route("/login", methods=["GET"])
    async def login(request: Request) -> Response:
        return await provider.login_page(request.query_params.get("state", ""))

    @mcp.custom_route("/login/callback", methods=["POST"])
    async def login_callback(request: Request) -> Response:
        return await provider.login_submit(request)

    accounts = AccountRegistry(settings.local_auth_file, store, CookieVault(settings.secret_key))
    register_tools(mcp, accounts)
    return mcp


def main() -> int:
    try:
        settings = load_settings()
        configure_logging(settings)
        if settings.transport == "stdio":
            build_stdio_server(settings).run(transport="stdio")
            return 0

        import uvicorn

        app = build_http_server(settings).streamable_http_app(host=settings.host)
        LOGGER.info("Serving MCP over Streamable HTTP on http://%s:%s/mcp", settings.host, settings.port)
        uvicorn.run(app, host=settings.host, port=settings.port)
        return 0
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
