"""The YouTube Music operations exposed as MCP tools.

Every function takes a ``YTMusic`` client as its first argument and returns
plain JSON-friendly data. tools.py registers them with the MCP server, which
supplies the right client for the calling user; the docstrings below are what
the AI model reads when choosing a tool.
"""

import time
from typing import Any, Literal

from ytmusicapi import YTMusic

SearchFilter = Literal["songs", "videos", "albums", "artists", "playlists"]
Privacy = Literal["PUBLIC", "PRIVATE", "UNLISTED"]

MAX_RESULTS = 200
MAX_TRACKS_PER_CALL = 100
MOVE_ATTEMPTS = 3


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_RESULTS))


def _check_batch(ids: list[str]) -> None:
    if not ids:
        raise ValueError("Provide at least one ID.")
    if len(ids) > MAX_TRACKS_PER_CALL:
        raise ValueError(f"At most {MAX_TRACKS_PER_CALL} tracks per call; split the request.")


def _artist_names(item: dict[str, Any]) -> str:
    return ", ".join(artist.get("name", "") for artist in item.get("artists") or [])


def thumbnail_url(item: dict[str, Any], target_width: int = 120) -> str | None:
    """Pick the smallest artwork URL that is still at least ``target_width`` wide."""
    thumbnails = (
        item.get("thumbnails")
        or (item.get("thumbnail") or {}).get("thumbnails")
        or ((item.get("videoDetails") or {}).get("thumbnail") or {}).get("thumbnails")
        or []
    )
    candidates = [thumb for thumb in thumbnails if thumb.get("url")]
    if not candidates:
        return None
    sized = [thumb for thumb in candidates if isinstance(thumb.get("width"), int)]
    if not sized:
        return candidates[0]["url"]
    big_enough = [thumb for thumb in sized if thumb["width"] >= target_width]
    chosen = (
        min(big_enough, key=lambda thumb: thumb["width"])
        if big_enough
        else max(sized, key=lambda thumb: thumb["width"])
    )
    return chosen["url"]


def _track_summary(track: dict[str, Any]) -> dict[str, Any]:
    return {
        "videoId": track.get("videoId"),
        "setVideoId": track.get("setVideoId"),  # needed to remove or reorder
        "title": track.get("title"),
        "artists": _artist_names(track),
        "album": (track.get("album") or {}).get("name"),
        "duration": track.get("duration"),
        "thumbnail": thumbnail_url(track),
    }


def _edit_status(result: Any) -> Any:
    return result.get("status") if isinstance(result, dict) else result


# -- search ---------------------------------------------------------------------------


def search_music(
    yt: YTMusic, query: str, filter: SearchFilter = "songs", limit: int = 10
) -> list[dict[str, Any]]:
    """Search YouTube Music for songs, videos, albums, artists, or playlists.

    Returns results with videoId (songs/videos) or browseId (albums/artists) and a
    title. Pass a videoId straight into add_tracks_to_playlist.
    """
    limit = _clamp_limit(limit)
    return [
        {
            "resultType": item.get("resultType"),
            "videoId": item.get("videoId"),
            "browseId": item.get("browseId"),
            "title": item.get("title"),
            "artists": _artist_names(item),
            "duration": item.get("duration"),
            "thumbnail": thumbnail_url(item),
        }
        for item in yt.search(query, filter=filter, limit=limit)[:limit]
    ]


# -- playlists ------------------------------------------------------------------------


def list_my_playlists(yt: YTMusic, limit: int = 50) -> list[dict[str, Any]]:
    """List the current user's playlists (id, title, track count)."""
    return [
        {"playlistId": p.get("playlistId"), "title": p.get("title"), "count": p.get("count")}
        for p in yt.get_library_playlists(limit=_clamp_limit(limit))
    ]


def _fetch_playlist(yt: YTMusic, playlist_id: str, limit: int | None) -> dict[str, Any]:
    try:
        return yt.get_playlist(playlist_id, limit=limit)
    except KeyError as exc:
        # ytmusicapi cannot parse YouTube's minimal response for a brand-new empty
        # playlist. Rebuild its metadata from the user's library instead.
        if "Unable to find 'contents'" not in str(exc):
            raise
        summary = next(
            (p for p in yt.get_library_playlists(limit=None) if p.get("playlistId") == playlist_id), None
        )
        if summary is None:
            raise
        return {
            "id": playlist_id,
            "title": summary.get("title"),
            "description": summary.get("description", ""),
            "privacy": summary.get("privacy", "PRIVATE"),
            "trackCount": 0,
            "tracks": [],
        }


def get_playlist(yt: YTMusic, playlist_id: str, limit: int | None = None) -> dict[str, Any]:
    """Get a playlist's metadata and its ordered tracks (all of them unless limit is set).

    Each track includes 'setVideoId', the ID that remove_tracks_from_playlist and
    the move/reorder tools need (NOT videoId).
    """
    playlist = _fetch_playlist(yt, playlist_id, None if limit is None else max(1, limit))
    return {
        "playlistId": playlist.get("id"),
        "title": playlist.get("title"),
        "description": playlist.get("description"),
        "privacy": playlist.get("privacy"),
        "trackCount": playlist.get("trackCount"),
        "tracks": [_track_summary(track) for track in playlist.get("tracks", [])],
    }


