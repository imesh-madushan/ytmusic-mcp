"""Small single-user OAuth 2.1 provider for the remote MCP endpoint.

The MCP SDK owns protocol validation, client authentication, PKCE verification,
metadata, and endpoint responses. This provider supplies the resource-owner
login, keeps short-lived authorization state in memory, and persists registered
clients and issued tokens in SQLite.
"""

import json
import uuid
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
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
from setup_auth import normalize_headers
from ytmusicapi import YTMusic, setup

OAUTH_UI_PATH = Path(__file__).resolve().parent / "widgets" / "oauth-login" / "dist" / "index.html"


class ResourceRefreshToken(RefreshToken):
    """Refresh token retaining the RFC 8707 resource audience."""

    resource: str | None = None


class PasswordOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, ResourceRefreshToken, AccessToken]
):
    """OAuth provider with an isolated subject and YouTube session per grant."""

    def __init__(
        self, issuer_url: str, scope: str, database_path: Path, credential_key: str
    ):
        self.issuer_url = issuer_url.rstrip("/")
        self.scope = scope
        self.store = SQLiteOAuthStore(database_path)
        self.credential_cipher = Fernet(credential_key.encode("ascii"))
        self.store.delete_expired_tokens()
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.pending: dict[str, AuthorizationParams] = {}
        self.pending_clients: dict[str, str] = {}
        self.pending_subjects: dict[str, str] = {}
        self.authenticated_states: set[str] = set()
        self.reconnect_tickets: dict[str, tuple[str, float]] = {}

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
        self.pending_subjects[login_state] = str(uuid.uuid4())
        return f"{self.issuer_url}/login?state={login_state}"

    def _encrypt_headers(self, headers_json: str) -> str:
        return self.credential_cipher.encrypt(headers_json.encode("utf-8")).decode("ascii")

    def youtube_headers(self, subject: str) -> str | None:
        encrypted = self.store.get_youtube_credentials(subject)
        if encrypted is None:
            return None
        try:
            return self.credential_cipher.decrypt(encrypted.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("Stored YouTube credentials cannot be decrypted") from exc

    def reconnect_url(self, subject: str) -> str:
        ticket = secrets.token_urlsafe(32)
        self.reconnect_tickets[ticket] = (subject, time.time() + 600)
        return f"{self.issuer_url}/youtube-auth?ticket={ticket}"

    async def login_page(
        self,
        state: str = "",
        error: str | None = None,
        status_code: int = 200,
        *,
        mode: str = "login",
        ticket: str = "",
    ) -> HTMLResponse:
        if state:
            if state not in self.pending:
                raise HTTPException(400, "Invalid or expired authorization request")
            client = await self.get_client(self.pending_clients[state])
            client_name = (client.client_name if client else None) or "your MCP client"
        elif ticket:
            self._reconnect_subject(ticket)
            client_name = "your MCP client"
        else:
            raise HTTPException(400, "Missing authorization request")
        page_data = json.dumps({
            "state": state,
            "ticket": ticket,
            "mode": mode,
            "clientName": client_name,
            "error": error,
        }).replace("<", "\\u003c")
        try:
            document = OAUTH_UI_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                "OAuth login UI is not built. Run `npm run build` in widgets/oauth-login."
            ) from exc
        return HTMLResponse(
            document.replace('"__OAUTH_PAGE_DATA__"', page_data), status_code=status_code
        )

    def _reconnect_subject(self, ticket: str) -> str:
        record = self.reconnect_tickets.get(ticket)
        if record is None or record[1] < time.time():
            self.reconnect_tickets.pop(ticket, None)
            raise HTTPException(400, "Invalid or expired YouTube reconnect link")
        return record[0]

    def _finish_authorization(self, state: str) -> RedirectResponse:
        params = self.pending[state]
        client_id = self.pending_clients[state]
        subject = self.pending_subjects[state]
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
            subject=subject,
        )
        del self.pending[state]
        del self.pending_clients[state]
        del self.pending_subjects[state]
        self.authenticated_states.discard(state)
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
        )

    async def login_callback(self, request: Request) -> Response:
        form = await request.form()
        state = form.get("state")
        username = form.get("username")
        password = form.get("password")
        if not all(isinstance(value, str) for value in (state, username, password)):
            raise HTTPException(400, "Missing login fields")
        if state not in self.pending:
            raise HTTPException(400, "Invalid or expired authorization request")
        if not self.store.verify_oauth_user(username, password):
            return await self.login_page(state, "That username or password did not match.", 401)
        self.authenticated_states.add(state)
        return await self.login_page(state, mode="youtube")

    @staticmethod
    def _validate_youtube_headers(raw_headers: str) -> str:
        headers_json = setup(filepath=None, headers_raw=normalize_headers(raw_headers))
        YTMusic(headers_json)
        return headers_json

    async def youtube_auth_page(self, state: str, ticket: str) -> HTMLResponse:
        return await self.login_page(state=state, mode="youtube", ticket=ticket)

    async def youtube_auth_callback(self, request: Request) -> Response:
        form = await request.form()
        state = form.get("state", "")
        ticket = form.get("ticket", "")
        raw_headers = form.get("headers")
        if not isinstance(raw_headers, str) or not raw_headers.strip():
            raise HTTPException(400, "Missing YouTube Music request headers")
        if isinstance(state, str) and state:
            if state not in self.pending_subjects or state not in self.authenticated_states:
                raise HTTPException(403, "Complete server login before connecting YouTube Music")
            subject = self.pending_subjects[state]
        elif isinstance(ticket, str) and ticket:
            subject = self._reconnect_subject(ticket)
        else:
            raise HTTPException(400, "Missing setup state")
        try:
            headers_json = self._validate_youtube_headers(raw_headers)
        except Exception:
            return await self.login_page(
                state if isinstance(state, str) else "",
                "Those headers could not be parsed or are missing required YouTube fields. Copy a fresh /youtubei/v1/next request and try again.",
                400,
                mode="youtube",
                ticket=ticket if isinstance(ticket, str) else "",
            )
        self.store.save_youtube_credentials(subject, self._encrypt_headers(headers_json))
        if isinstance(ticket, str) and ticket:
            self.reconnect_tickets.pop(ticket, None)
            return HTMLResponse(
                "<!doctype html><meta name='viewport' content='width=device-width'><title>Connected</title>"
                "<body style='font:16px system-ui;background:#0b0b0d;color:white;display:grid;place-items:center;min-height:100vh'>"
                "<main><h1>YouTube Music reconnected</h1><p>You can close this tab and retry your request.</p></main></body>"
            )
        assert isinstance(state, str)
        return self._finish_authorization(state)

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