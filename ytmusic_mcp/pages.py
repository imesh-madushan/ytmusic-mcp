"""The browser pages of the login flow.

If the React sign-in app has been built (`npm run build` in widgets/oauth-login)
it is served; otherwise a plain server-rendered page with the same form fields
is used, so a fresh clone works without Node.
"""

import html
import json
from pathlib import Path
from typing import Literal

from starlette.responses import HTMLResponse

from .config import PROJECT_DIR

OAUTH_UI_PATH = PROJECT_DIR / "widgets" / "oauth-login" / "dist" / "index.html"
PAGE_DATA_PLACEHOLDER = '"__OAUTH_PAGE_DATA__"'

Mode = Literal["login", "youtube"]

# Never allow these pages to be framed (clickjacking) or cached. The React build
# inlines its script and styles, so it needs 'unsafe-inline' for both.
BASE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
PLAIN_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
REACT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; base-uri 'none'; frame-ancestors 'none'"
)

PAGE_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}}
label{{display:block;margin-top:1rem}}
input,textarea{{box-sizing:border-box;width:100%;padding:.7rem;margin-top:.25rem;font:inherit}}
textarea{{min-height:11rem;font:13px ui-monospace,monospace}}
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

YOUTUBE_BODY = """<h1>Connect YouTube Music</h1>
<p>Your YouTube Music session is missing or expired. The headers go straight to your own
server, are validated, and are stored encrypted.</p>
<ol>
<li>Open <a href="https://music.youtube.com" target="_blank" rel="noreferrer">music.youtube.com</a> and sign in.</li>
<li>Press F12, open <strong>Network</strong>, then play a song or refresh.</li>
<li>Pick a request beginning <code>/youtubei/v1/</code> and copy all its <strong>Request Headers</strong>.</li>
</ol>
{error}
<form action="/youtube-auth/callback" method="post">
<input type="hidden" name="state" value="{state}">
<input type="hidden" name="ticket" value="{ticket}">
<label>Request headers<textarea name="headers" required spellcheck="false"></textarea></label>
<p>Raw headers, a JSON header list, HAR, or copied cURL all work.</p>
<button type="submit">Validate and connect</button>
</form>"""


def _respond(
    body: str, title: str, status_code: int, *, react: bool = False, document: str | None = None
) -> HTMLResponse:
    headers = {**BASE_HEADERS, "Content-Security-Policy": REACT_CSP if react else PLAIN_CSP}
    page = document if document is not None else PAGE_TEMPLATE.format(title=html.escape(title), body=body)
    return HTMLResponse(page, status_code=status_code, headers=headers)


def _built_react_page() -> str | None:
    try:
        return Path(OAUTH_UI_PATH).read_text(encoding="utf-8")
    except OSError:
        return None


def render_page(
    mode: Mode,
    *,
    state: str = "",
    ticket: str = "",
    client_name: str = "your MCP client",
    redirect_host: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render the password step (``login``) or the paste-your-headers step (``youtube``)."""
    document = _built_react_page()
    if document is not None:
        page_data = json.dumps(
            {
                "state": state,
                "ticket": ticket,
                "mode": mode,
                "clientName": client_name,
                "redirectHost": redirect_host,
                "error": error,
            }
        )
        # Keep the data from ending the script element or being read as markup.
        page_data = page_data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        return _respond(
            "", "", status_code, react=True, document=document.replace(PAGE_DATA_PLACEHOLDER, page_data)
        )

    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    if mode == "youtube":
        body = YOUTUBE_BODY.format(
            error=error_html, state=html.escape(state, quote=True), ticket=html.escape(ticket, quote=True)
        )
        return _respond(body, "Connect YouTube Music", status_code)
    body = LOGIN_BODY.format(
        client_name=html.escape(client_name),
        redirect_host=html.escape(redirect_host),
        state=html.escape(state, quote=True),
        error=error_html,
    )
    return _respond(body, "Authorize access", status_code)


def message_page(message: str, status_code: int = 400) -> HTMLResponse:
    return _respond(
        f"<h1>Authorization error</h1><p>{html.escape(message)}</p>", "Authorization error", status_code
    )


def connected_page() -> HTMLResponse:
    return _respond(
        "<h1>YouTube Music connected</h1><p>You can close this tab and retry your request.</p>",
        "Connected",
        200,
    )
