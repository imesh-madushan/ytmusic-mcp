"""End-to-end tests against the configured real YouTube Music account.

The numbered tests intentionally form one workflow to minimize mutations. A
private, uniquely named playlist is created and deleted by the session fixture.
"""

from __future__ import annotations

import time
import uuid

import pytest

import server


pytestmark = pytest.mark.youtube_integration


def eventually_playlist(playlist_id: str, predicate, timeout: float = 15.0) -> dict:
    """Poll briefly because YouTube playlist writes can be eventually consistent."""
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        try:
            latest = server.get_playlist(playlist_id, limit=200)
        except KeyError as exc:
            # A newly created empty playlist can briefly be absent from both
            # its browse response and the account's library listing.
            if "Unable to find 'contents'" not in str(exc):
                raise
            time.sleep(1)
            continue
        if predicate(latest):
            return latest
        time.sleep(1)
    pytest.fail(f"YouTube Music did not reach the expected playlist state; latest={latest!r}")


def require_playlist(state: dict) -> str:
    """Skip dependent workflow steps when real playlist creation failed."""
    if not state.get("id"):
        pytest.skip("requires the private playlist created by test_04")
    return state["id"]


@pytest.fixture(scope="module")
def real_playlist() -> dict:
    state: dict = {
        "id": None,
        "name": f"ytmusic-mcp-integration-{uuid.uuid4().hex[:10]}",
        "video_ids": [],
    }
    yield state
    if state["id"]:
        try:
            server.delete_playlist(state["id"])
        except Exception:
            server.LOGGER.exception("Cleanup failed for integration playlist %s", state["id"])


def test_01_search_music_uses_real_youtube(real_playlist: dict) -> None:
    results = server.search_music("Never Gonna Give You Up Rick Astley", limit=5)
    video_ids = [item["videoId"] for item in results if item.get("videoId")]
    assert video_ids
    real_playlist["video_ids"].append(video_ids[0])


def test_02_search_collects_distinct_tracks(real_playlist: dict) -> None:
    for query in ("Take On Me a-ha", "Africa Toto"):
        results = server.search_music(query, limit=5)
        match = next(
            (item["videoId"] for item in results if item.get("videoId") not in real_playlist["video_ids"]),
            None,
        )
        assert match, f"No distinct playable result for {query!r}"
        real_playlist["video_ids"].append(match)
    assert len(real_playlist["video_ids"]) == 3


def test_03_list_my_playlists(real_playlist: dict) -> None:
    playlists = server.list_my_playlists(limit=5)
    assert isinstance(playlists, list)
    assert all("playlistId" in playlist and "title" in playlist for playlist in playlists)


def test_04_create_private_playlist(real_playlist: dict) -> None:
    created = server.create_playlist(real_playlist["name"], "Temporary MCP integration test", "PRIVATE")
    assert created["playlistId"]
    real_playlist["id"] = created["playlistId"]
    playlist = eventually_playlist(real_playlist["id"], lambda value: value.get("title") == real_playlist["name"])
    assert playlist["privacy"] in ("PRIVATE", None)


def test_05_get_playlist(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    playlist = server.get_playlist(playlist_id)
    assert playlist["playlistId"] == playlist_id
    assert playlist["title"] == real_playlist["name"]
    assert playlist["tracks"] == []


def test_06_rename_playlist(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    renamed = real_playlist["name"] + "-renamed"
    result = server.rename_playlist(
        playlist_id, title=renamed, description="Renamed by integration test", privacy_status="PRIVATE"
    )
    assert "Updated playlist" in result
    eventually_playlist(playlist_id, lambda value: value.get("title") == renamed)
    real_playlist["name"] = renamed


def test_07_add_tracks_to_playlist(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    result = server.add_tracks_to_playlist(playlist_id, real_playlist["video_ids"][:2])
    assert "Added 2 track(s)" in result
    playlist = eventually_playlist(playlist_id, lambda value: len(value.get("tracks", [])) == 2)
    assert [track["videoId"] for track in playlist["tracks"]] == real_playlist["video_ids"][:2]


def test_08_add_track_to_top(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    result = server.add_tracks_to_playlist(
        playlist_id, [real_playlist["video_ids"][2]], add_to_top=True
    )
    assert "Added 1 track(s)" in result
    playlist = eventually_playlist(playlist_id, lambda value: len(value.get("tracks", [])) == 3)
    assert playlist["tracks"][0]["videoId"] == real_playlist["video_ids"][2]


def test_09_reorder_playlist_track(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    playlist = server.get_playlist(playlist_id)
    moved = playlist["tracks"][-1]["setVideoId"]
    before = playlist["tracks"][0]["setVideoId"]
    server.reorder_playlist_track(playlist_id, moved, before)
    playlist = eventually_playlist(playlist_id, lambda value: value["tracks"][0]["setVideoId"] == moved)
    assert playlist["tracks"][0]["setVideoId"] == moved


def test_10_move_track_to_bottom(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    playlist = server.get_playlist(playlist_id)
    moved = playlist["tracks"][0]["setVideoId"]
    server.move_track_to_bottom(playlist_id, moved)
    playlist = eventually_playlist(playlist_id, lambda value: value["tracks"][-1]["setVideoId"] == moved)
    assert playlist["tracks"][-1]["setVideoId"] == moved


def test_11_move_track_to_top(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    playlist = server.get_playlist(playlist_id)
    moved = playlist["tracks"][-1]["setVideoId"]
    server.move_track_to_top(playlist_id, moved)
    playlist = eventually_playlist(playlist_id, lambda value: value["tracks"][0]["setVideoId"] == moved)
    assert playlist["tracks"][0]["setVideoId"] == moved


def test_12_remove_tracks_from_playlist(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    playlist = server.get_playlist(playlist_id)
    removed = playlist["tracks"][0]["setVideoId"]
    result = server.remove_tracks_from_playlist(playlist_id, [removed])
    assert "Removed 1 track(s)" in result
    playlist = eventually_playlist(
        playlist_id, lambda value: all(track["setVideoId"] != removed for track in value["tracks"])
    )
    assert len(playlist["tracks"]) == 2


def test_13_delete_playlist(real_playlist: dict) -> None:
    playlist_id = require_playlist(real_playlist)
    result = server.delete_playlist(playlist_id)
    assert "Deleted playlist" in result
    real_playlist["id"] = None
