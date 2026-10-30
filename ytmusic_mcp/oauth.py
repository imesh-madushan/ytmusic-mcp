"""OAuth 2.1 authorization server backed by the SQLite store.

The MCP SDK owns protocol validation, client authentication, PKCE verification,
metadata and endpoint responses. This provider supplies what the SDK leaves to
us: the resource-owner login page (which also acts as the consent screen) and
storage for clients, authorization codes and tokens.

Each token's ``subject`` is the username that logged in, which is how the tools
later find that user's YouTube account.
"""

import html
import secrets
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .config import StaticClient
from .security import DUMMY_PASSWORD_HASH, hash_token, verify_password
from .store import Store

LOGIN_REQUEST_TTL = 600  # seconds the login page stays valid
AUTH_CODE_TTL = 300
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
MAX_LOGIN_ATTEMPTS = 5  # wrong passwords allowed per login request
MAX_DYNAMIC_CLIENTS = 200  # caps what anonymous /register calls can store

# The login page must never be framed (clickjacking) or cached, and has no scripts.
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

PAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem}}
label{{display:block;margin-top:1rem}}
input{{box-sizing:border-box;width:100%;padding:.7rem;margin-top:.25rem}}
button{{margin-top:1.25rem;padding:.7rem 1rem}}
.client{{padding:.75rem 1rem;background:#f3f3f3;border-radius:.4rem;color:#222}}
.error{{color:#b00020}}
</style></head><body>{body}</body></html>"""

LOGIN_BODY = """<h1>Authorize access</h1>
<p class="client"><strong>{client_name}</strong><br>wants to manage the playlists on your
YouTube Music account and will send you back to <strong>{redirect_host}</strong>.</p>
<p>Only continue if you started this connection yourself.</p>
{error}
<form action="/login/callback" method="post">
<input type="hidden" name="state" value="{state}">
<label>Username<input name="username" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Authorize</button>
</form>"""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        PAGE_TEMPLATE.format(title=html.escape(title), body=body),
        status_code=status_code,
        headers=PAGE_HEADERS,
    )


def _message_page(message: str, status_code: int) -> HTMLResponse:
    return _page(
        "Authorization error", f"<h1>Authorization error</h1><p>{html.escape(message)}</p>", status_code
    )


class ResourceRefreshToken(RefreshToken):
    """Refresh token that remembers the RFC 8707 resource it was issued for."""

    resource: str | None = None


class SQLiteOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, ResourceRefreshToken, AccessToken]
):
    """Multi-user OAuth provider: users and tokens live in SQLite."""

    def __init__(self, store: Store, issuer_url: str, scope: str):
        self.store = store
        self.issuer_url = issuer_url.rstrip("/")
        self.scope = scope

    def add_static_client(self, static: StaticClient) -> None:
        """Register a confidential client configured by the server owner."""
        info = OAuthClientInformationFull(
            client_id=static.client_id,
            client_secret=static.client_secret,
            redirect_uris=[AnyUrl(uri) for uri in static.redirect_uris],
            token_endpoint_auth_method="client_secret_basic",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=self.scope,
            client_name="Claude",
            application_type="web",
        )
        self.store.save_client(static.client_id, info.model_dump_json(), is_static=True)

    # -- client registration -----------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.store.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.purge_expired()
        if self.store.count_dynamic_clients() >= MAX_DYNAMIC_CLIENTS:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="Too many registered clients; try again later.",
            )
        self.store.save_client(client_info.client_id, client_info.model_dump_json())

    # -- authorization: login page and consent -----------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to our login page."""
        state = secrets.token_urlsafe(32)
        self.store.save_pending(
            hash_token(state), client.client_id, params.model_dump_json(), time.time() + LOGIN_REQUEST_TTL
        )
        return f"{self.issuer_url}/login?state={state}"

    async def login_page(self, state: str) -> Response:
        pending = self.store.get_pending(hash_token(state))
        if pending is None:
            return _message_page("This authorization request is invalid or has expired.", 400)
        return await self._render_login(state, pending.client_id)

    async def _render_login(
        self, state: str, client_id: str, error: str = "", status_code: int = 200
    ) -> Response:
        client = await self.get_client(client_id)
        client_name = (client.client_name if client else None) or client_id
        redirect_host = (
            urlparse(str(client.redirect_uris[0])).netloc if client and client.redirect_uris else "?"
        )
        body = LOGIN_BODY.format(
            client_name=html.escape(client_name),
            redirect_host=html.escape(redirect_host),
            state=html.escape(state, quote=True),
            error=f'<p class="error">{html.escape(error)}</p>' if error else "",
        )
        return _page("Authorize access", body, status_code)

    async def login_submit(self, request: Request) -> Response:
        form = await request.form()
        state, username, password = (form.get(name) for name in ("state", "username", "password"))
        if not all(isinstance(value, str) for value in (state, username, password)):
            return _message_page("The login form was incomplete.", 400)

        state_hash = hash_token(state)
        pending = self.store.get_pending(state_hash)
        if pending is None:
            return _message_page("This authorization request is invalid or has expired.", 400)

        # Always run a hash check, even for unknown users, so timing reveals nothing.
        user = self.store.get_user(username)
        password_ok = verify_password(password, user.password_hash if user else DUMMY_PASSWORD_HASH)
        if user is None or not password_ok:
            if self.store.record_failed_login(state_hash) >= MAX_LOGIN_ATTEMPTS:
                self.store.delete_pending(state_hash)
                return _message_page(
                    "Too many failed attempts. Start the connection again from your client.", 429
                )
            return await self._render_login(state, pending.client_id, "Invalid username or password.", 401)

        params = AuthorizationParams.model_validate_json(pending.params)
        code = secrets.token_urlsafe(32)
        authorization = AuthorizationCode(
            code=code,
            client_id=pending.client_id,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + AUTH_CODE_TTL,
            scopes=params.scopes or [self.scope],
            code_challenge=params.code_challenge,
            resource=params.resource,
            subject=user.username,
        )
        self.store.save_auth_code(
            hash_token(code), pending.client_id, authorization.model_dump_json(), authorization.expires_at
        )
        self.store.delete_pending(state_hash)
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
            headers=PAGE_HEADERS,
        )

    # -- authorization code exchange ---------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.store.get_auth_code(hash_token(authorization_code))
        if row is None or row[0] != client.client_id:
            return None
        return AuthorizationCode.model_validate_json(row[1])

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.store.delete_auth_code(hash_token(authorization_code.code))  # codes are single-use
        return self._issue_tokens(
            client.client_id,
            authorization_code.subject or "",
            authorization_code.scopes,
            authorization_code.resource,
            family=secrets.token_urlsafe(16),
        )

    # -- access and refresh tokens -----------------------------------------------

    def _issue_tokens(
        self, client_id: str, username: str, scopes: list[str], resource: str | None, family: str
    ) -> OAuthToken:
        self.store.purge_expired()
        now = time.time()
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        for token, kind, ttl in (
            (access, "access", ACCESS_TOKEN_TTL),
            (refresh, "refresh", REFRESH_TOKEN_TTL),
        ):
            self.store.save_token(
                hash_token(token), kind, family, client_id, username, scopes, resource, now + ttl
            )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.get_token(hash_token(token), "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=int(row.expires_at),
            resource=row.resource,
            subject=row.username,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> ResourceRefreshToken | None:
        row = self.store.get_token(hash_token(refresh_token), "refresh")
        if row is None or row.client_id != client.client_id:
            return None
        return ResourceRefreshToken(
            token=refresh_token,
            client_id=row.client_id,
            scopes=row.scopes,
            expires_at=int(row.expires_at),
            subject=row.username,
            resource=row.resource,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: ResourceRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        requested = scopes or refresh_token.scopes
        if not set(requested).issubset(refresh_token.scopes):
            raise TokenError(
                error="invalid_scope", error_description="Requested scope exceeds the original grant."
            )
        token_hash = hash_token(refresh_token.token)
        row = self.store.get_token(token_hash, "refresh")
        if row is None:
            raise TokenError(error="invalid_grant", error_description="Refresh token is no longer valid.")
        self.store.delete_token(token_hash)  # refresh tokens rotate on every use
        return self._issue_tokens(
            client.client_id,
            refresh_token.subject or "",
            requested,
            refresh_token.resource,
            family=row.family,
        )

    async def revoke_token(self, token: AccessToken | ResourceRefreshToken) -> None:
        """Revoking either half of a pair revokes the whole session."""
        self.store.delete_token_family(hash_token(token.token))