def create_playlist(
    yt: YTMusic, title: str, description: str = "", privacy_status: Privacy = "PRIVATE"
) -> dict[str, Any]:
    """Create a new playlist. privacy_status is PUBLIC, PRIVATE, or UNLISTED."""
    playlist_id = yt.create_playlist(title, description, privacy_status=privacy_status)
    return {"playlistId": playlist_id, "title": title}


def delete_playlist(yt: YTMusic, playlist_id: str) -> dict[str, Any]:
    """Permanently delete a playlist you own. This cannot be undone."""
    yt.delete_playlist(playlist_id)
    return {"deleted": playlist_id}


def rename_playlist(
    yt: YTMusic,
    playlist_id: str,
    title: str | None = None,
    description: str | None = None,
    privacy_status: Privacy | None = None,
) -> dict[str, Any]:
    """Edit a playlist's title, description, and/or privacy status."""
    yt.edit_playlist(playlist_id, title=title, description=description, privacyStatus=privacy_status)
    return {"updated": playlist_id}


# -- adding and removing tracks --------------------------------------------------------


def add_tracks_to_playlist(
    yt: YTMusic,
    playlist_id: str,
    video_ids: list[str],
    add_to_top: bool = False,
    allow_duplicates: bool = False,
) -> dict[str, Any]:
    """Add one or more tracks (videoIds from search_music) to a playlist.

    add_to_top: insert the new tracks at the top instead of appending them.
    """
    _check_batch(video_ids)
    if add_to_top:
        yt.edit_playlist(playlist_id, addToTop=True)
    try:
        result = yt.add_playlist_items(playlist_id, video_ids, duplicates=allow_duplicates)
    finally:
        if add_to_top:
            # addToTop is stored on the playlist, so switch it off again to keep
            # this option scoped to the current call.
            yt.edit_playlist(playlist_id, addToTop=False)
    return {"requested": len(video_ids), "status": _edit_status(result)}


def remove_tracks_from_playlist(yt: YTMusic, playlist_id: str, set_video_ids: list[str]) -> dict[str, Any]:
    """Remove tracks from a playlist.

    set_video_ids must be each track's 'setVideoId' (from get_playlist), NOT its
    videoId: the same song can appear several times with different setVideoIds.
    """
    _check_batch(set_video_ids)
    playlist = yt.get_playlist(playlist_id, limit=None)
    video_by_set_id = {
        track["setVideoId"]: track["videoId"]
        for track in playlist.get("tracks", [])
        if track.get("setVideoId")
    }
    found = [set_id for set_id in set_video_ids if set_id in video_by_set_id]
    if found:
        yt.remove_playlist_items(
            playlist_id, [{"videoId": video_by_set_id[set_id], "setVideoId": set_id} for set_id in found]
        )
    return {
        "removed": len(found),
        "notFound": [set_id for set_id in set_video_ids if set_id not in video_by_set_id],
    }


# -- reordering ------------------------------------------------------------------------


def _move_item(
    yt: YTMusic, playlist_id: str, set_video_id: str, before_set_video_id: str | None = None
) -> None:
    """Move a track before another one, or to the end when no target is given.

    YouTube sometimes answers a move with HTTP 409 while an earlier edit is still
    settling, so the call is retried a couple of times.
    """
    move = (set_video_id, before_set_video_id) if before_set_video_id else set_video_id
    for attempt in range(1, MOVE_ATTEMPTS + 1):
        try:
            yt.edit_playlist(playlist_id, moveItem=move)
            return
        except Exception as exc:
            if "409" not in str(exc) or attempt == MOVE_ATTEMPTS:
                raise
            time.sleep(attempt)


def reorder_playlist_track(
    yt: YTMusic, playlist_id: str, set_video_id: str, before_set_video_id: str | None = None
) -> dict[str, Any]:
    """Move a track within a playlist.

    set_video_id: the track to move (its setVideoId, from get_playlist).
    before_set_video_id: the setVideoId of the track it should end up directly in
    front of. Omit it to move the track to the very end.
    """
    _move_item(yt, playlist_id, set_video_id, before_set_video_id)
    return {"moved": set_video_id, "before": before_set_video_id or "end of playlist"}


def move_track_to_top(yt: YTMusic, playlist_id: str, set_video_id: str) -> dict[str, Any]:
    """Move a single track to the very top of the playlist."""
    tracks = yt.get_playlist(playlist_id, limit=1).get("tracks", [])
    if not tracks:
        return {"moved": None, "note": "The playlist is empty."}
    first = tracks[0].get("setVideoId")
    if first == set_video_id:
        return {"moved": None, "note": "The track is already at the top."}
    _move_item(yt, playlist_id, set_video_id, first)
    return {"moved": set_video_id, "before": first}


def move_track_to_bottom(yt: YTMusic, playlist_id: str, set_video_id: str) -> dict[str, Any]:
    """Move a single track to the very bottom of the playlist."""
    _move_item(yt, playlist_id, set_video_id)
    return {"moved": set_video_id, "before": "end of playlist"}
