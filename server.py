"""
YouTube Music MCP Server
=========================
Exposes ytmusicapi playlist/track management as MCP tools:
search, create/delete playlists, list playlists, view a playlist,
add tracks, remove tracks, and reorder tracks (move to top/bottom/
before another track).

Auth: every OAuth grant owns an encrypted YouTube Music browser session.
No process-global browser.json credentials are used.

Built on FastMCP and the standard MCP Apps resource protocol.
"""

import base64
import logging
import os
import socket
import sys
import time
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import CallToolResult, Icon, TextContent
from ytmusicapi import YTMusic
import urllib3.util.connection as urllib3_connection


# Apply the project configuration to direct runs as well as systemd launches.
# Existing process/systemd variables take precedence over values in the file.
load_dotenv(Path(__file__).resolve().with_name(".env"))

# This deployment intentionally disables IPv6. Keep all requests-based
# outbound traffic on IPv4 instead of waiting on unreachable IPv6 addresses.
urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

logging.basicConfig(
    level=os.environ.get("YTMUSIC_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
LOGGER = logging.getLogger(__name__)

# DEBUG is useful for this application's diagnostics, but dependency DEBUG logs
# include raw JSON-RPC/SSE payloads and overwhelm journald. They can be enabled
# separately when protocol-level troubleshooting is actually needed.
LIBRARY_LOG_LEVEL = os.environ.get("MCP_LIBRARY_LOG_LEVEL", "INFO").upper()
for logger_name in ("mcp", "sse_starlette", "httpx", "httpcore"):
    logging.getLogger(logger_name).setLevel(LIBRARY_LOG_LEVEL)

PLAYLIST_APP_URI = "ui://ytmusic/playlist-manager-v4.html"
PLAYLIST_OPENAI_URI = "ui://ytmusic/playlist-manager-v4-openai.html"
PLAYLIST_LEGACY_URI = "ui://ytmusic/playlist-tracks"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
OPENAI_WIDGET_MIME_TYPE = "text/html+skybridge"
WIDGET_PATH = Path(__file__).resolve().parent / "widgets" / "playlist-manager" / "dist" / "index.html"
CONNECTOR_ICON_PATH = Path(__file__).resolve().parent / "assets" / "connector-icon-96.png"
CONNECTOR_ICONS = [Icon(
    src="data:image/png;base64," + base64.b64encode(CONNECTOR_ICON_PATH.read_bytes()).decode("ascii"),
    mimeType="image/png",
    sizes=["96x96"],
)]

@lru_cache(maxsize=1)
def playlist_manager_html() -> str:
    """Load the compiled, self-contained React MCP App."""
    try:
        return WIDGET_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "Playlist manager is not built. Run `npm run build` in widgets/playlist-manager."
        ) from exc

OAUTH_PROVIDER = None
if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
    from pydantic import AnyHttpUrl
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from oauth_provider import PasswordOAuthProvider

    public_url = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
    credential_key = os.environ.get("YTMUSIC_CREDENTIAL_KEY", "")
    if not public_url or not credential_key:
        sys.exit("MCP_PUBLIC_URL and YTMUSIC_CREDENTIAL_KEY are required in HTTP mode")
    issuer_url = public_url.rsplit("/", 1)[0]
    oauth_database = Path(
        os.environ.get("OAUTH_DB_PATH", str(Path(__file__).resolve().with_name("oauth.sqlite3")))
    )
    OAUTH_PROVIDER = PasswordOAuthProvider(
        issuer_url, "ytmusic", oauth_database, credential_key
    )
    static_client_id = os.environ.get("OAUTH_CLIENT_ID", "")
    static_client_secret = os.environ.get("OAUTH_CLIENT_SECRET", "")
    static_redirect_uris = [
        value.strip()
        for value in os.environ.get("OAUTH_REDIRECT_URIS", "").split(",")
        if value.strip()
    ]
    if any((static_client_id, static_client_secret, static_redirect_uris)):
        if not all((static_client_id, static_client_secret, static_redirect_uris)):
            sys.exit(
                "OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, and OAUTH_REDIRECT_URIS "
                "must all be set when using a static OAuth client"
            )
        OAUTH_PROVIDER.add_static_client(
            static_client_id, static_client_secret, static_redirect_uris
        )
    mcp = FastMCP(
        "youtube-music",
        icons=CONNECTOR_ICONS,
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8787")),
        auth_server_provider=OAUTH_PROVIDER,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(issuer_url),
            resource_server_url=AnyHttpUrl(public_url),
            required_scopes=["ytmusic"],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["ytmusic"],
                default_scopes=["ytmusic"],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
else:
    mcp = FastMCP("youtube-music", icons=CONNECTOR_ICONS)


@mcp.resource(
    PLAYLIST_APP_URI,
    name="YouTube Music playlist tracks",
    description="Interactive ordered playlist track browser",
    mime_type=MCP_APP_MIME_TYPE,
    meta={
        "ui": {
            "prefersBorder": True,
            "csp": {"resourceDomains": [
                "https://i.ytimg.com", "https://*.ytimg.com", "https://yt3.googleusercontent.com",
                "https://*.googleusercontent.com", "https://lh3.googleusercontent.com", "https://yt3.ggpht.com"
            ]},
        }
    },
)
def playlist_tracks_app_resource() -> str:
    return playlist_manager_html()


@mcp.resource(
    PLAYLIST_OPENAI_URI,
    name="YouTube Music playlist tracks (ChatGPT compatibility)",
    description="Interactive ordered playlist track browser",
    mime_type=OPENAI_WIDGET_MIME_TYPE,
    meta={
        "openai/widgetDescription": "Manage YouTube Music playlists, tracks, search, and ordering.",
        "openai/widgetPrefersBorder": True,
        "openai/widgetCSP": {"resource_domains": [
            "https://i.ytimg.com", "https://*.ytimg.com", "https://yt3.googleusercontent.com",
                "https://*.googleusercontent.com", "https://lh3.googleusercontent.com", "https://yt3.ggpht.com"
        ]},
    },
)
def playlist_tracks_openai_resource() -> str:
    return playlist_manager_html()


@mcp.resource(
    PLAYLIST_LEGACY_URI,
    name="YouTube Music playlist tracks (legacy compatibility)",
    description="Compatibility alias for previously cached ChatGPT connectors",
    mime_type=OPENAI_WIDGET_MIME_TYPE,
    meta={
        "openai/widgetDescription": "Manage YouTube Music playlists and tracks.",
        "openai/widgetPrefersBorder": True,
        "openai/widgetCSP": {"resource_domains": [
            "https://i.ytimg.com", "https://*.ytimg.com", "https://yt3.googleusercontent.com",
            "https://*.googleusercontent.com", "https://lh3.googleusercontent.com", "https://yt3.ggpht.com"
        ]},
    },
)
def playlist_tracks_legacy_resource() -> str:
    return playlist_manager_html()


if OAUTH_PROVIDER is not None:
    from starlette.requests import Request
    from starlette.responses import Response

    @mcp.custom_route("/login", methods=["GET"])
    async def oauth_login(request: Request) -> Response:
        return await OAUTH_PROVIDER.login_page(request.query_params.get("state", ""))

    @mcp.custom_route("/login/callback", methods=["POST"])
    async def oauth_login_callback(request: Request) -> Response:
        return await OAUTH_PROVIDER.login_callback(request)

    from starlette.requests import Request
    from starlette.responses import Response

    @mcp.custom_route("/youtube-auth", methods=["GET"])
    async def youtube_auth(request: Request) -> Response:
        return await OAUTH_PROVIDER.youtube_auth_page(
            request.query_params.get("state", ""), request.query_params.get("ticket", "")
        )

    @mcp.custom_route("/youtube-auth/callback", methods=["POST"])
    async def youtube_auth_callback(request: Request) -> Response:
        return await OAUTH_PROVIDER.youtube_auth_callback(request)


_yt_clients: dict[str, YTMusic] = {}

def _oauth_subject() -> str:
    token = get_access_token()
    if token is None or not token.subject:
        raise RuntimeError("An authenticated OAuth user is required for YouTube Music tools.")
    return token.subject


def log_tool_errors(func):
    """Log tool failures with a traceback, then let MCP report them to the client."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            LOGGER.exception("MCP tool %s failed", func.__name__)
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            message = str(exc).lower()
            auth_markers = ("unauthorized", "authentication required", "login required", "sapisid")
            if status_code in (401, 403) or any(marker in message for marker in auth_markers):
                token = get_access_token()
                subject = token.subject if token is not None else None
                if subject and OAUTH_PROVIDER is not None:
                    OAUTH_PROVIDER.store.invalidate_youtube_credentials(
                        subject, "YouTube authentication rejected"
                    )
                    _yt_clients.pop(subject, None)
                    setup_url = OAUTH_PROVIDER.reconnect_url(subject)
                    raise RuntimeError(
                        "Your YouTube Music browser session expired. "
                        f"Open {setup_url} to paste fresh request headers, then retry."
                    ) from exc
            raise

    return wrapper


def get_client() -> YTMusic:
    """Return the YouTube Music session belonging to this OAuth grant."""
    subject = _oauth_subject()
    cached = _yt_clients.get(subject)
    if cached is not None:
        return cached
    if OAUTH_PROVIDER is None:
        raise RuntimeError("Per-user YouTube Music sessions require HTTP OAuth mode.")
    headers = OAUTH_PROVIDER.youtube_headers(subject)
    if headers is None:
        url = OAUTH_PROVIDER.reconnect_url(subject)
        raise RuntimeError(f"YouTube Music is not connected. Open {url} to paste fresh browser request headers.")
    client = YTMusic(headers)
    _yt_clients[subject] = client
    return client


def _thumbnail_url(item: dict[str, Any], target_width: int = 120) -> str | None:
    """Choose the smallest useful official artwork URL."""
    thumbnail_data = (
        item.get("thumbnails")
        or (item.get("thumbnail") or {}).get("thumbnails")
        or ((item.get("videoDetails") or {}).get("thumbnail") or {}).get("thumbnails")
        or []
    )
    candidates = [thumb for thumb in thumbnail_data if thumb.get("url")]
    if not candidates:
        return None
    with_width = [thumb for thumb in candidates if isinstance(thumb.get("width"), int)]
    if not with_width:
        return candidates[0]["url"]
    large_enough = [thumb for thumb in with_width if thumb["width"] >= target_width]
    chosen = min(large_enough, key=lambda thumb: thumb["width"]) if large_enough else max(
        with_width, key=lambda thumb: thumb["width"]
    )
    return chosen["url"]


def _track_summary(track: dict[str, Any]) -> dict[str, Any]:
    artists = ", ".join(a.get("name", "") for a in track.get("artists") or [])
    return {
        "videoId": track.get("videoId"),
        "setVideoId": track.get("setVideoId"),  # needed to remove/reorder
        "title": track.get("title"),
        "artists": artists,
        "album": (track.get("album") or {}).get("name"),
        "duration": track.get("duration"),
        "thumbnail": _thumbnail_url(track),
    }


@mcp.tool()
def reconnect_youtube_music() -> dict[str, str]:
    """Get a private setup link when YouTube Music cookies are missing or expired."""
    subject = _oauth_subject()
    if OAUTH_PROVIDER is None:
        raise RuntimeError("YouTube Music reconnect requires HTTP OAuth mode.")
    return {
        "status": "connected" if OAUTH_PROVIDER.youtube_headers(subject) else "reconnect_required",
        "setupUrl": OAUTH_PROVIDER.reconnect_url(subject),
        "instructions": "Open setupUrl, copy fresh request headers from a music.youtube.com /youtubei/v1/next request, and paste them into the private form.",
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@mcp.tool()
@log_tool_errors
def search_music(query: str, filter: str = "songs", limit: int = 10) -> list[dict[str, Any]]:
    """Search YouTube Music for songs, videos, albums, artists, or playlists.

    filter: one of "songs", "videos", "albums", "artists", "playlists" (default "songs").
    Returns a list of results with at least videoId/browseId and title, so you can
    pass the videoId straight into add_tracks_to_playlist.
    """
    yt = get_client()
    results = yt.search(query, filter=filter, limit=limit)
    out = []
    for r in results[:limit]:
        out.append(
            {
                "resultType": r.get("resultType"),
                "videoId": r.get("videoId"),
                "browseId": r.get("browseId"),
                "title": r.get("title"),
                "artists": ", ".join(a.get("name", "") for a in r.get("artists") or []),
                "duration": r.get("duration"),
                "thumbnail": _thumbnail_url(r),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Playlist-level management
# ---------------------------------------------------------------------------


@mcp.tool()
@log_tool_errors
def list_my_playlists(limit: int = 50) -> list[dict[str, Any]]:
    """List the current user's playlists (id, title, track count)."""
    yt = get_client()
    playlists = yt.get_library_playlists(limit=limit)
    return [
        {
            "playlistId": p.get("playlistId"),
            "title": p.get("title"),
            "count": p.get("count"),
        }
        for p in playlists
    ]


@mcp.tool()
@log_tool_errors
def get_playlist(playlist_id: str, limit: int | None = None) -> dict[str, Any]:
    """Get a playlist's metadata and its ordered list of tracks.

    Each track includes 'setVideoId', which is the ID you must use with
    remove_tracks_from_playlist and reorder_playlist_track (NOT videoId).
    """
    yt = get_client()
    try:
        pl = yt.get_playlist(playlist_id, limit=limit)
    except KeyError as exc:
        # ytmusicapi 1.12 cannot parse YouTube's minimal response for a newly
        # created empty playlist. Recover its metadata from the user's library.
        if "Unable to find 'contents'" not in str(exc):
            raise
        summary = next(
            (
                item
                for item in yt.get_library_playlists(limit=None)
                if item.get("playlistId") == playlist_id
            ),
            None,
        )
        if summary is None:
            raise
        pl = {
            "id": playlist_id,
            "title": summary.get("title"),
            "description": summary.get("description", ""),
            "privacy": summary.get("privacy", "PRIVATE"),
            "trackCount": 0,
            "tracks": [],
        }
    return {
        "playlistId": pl.get("id"),
        "title": pl.get("title"),
        "description": pl.get("description"),
        "privacy": pl.get("privacy"),
        "trackCount": pl.get("trackCount"),
        "tracks": [_track_summary(t) for t in pl.get("tracks", [])],
    }


_MANAGER_TOOL_META = {
    "ui": {"resourceUri": PLAYLIST_APP_URI, "visibility": ["model"]},
    "ui/resourceUri": PLAYLIST_APP_URI,
    "openai/outputTemplate": PLAYLIST_OPENAI_URI,
    "openai/toolInvocation/invoking": "Opening music manager…",
    "openai/toolInvocation/invoked": "Music manager ready",
}


@mcp.tool(meta=_MANAGER_TOOL_META)
@log_tool_errors
def open_music_manager_ui(limit: int = 100) -> CallToolResult:
    """Open the complete YouTube Music playlist manager UI."""
    data = {"view": "manager", "playlists": list_my_playlists(limit=limit)}
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Opened playlist manager with {len(data['playlists'])} playlists.",
            ),
        ],
        structuredContent=data,
    )


