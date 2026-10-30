"""The interactive playlist manager: an MCP App (React) plus the tools that open it.

UI-capable hosts (Claude, ChatGPT) render the built widget; other hosts still
get the text summary and structured data these tools return. The widget is
built with `npm run build` in widgets/playlist-manager and its single-file
output is committed, so no Node is needed to run the server.
"""

import base64
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, Icon, TextContent
from ytmusicapi import YTMusic

from . import youtube_ops as ops
from .config import PROJECT_DIR

LOGGER = logging.getLogger(__name__)

PLAYLIST_APP_URI = "ui://ytmusic/playlist-manager-v4.html"
PLAYLIST_OPENAI_URI = "ui://ytmusic/playlist-manager-v4-openai.html"
PLAYLIST_V5_APP_URI = "ui://ytmusic/playlist-manager-v5.html"
PLAYLIST_V5_OPENAI_URI = "ui://ytmusic/playlist-manager-v5-openai.html"
PLAYLIST_LEGACY_URI = "ui://ytmusic/playlist-tracks"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
OPENAI_WIDGET_MIME_TYPE = "text/html+skybridge"

WIDGET_PATH = PROJECT_DIR / "widgets" / "playlist-manager" / "dist" / "index.html"
CONNECTOR_ICON_PATH = PROJECT_DIR / "assets" / "connector-icon-96.png"

# Where the widget may load album art from.
ARTWORK_DOMAINS = [
    "https://i.ytimg.com",
    "https://*.ytimg.com",
    "https://yt3.googleusercontent.com",
    "https://*.googleusercontent.com",
    "https://lh3.googleusercontent.com",
    "https://yt3.ggpht.com",
]

MANAGER_TOOL_META = {
    "ui": {"resourceUri": PLAYLIST_APP_URI, "visibility": ["model"]},
    "ui/resourceUri": PLAYLIST_APP_URI,
    "openai/outputTemplate": PLAYLIST_OPENAI_URI,
    "openai/toolInvocation/invoking": "Opening music manager…",
    "openai/toolInvocation/invoked": "Music manager ready",
}
PLAYLIST_TOOL_META = {
    **MANAGER_TOOL_META,
    "openai/toolInvocation/invoking": "Loading playlist…",
    "openai/toolInvocation/invoked": "Playlist ready",
}


def connector_icons() -> list[Icon]:
    """The connector icon as a data URI, or none if the asset is not present."""
    try:
        data = base64.b64encode(CONNECTOR_ICON_PATH.read_bytes()).decode("ascii")
    except OSError:
        LOGGER.warning("Connector icon not found at %s; running without it", CONNECTOR_ICON_PATH)
        return []
    return [Icon(src=f"data:image/png;base64,{data}", mimeType="image/png", sizes=["96x96"])]


@lru_cache(maxsize=1)
def _read_widget() -> str:
    try:
        return Path(WIDGET_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            "Playlist manager is not built. Run `npm run build` in widgets/playlist-manager."
        ) from exc


def playlist_manager_html() -> str:
    """The compiled, self-contained React MCP App."""
    return _read_widget()


# -- tools ----------------------------------------------------------------------------------


def open_music_manager_ui(yt: YTMusic, limit: int = 100) -> CallToolResult:
    """Open the complete YouTube Music playlist manager UI."""
    playlists = ops.list_my_playlists(yt, limit=limit)
    return CallToolResult(
        content=[TextContent(type="text", text=f"Opened playlist manager with {len(playlists)} playlists.")],
        structuredContent={"view": "manager", "playlists": playlists},
    )


def get_playlist_tracks_ui(yt: YTMusic, playlist_id: str) -> CallToolResult:
    """Show every track in a playlist as an interactive track-list UI.

    UI-capable clients render a fast, compact track list with metadata, filtering, and
    YouTube Music links. Other clients still receive text and structured data.
    """
    playlist = ops.get_playlist(yt, playlist_id, limit=None)
    count = len(playlist["tracks"])
    summary = f"Playlist {playlist['title'] or playlist_id}: {count} track{'' if count == 1 else 's'}."
    return CallToolResult(content=[TextContent(type="text", text=summary)], structuredContent=playlist)


# -- resources ------------------------------------------------------------------------------


def register_resources(mcp: FastMCP) -> None:
    """Expose the widget under every URI that MCP Apps hosts and ChatGPT may have cached."""
    modern_meta = {"ui": {"prefersBorder": True, "csp": {"resourceDomains": ARTWORK_DOMAINS}}}

    def openai_meta(description: str) -> dict[str, Any]:
        return {
            "openai/widgetDescription": description,
            "openai/widgetPrefersBorder": True,
            "openai/widgetCSP": {"resource_domains": ARTWORK_DOMAINS},
        }

    full_description = "Manage YouTube Music playlists, tracks, search, and ordering."
    # (uri, name, description, mime type, host metadata)
    widgets = [
        (
            PLAYLIST_APP_URI,
            "YouTube Music playlist tracks",
            "Interactive ordered playlist track browser",
            MCP_APP_MIME_TYPE,
            modern_meta,
        ),
        (
            PLAYLIST_OPENAI_URI,
            "YouTube Music playlist tracks (ChatGPT compatibility)",
            "Interactive ordered playlist track browser",
            OPENAI_WIDGET_MIME_TYPE,
            openai_meta(full_description),
        ),
        (
            PLAYLIST_V5_APP_URI,
            "YouTube Music playlist tracks (v5 compatibility)",
            "Compatibility alias serving the stable playlist manager",
            MCP_APP_MIME_TYPE,
            modern_meta,
        ),
        (
            PLAYLIST_V5_OPENAI_URI,
            "YouTube Music playlist tracks (v5 ChatGPT compatibility)",
            "Compatibility alias serving the stable playlist manager",
            OPENAI_WIDGET_MIME_TYPE,
            openai_meta(full_description),
        ),
        (
            PLAYLIST_LEGACY_URI,
            "YouTube Music playlist tracks (legacy compatibility)",
            "Compatibility alias for previously cached ChatGPT connectors",
            OPENAI_WIDGET_MIME_TYPE,
            openai_meta("Manage YouTube Music playlists and tracks."),
        ),
    ]
    for uri, name, description, mime_type, meta in widgets:
        mcp.resource(uri, name=name, description=description, mime_type=mime_type, meta=meta)(
            playlist_manager_html
        )
