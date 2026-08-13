"""
YouTube Music MCP Server
=========================
Exposes ytmusicapi playlist/track management as MCP tools:
search, create/delete playlists, list playlists, view a playlist,
add tracks, remove tracks, and reorder tracks (move to top/bottom/
before another track).

Auth: reads browser.json (created via `python setup_auth.py`, using the
browser-cookie method) from the same directory. OAuth is not supported.
"""

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from ytmusicapi import YTMusic
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

from logging_config import configure_logging

configure_logging()
LOGGER = logging.getLogger(__name__)

AUTH_FILE = Path(__file__).resolve().with_name("browser.json")

mcp = MCPServer("youtube-music")

_yt: YTMusic | None = None


def load_browser_auth(auth_file: Path = AUTH_FILE) -> dict[str, Any]:
    """Load cookie headers and add the browser authorization hash in memory."""
    try:
        headers = json.loads(auth_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        LOGGER.error("Browser authentication file not found: %s", auth_file)
        raise RuntimeError(
            f"No browser-cookie auth file found at {auth_file}. "
            "Run `python setup_auth.py` first."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("Browser authentication file is unreadable or invalid JSON")
        raise RuntimeError("browser.json is invalid; run `python setup_auth.py` again.") from exc

    keys = {str(key).lower() for key in headers} if isinstance(headers, dict) else set()
    missing = {"cookie", "x-goog-authuser"} - keys
    if missing:
        LOGGER.error("Auth file is not a browser-cookie header file")
        raise RuntimeError(
            "browser.json does not contain browser-cookie authentication. "
            "OAuth files are not supported; run `python setup_auth.py`."
        )

    origin = headers.get("origin") or headers.get("x-origin")
    if not origin:
        raise RuntimeError(
            "browser.json is missing the Origin header; run `python setup_auth.py` again."
        )
    try:
        sapisid = sapisid_from_cookie(headers["cookie"])
    except (KeyError, ValueError) as exc:
        raise RuntimeError(
            "The browser cookie is missing __Secure-3PAPISID; capture headers "
            "from a logged-in /browse request."
        ) from exc

    # ytmusicapi 1.12.x uses this header to classify the credentials as
    # browser auth. It refreshes the timestamped hash for later requests.
    headers["authorization"] = get_authorization(f"{sapisid} {origin}")
    return headers


def get_client() -> YTMusic:
    """Lazily create the YTMusic client so import/list-tools works even
    before auth is set up."""
    global _yt
    if _yt is None:
        headers = load_browser_auth()
        LOGGER.info("Initializing YouTube Music client")
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
def get_playlist(playlist_id: str, limit: int = 200) -> dict[str, Any]:
    """Get a playlist's metadata and its ordered list of tracks.

    Each track includes 'setVideoId', which is the ID you must use with
    remove_tracks_from_playlist and reorder_playlist_track (NOT videoId).
    """
    yt = get_client()
    pl = yt.get_playlist(playlist_id, limit=limit)
    return {
        "playlistId": pl.get("id"),
        "title": pl.get("title"),
        "description": pl.get("description"),
        "privacy": pl.get("privacy"),
        "trackCount": pl.get("trackCount"),
        "tracks": [_track_summary(t) for t in pl.get("tracks", [])],
    }


@mcp.tool()
def create_playlist(
    title: str, description: str = "", privacy_status: str = "PRIVATE"
) -> dict[str, Any]:
    """Create a new playlist. privacy_status: PUBLIC, PRIVATE, or UNLISTED."""
    yt = get_client()
    playlist_id = yt.create_playlist(title, description, privacyStatus=privacy_status)
    return {"playlistId": playlist_id, "title": title}


@mcp.tool()
def delete_playlist(playlist_id: str) -> str:
    """Permanently delete a playlist you own."""
    yt = get_client()
    yt.delete_playlist(playlist_id)
    return f"Deleted playlist {playlist_id}"


@mcp.tool()
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
def add_tracks_to_playlist(
    playlist_id: str, video_ids: list[str], add_to_top: bool = False, allow_duplicates: bool = False
) -> str:
    """Add one or more tracks (by videoId, from search_music) to a playlist.

    add_to_top: if True, new tracks are inserted at the top instead of appended.
    """
    yt = get_client()
    result = yt.add_playlist_items(
        playlist_id, video_ids, duplicates=allow_duplicates
    )
    if add_to_top:
        yt.edit_playlist(playlist_id, addToTop=True)
    return f"Added {len(video_ids)} track(s): {result}"


@mcp.tool()
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
        yt.edit_playlist(playlist_id, moveItem=(set_video_id, before_set_video_id))
        return f"Moved {set_video_id} before {before_set_video_id}"
    else:
        yt.edit_playlist(playlist_id, moveItem=set_video_id)
        return f"Moved {set_video_id} to the end of the playlist"


@mcp.tool()
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
def move_track_to_bottom(playlist_id: str, set_video_id: str) -> str:
    """Move a single track to the very bottom of the playlist."""
    yt = get_client()
    yt.edit_playlist(playlist_id, moveItem=set_video_id)
    return f"Moved {set_video_id} to the bottom"


if __name__ == "__main__":
    LOGGER.info("Starting YouTube Music MCP server with stdio transport")
    mcp.run(transport="stdio")