@mcp.tool(
    meta={
        **_MANAGER_TOOL_META,
        "openai/toolInvocation/invoking": "Loading playlist…",
        "openai/toolInvocation/invoked": "Playlist ready",
    }
)
@log_tool_errors
def get_playlist_tracks_ui(playlist_id: str, limit: int | None = None) -> CallToolResult:
    """Show a playlist as an interactive track-list UI.

    UI-capable clients render a fast, compact track list with metadata, filtering, and
    YouTube Music links. Other clients still receive text and structured data.
    """
    playlist = get_playlist(playlist_id, limit=limit)
    track_count = len(playlist["tracks"])
    summary = (
        f"Playlist {playlist['title'] or playlist_id}: "
        f"{track_count} track{'' if track_count == 1 else 's'}."
    )
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent=playlist,
    )


@mcp.tool()
@log_tool_errors
def create_playlist(
    title: str, description: str = "", privacy_status: str = "PRIVATE"
) -> dict[str, Any]:
    """Create a new playlist. privacy_status: PUBLIC, PRIVATE, or UNLISTED."""
    yt = get_client()
    playlist_id = yt.create_playlist(title, description, privacy_status=privacy_status)
    return {"playlistId": playlist_id, "title": title}


@mcp.tool()
@log_tool_errors
def delete_playlist(playlist_id: str) -> str:
    """Permanently delete a playlist you own."""
    yt = get_client()
    yt.delete_playlist(playlist_id)
    return f"Deleted playlist {playlist_id}"


