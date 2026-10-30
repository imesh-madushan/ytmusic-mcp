"""OAuth 2.1 authorization server backed by the SQLite store.

The MCP SDK owns protocol validation, client authentication, PKCE verification,
metadata and endpoint responses. This provider supplies what the SDK leaves to
us: the resource-owner login (which doubles as the consent screen), the step
where a user connects their YouTube cookies, and storage for clients,
authorization codes and tokens.

A user logs in with their password. If YouTube cookies are already stored for
them the grant completes at once; otherwise they are asked to paste fresh
request headers first. A token's ``subject`` is the username, which is how the
tools later find that user's YouTube account.
"""

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
from starlette.responses import RedirectResponse, Response

from .config import StaticClient
from .errors import AccountError
from .headers import encrypt_headers, headers_from_text
from .pages import BASE_HEADERS, PLAIN_CSP, connected_page, message_page, render_page
from .security import DUMMY_PASSWORD_HASH, CookieVault, hash_token, verify_password
from .store import PendingLogin, Store

LOGIN_REQUEST_TTL = 600  # seconds the login page stays valid
RECONNECT_TICKET_TTL = 600
AUTH_CODE_TTL = 300
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 30 * 24 * 3600
MAX_LOGIN_ATTEMPTS = 5  # wrong passwords allowed per login request
MAX_DYNAMIC_CLIENTS = 200  # caps what anonymous /register calls can store
EXPIRED_REQUEST = "This authorization request is invalid or has expired."
HEADERS_REJECTED = (
    "Those headers could not be read or are missing required YouTube fields. "
    "Copy a fresh request made by music.youtube.com and try again."
)


class ResourceRefreshToken(RefreshToken):
    """Refresh token that remembers the RFC 8707 resource it was issued for."""

    resource: str | None = None


class SQLiteOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, ResourceRefreshToken, AccessToken]
):
    """Multi-user OAuth provider: users, cookies and tokens live in SQLite."""

    def __init__(self, store: Store, issuer_url: str, scope: str, vault: CookieVault):
        self.store = store
        self.issuer_url = issuer_url.rstrip("/")
        self.scope = scope
        self.vault = vault

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

    # -- step 1: authorize, then log in --------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to our login page."""
        state = secrets.token_urlsafe(32)
        self.store.save_pending(
            hash_token(state), client.client_id, params.model_dump_json(), time.time() + LOGIN_REQUEST_TTL
        )
        return f"{self.issuer_url}/login?state={state}"

    async def _client_details(self, client_id: str) -> tuple[str, str]:
        """Name and redirect host to show, so the user can see who is asking."""
        client = await self.get_client(client_id)
        if client is None:
            return client_id, ""
        redirect_host = urlparse(str(client.redirect_uris[0])).netloc if client.redirect_uris else ""
        return client.client_name or client_id, redirect_host

    async def _login_form(
        self, state: str, client_id: str, error: str | None = None, status_code: int = 200
    ) -> Response:
        client_name, redirect_host = await self._client_details(client_id)
        return render_page(
            "login",
            state=state,
            client_name=client_name,
            redirect_host=redirect_host,
            error=error,
            status_code=status_code,
        )

    async def login_page(self, state: str) -> Response:
        pending = self.store.get_pending(hash_token(state))
        if pending is None:
            return message_page(EXPIRED_REQUEST)
        return await self._login_form(state, pending.client_id)

    async def login_submit(self, request: Request) -> Response:
        form = await request.form()
        state, username, password = (form.get(name) for name in ("state", "username", "password"))
        if not all(isinstance(value, str) for value in (state, username, password)):
            return message_page("The login form was incomplete.")

        state_hash = hash_token(state)
        pending = self.store.get_pending(state_hash)
        if pending is None:
            return message_page(EXPIRED_REQUEST)

        # Always run a hash check, even for unknown users, so timing reveals nothing.
        user = self.store.get_user(username)
        password_ok = verify_password(password, user.password_hash if user else DUMMY_PASSWORD_HASH)
        if user is None or not password_ok:
            if self.store.record_failed_login(state_hash) >= MAX_LOGIN_ATTEMPTS:
                self.store.delete_pending(state_hash)
                return message_page(
                    "Too many failed attempts. Start the connection again from your client.", 429
                )
            return await self._login_form(state, pending.client_id, "Invalid username or password.", 401)

        self.store.mark_pending_authenticated(state_hash, user.username)
        if user.cookies is None:
            return render_page("youtube", state=state)
        return self._finish_authorization(state_hash, pending, user.username)

    # -- step 2: connect YouTube cookies (new users, or after they expire) -----------

    def reconnect_url(self, username: str) -> str:
        """A short-lived, single-use link where ``username`` can paste fresh headers."""
        ticket = secrets.token_urlsafe(32)
        self.store.save_reconnect_ticket(hash_token(ticket), username, time.time() + RECONNECT_TICKET_TTL)
        return f"{self.issuer_url}/youtube-auth?ticket={ticket}"

    async def youtube_page(self, state: str, ticket: str) -> Response:
        if state:
            pending = self.store.get_pending(hash_token(state))
            if pending is None or pending.username is None:
                return message_page(EXPIRED_REQUEST)
        elif self.store.get_reconnect_ticket(hash_token(ticket)) is None:
            return message_page("This reconnect link is invalid or has expired.")
        return render_page("youtube", state=state, ticket=ticket)

    async def youtube_submit(self, request: Request) -> Response:
        form = await request.form()
        state, ticket, raw_headers = (form.get(name, "") for name in ("state", "ticket", "headers"))
        if (
            not all(isinstance(value, str) for value in (state, ticket, raw_headers))
            or not raw_headers.strip()
        ):
            return message_page("Paste the YouTube Music request headers.")

        pending: PendingLogin | None = None
        if state:
            pending = self.store.get_pending(hash_token(state))
            if pending is None or pending.username is None:
                return message_page("Sign in with your password before connecting YouTube Music.", 403)
            username = pending.username
        else:
            username = self.store.get_reconnect_ticket(hash_token(ticket))
            if username is None:
                return message_page("This reconnect link is invalid or has expired.")

        try:
            encrypted = encrypt_headers(headers_from_text(raw_headers), self.vault)
        except AccountError:
            return render_page("youtube", state=state, ticket=ticket, error=HEADERS_REJECTED, status_code=400)
        self.store.set_cookies(username, encrypted)

        if pending is not None:
            return self._finish_authorization(hash_token(state), pending, username)
        self.store.delete_reconnect_ticket(hash_token(ticket))
        return connected_page()

    # -- step 3: issue the authorization code ---------------------------------------------

    def _finish_authorization(self, state_hash: str, pending: PendingLogin, username: str) -> Response:
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
            subject=username,
        )
        self.store.save_auth_code(
            hash_token(code), pending.client_id, authorization.model_dump_json(), authorization.expires_at
        )
        self.store.delete_pending(state_hash)
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
            headers={**BASE_HEADERS, "Content-Security-Policy": PLAIN_CSP},
        )

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
