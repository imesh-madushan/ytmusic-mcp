"""Small single-user OAuth 2.1 provider for the remote MCP endpoint.

The MCP SDK owns protocol validation, client authentication, PKCE verification,
metadata, and endpoint responses. This provider supplies the resource-owner
login, keeps short-lived authorization state in memory, and persists registered
clients and issued tokens in SQLite.
"""

import html
import secrets
import time
from pathlib import Path

from pydantic import AnyUrl
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from oauth_store import SQLiteOAuthStore


class ResourceRefreshToken(RefreshToken):
    """Refresh token retaining the RFC 8707 resource audience."""

    resource: str | None = None


class PasswordOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, ResourceRefreshToken, AccessToken]
):
    """OAuth provider backed by one configured resource-owner login."""

    def __init__(
        self, issuer_url: str, username: str, password: str, scope: str, database_path: Path
    ):
        self.issuer_url = issuer_url.rstrip("/")
        self.username = username
        self.password = password
        self.scope = scope
        self.store = SQLiteOAuthStore(database_path)
        self.store.delete_expired_tokens()
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.pending: dict[str, AuthorizationParams] = {}
        self.pending_clients: dict[str, str] = {}

    def add_static_client(self, client_id: str, client_secret: str, redirect_uris: list[str]) -> None:
        """Register a confidential client configured by the server owner."""
        self.store.save_client(OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[AnyUrl(uri) for uri in redirect_uris],
            token_endpoint_auth_method="client_secret_basic",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=self.scope,
            client_name="Claude",
            application_type="web",
        ))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.save_client(client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        login_state = secrets.token_urlsafe(32)
        self.pending[login_state] = params
        self.pending_clients[login_state] = client.client_id
        return f"{self.issuer_url}/login?state={login_state}"

    async def login_page(self, state: str) -> HTMLResponse:
        if state not in self.pending:
            raise HTTPException(400, "Invalid or expired authorization request")
        safe_state = html.escape(state, quote=True)
        return HTMLResponse(
            f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Authorize YouTube Music MCP</title>
<style>body{{font:16px system-ui;max-width:28rem;margin:4rem auto;padding:0 1rem}}
label{{display:block;margin-top:1rem}}input{{box-sizing:border-box;width:100%;padding:.7rem}}
button{{margin-top:1.25rem;padding:.7rem 1rem}}</style></head>
<body><h1>Authorize YouTube Music MCP</h1>
<p>Sign in with the private OAuth credentials configured on your server.</p>
<form action="/login/callback" method="post">
<input type="hidden" name="state" value="{safe_state}">
<label>Username<input name="username" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Authorize Claude</button></form></body></html>"""
        )

    async def login_callback(self, request: Request) -> Response:
        form = await request.form()
        state = form.get("state")
        username = form.get("username")
        password = form.get("password")
        if not all(isinstance(value, str) for value in (state, username, password)):
            raise HTTPException(400, "Missing login fields")
        assert isinstance(state, str) and isinstance(username, str) and isinstance(password, str)
        params = self.pending.get(state)
        client_id = self.pending_clients.get(state)
        if params is None or client_id is None:
            raise HTTPException(400, "Invalid or expired authorization request")
        if not secrets.compare_digest(username, self.username) or not secrets.compare_digest(password, self.password):
            raise HTTPException(401, "Invalid username or password")

        code = secrets.token_urlsafe(32)
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=client_id,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            expires_at=time.time() + 300,
            scopes=params.scopes or [self.scope],
            code_challenge=params.code_challenge,
            resource=params.resource,
            subject=self.username,
        )
        del self.pending[state]
        del self.pending_clients[state]
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    def _new_tokens(
        self, client_id: str, scopes: list[str], resource: str | None, subject: str | None
    ) -> tuple[AccessToken, ResourceRefreshToken, OAuthToken]:
        access_value = secrets.token_urlsafe(48)
        refresh_value = secrets.token_urlsafe(48)
        access_token = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 3600,
            resource=resource,
            subject=subject,
        )
        refresh_token = ResourceRefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time()) + 30 * 24 * 3600,
            subject=subject,
            resource=resource,
        )
        response = OAuthToken(
            access_token=access_value,
            refresh_token=refresh_value,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(scopes),
        )
        return access_token, refresh_token, response

    def _issue_tokens(
        self, client_id: str, scopes: list[str], resource: str | None, subject: str | None
    ) -> OAuthToken:
        access_token, refresh_token, response = self._new_tokens(
            client_id, scopes, resource, subject
        )
        self.store.save_tokens(access_token, refresh_token)
        return response

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self.auth_codes.pop(authorization_code.code, None)
        return self._issue_tokens(
            client.client_id,
            authorization_code.scopes,
            authorization_code.resource,
            authorization_code.subject,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.store.get_access_token(token)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> ResourceRefreshToken | None:
        token = self.store.get_refresh_token(refresh_token, ResourceRefreshToken)
        if token is None or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: ResourceRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        requested = scopes or refresh_token.scopes
        if not set(requested).issubset(refresh_token.scopes):
            raise ValueError("Requested scope exceeds the original grant")
        access_token, replacement_refresh, response = self._new_tokens(
            client.client_id, requested, refresh_token.resource, refresh_token.subject
        )
        self.store.rotate_refresh_token(
            refresh_token.token, access_token, replacement_refresh
        )
        return response

    async def revoke_token(self, token: AccessToken | ResourceRefreshToken) -> None:
        self.store.revoke_token(token.token)