@mcp.tool()
@log_tool_errors
def rename_playlist(
    playlist_id: str,
    title: str | None = None,
    description: str | None = None,
    privacy_status: str | None = None,
) -> str:
    """Edit a playlist's title, description, and/or privacy status."""
    yt = get_client()
    yt.edit_playlist(
        playlist_id, title=title, description=description, privacyStatus=privacy_status
    )
    return f"Updated playlist {playlist_id}"


# ---------------------------------------------------------------------------
# Track add/remove
# ---------------------------------------------------------------------------


@mcp.tool()
@log_tool_errors
def add_tracks_to_playlist(
    playlist_id: str, video_ids: list[str], add_to_top: bool = False, allow_duplicates: bool = False
) -> str:
    """Add one or more tracks (by videoId, from search_music) to a playlist.

    add_to_top: if True, new tracks are inserted at the top instead of appended.
    """
    yt = get_client()
    if add_to_top:
        yt.edit_playlist(playlist_id, addToTop=True)
    try:
        result = yt.add_playlist_items(
            playlist_id, video_ids, duplicates=allow_duplicates
        )
    finally:
        if add_to_top:
            # addToTop is persistent playlist state; restore normal append behavior
            # so this option affects only the current tool call.
            yt.edit_playlist(playlist_id, addToTop=False)
    return f"Added {len(video_ids)} track(s): {result}"


