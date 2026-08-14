"""
YouTube Music MCP Server
=========================
Exposes ytmusicapi playlist/track management as MCP tools:
search, create/delete playlists, list playlists, view a playlist,
add tracks, remove tracks, and reorder tracks (move to top/bottom/
before another track).

Auth: reads browser.json (created via `python3 setup_auth.py`, using the
free browser-cookie method) from the same directory, or from the path in
the YTMUSIC_AUTH_FILE env var.

Built on FastMCP (mcp<2.0.0), not the newer MCPServer class, because
mcp-ui-server (used for MCP-UI widget tools) is not yet compatible with
mcp==2.0.0. See requirements.txt for details.
"""

import json
import logging
import os
import sys
import time
from functools import wraps
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from ytmusicapi import YTMusic
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

# Apply the project configuration to direct runs as well as systemd launches.
# Existing process/systemd variables take precedence over values in the file.
load_dotenv(Path(__file__).resolve().with_name(".env"))

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

AUTH_FILE = Path(os.environ.get(
    "YTMUSIC_AUTH_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser.json"),
)).resolve()

OAUTH_PROVIDER = None
if os.environ.get("MCP_TRANSPORT", "stdio") == "http":
    from pydantic import AnyHttpUrl
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from oauth_provider import PasswordOAuthProvider

    public_url = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
    oauth_username = os.environ.get("OAUTH_USERNAME", "")
    oauth_password = os.environ.get("OAUTH_PASSWORD", "")
    if not public_url or not oauth_username or not oauth_password:
        sys.exit("MCP_PUBLIC_URL, OAUTH_USERNAME, and OAUTH_PASSWORD are required in HTTP mode")
    issuer_url = public_url.rsplit("/", 1)[0]
    oauth_database = Path(
        os.environ.get("OAUTH_DB_PATH", str(Path(__file__).resolve().with_name("oauth.sqlite3")))
    )
    OAUTH_PROVIDER = PasswordOAuthProvider(
        issuer_url, oauth_username, oauth_password, "ytmusic", oauth_database
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
    mcp = FastMCP("youtube-music")


if OAUTH_PROVIDER is not None:
    from starlette.requests import Request
    from starlette.responses import Response

    @mcp.custom_route("/login", methods=["GET"])
    async def oauth_login(request: Request) -> Response:
        state = request.query_params.get("state", "")
        return await OAUTH_PROVIDER.login_page(state)

    @mcp.custom_route("/login/callback", methods=["POST"])
    async def oauth_login_callback(request: Request) -> Response:
        return await OAUTH_PROVIDER.login_callback(request)

_yt: YTMusic | None = None


def log_tool_errors(func):
    """Log tool failures with a traceback, then let MCP report them to the client."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            LOGGER.exception("MCP tool %s failed", func.__name__)
            raise

    return wrapper


def load_browser_auth(auth_file: Path = AUTH_FILE) -> dict[str, Any]:
    """Load cookie headers and add ytmusicapi's browser-auth hash in memory."""
    try:
        headers = json.loads(auth_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"No browser-cookie auth file found at {auth_file}. "
            "Run `python3 setup_auth.py` first."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("browser.json is unreadable or invalid JSON.") from exc

    keys = {str(key).lower() for key in headers} if isinstance(headers, dict) else set()
    if {"cookie", "x-goog-authuser"} - keys:
        raise RuntimeError(
            "The auth file is not browser-cookie credentials. OAuth is not supported; "
            "run `python3 setup_auth.py`."
        )
    origin = headers.get("origin") or headers.get("x-origin")
    if not origin:
        raise RuntimeError("browser.json is missing the Origin header; capture a /browse request.")
    try:
        sapisid = sapisid_from_cookie(headers["cookie"])
    except (KeyError, ValueError) as exc:
        raise RuntimeError(
            "The cookie is missing __Secure-3PAPISID; capture headers from a logged-in /browse request."
        ) from exc

    # ytmusicapi 1.12.x needs this to classify the input as browser auth.
    # It is derived from the cookie in memory; no OAuth credentials are used.
    headers["authorization"] = get_authorization(f"{sapisid} {origin}")
    return headers


def get_client() -> YTMusic:
    """Lazily create the YTMusic client so import/list-tools works even
    before auth is set up."""
    global _yt
    if _yt is None:
        headers = load_browser_auth()
        LOGGER.info("Initializing YouTube Music client with browser-cookie authentication")
        _yt = YTMusic(headers)
        LOGGER.info("YouTube Music client initialized")
    return _yt


def _track_summary(track: dict[str, Any]) -> dict[str, Any]:
    artists = ", ".join(a.get("name", "") for a in track.get("artists") or [])
    return {
        "videoId": track.get("videoId"),
        "setVideoId": track.get("setVideoId"),  # needed to remove/reorder
        "title": track.get("title"),
        "artists": artists,
        "album": (track.get("album") or {}).get("name"),
        "duration": track.get("duration"),
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
def get_playlist(playlist_id: str, limit: int = 200) -> dict[str, Any]:
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