@mcp.tool()
@log_tool_errors
def remove_tracks_from_playlist(playlist_id: str, set_video_ids: list[str]) -> str:
    """Remove tracks from a playlist.

    set_video_ids must be each track's 'setVideoId' (get it from get_playlist),
    NOT its videoId — the same song can appear multiple times in a playlist
    with different setVideoIds.
    """
    yt = get_client()
    videos = [{"setVideoId": svid} for svid in set_video_ids]
    # ytmusicapi also wants videoId for each item when available; fetch playlist
    # to map setVideoId -> videoId for a reliable removal payload.
    pl = yt.get_playlist(playlist_id, limit=None)
    lookup = {t["setVideoId"]: t["videoId"] for t in pl.get("tracks", []) if t.get("setVideoId")}
    videos = [
        {"videoId": lookup[svid], "setVideoId": svid} for svid in set_video_ids if svid in lookup
    ]
    if not videos:
        return "No matching tracks found for the given setVideoIds."
    result = yt.remove_playlist_items(playlist_id, videos)
    return f"Removed {len(videos)} track(s): {result}"


# ---------------------------------------------------------------------------
# Reordering
# ---------------------------------------------------------------------------


@mcp.tool()
@log_tool_errors
def reorder_playlist_track(
    playlist_id: str, set_video_id: str, before_set_video_id: str | None = None
) -> str:
    """Move a track within a playlist.

    set_video_id: the track to move (its setVideoId, from get_playlist).
    before_set_video_id: the setVideoId of the track it should be moved
      directly in front of. Omit this to move the track to the very end
      of the playlist instead.
    """
    yt = get_client()
    if before_set_video_id:
        last_error = None
        for attempt in range(3):
            try:
                yt.edit_playlist(playlist_id, moveItem=(set_video_id, before_set_video_id))
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if "409" not in str(exc) or attempt == 2:
                    raise
                time.sleep(attempt + 1)
        if last_error is not None:
            raise last_error
        return f"Moved {set_video_id} before {before_set_video_id}"
    else:
        yt.edit_playlist(playlist_id, moveItem=set_video_id)
        return f"Moved {set_video_id} to the end of the playlist"


@mcp.tool()
@log_tool_errors
def move_track_to_top(playlist_id: str, set_video_id: str) -> str:
    """Move a single track to the very top of the playlist."""
    yt = get_client()
    pl = yt.get_playlist(playlist_id, limit=1)
    tracks = pl.get("tracks", [])
    if not tracks:
        return "Playlist is empty."
    first_svid = tracks[0].get("setVideoId")
    if first_svid == set_video_id:
        return "Track is already at the top."
    yt.edit_playlist(playlist_id, moveItem=(set_video_id, first_svid))
    return f"Moved {set_video_id} to the top"


@mcp.tool()
@log_tool_errors
def move_track_to_bottom(playlist_id: str, set_video_id: str) -> str:
    """Move a single track to the very bottom of the playlist."""
    yt = get_client()
    yt.edit_playlist(playlist_id, moveItem=set_video_id)
    return f"Moved {set_video_id} to the bottom"


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")

    if transport == "http":
        # --- Remote hosting mode -------------------------------------------------
        # Serves the server over Streamable HTTP (what Claude/ChatGPT custom
        # connectors speak) at http://HOST:PORT/mcp, protected by OAuth.
        import uvicorn
        from starlette.applications import Starlette

        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8787"))

        # FastMCP takes host/port at construction time (set above), and
        # streamable_http_app() takes no arguments in this SDK version.
        app: Starlette = mcp.streamable_http_app()

        LOGGER.info(
            "Serving MCP over Streamable HTTP on http://%s:%s/mcp (OAuth protected)",
            host,
            port,
        )
        uvicorn.run(app, host=host, port=port)
    else:
        # --- Local mode (Claude Desktop config launches this directly) -----------
        mcp.run(transport="stdio")