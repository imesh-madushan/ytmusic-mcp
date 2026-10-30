"""End-to-end tests against a real YouTube Music account (stdio-mode cookie file).

The numbered tests form one workflow to keep the number of account mutations
small. A private, uniquely named playlist is created and is always deleted by
the module fixture. Run with:  pytest --run-youtube-integration
"""

import time
import uuid

import pytest

from ytmusic_mcp import youtube_ops as ops
from ytmusic_mcp.accounts import AccountRegistry
from ytmusic_mcp.config import settings_from_env

pytestmark = pytest.mark.youtube_integration


@pytest.fixture(scope="module")
def yt():
    return AccountRegistry(settings_from_env({}).local_auth_file).client_for(None)


@pytest.fixture(scope="module")
def state(yt) -> dict:
    data: dict = {"id": None, "name": f"ytmusic-mcp-integration-{uuid.uuid4().hex[:10]}", "video_ids": []}
    yield data
    if data["id"]:
        try:
            ops.delete_playlist(yt, data["id"])
        except Exception as exc:  # noqa: BLE001 - cleanup must not hide the test result
            print(f"Cleanup failed for integration playlist {data['id']}: {exc}")


def eventually(yt, playlist_id: str, predicate, timeout: float = 15.0) -> dict:
    """Poll briefly because YouTube playlist writes are eventually consistent."""
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        try:
            latest = ops.get_playlist(yt, playlist_id)
        except KeyError:
            time.sleep(1)
            continue
        if predicate(latest):
            return latest
        time.sleep(1)
    pytest.fail(f"YouTube Music did not reach the expected playlist state; latest={latest!r}")


def playlist_id_of(state: dict) -> str:
    if not state["id"]:
        pytest.skip("requires the playlist created by test_04")
    return state["id"]


def test_01_search_finds_playable_tracks(yt, state):
    for query in ("Never Gonna Give You Up Rick Astley", "Take On Me a-ha", "Africa Toto"):
        results = ops.search_music(yt, query, limit=5)
        match = next((r["videoId"] for r in results if r.get("videoId") not in state["video_ids"]), None)
        assert match, f"No distinct playable result for {query!r}"
        state["video_ids"].append(match)


def test_02_list_my_playlists(yt):
    playlists = ops.list_my_playlists(yt, limit=5)
    assert all("playlistId" in p and "title" in p for p in playlists)


def test_03_create_private_playlist(yt, state):
    created = ops.create_playlist(yt, state["name"], "Temporary MCP integration test", "PRIVATE")
    state["id"] = created["playlistId"]
    playlist = eventually(yt, state["id"], lambda p: p.get("title") == state["name"])
    assert playlist["privacy"] in ("PRIVATE", None)


def test_04_rename_playlist(yt, state):
    playlist_id = playlist_id_of(state)
    renamed = state["name"] + "-renamed"
    ops.rename_playlist(yt, playlist_id, title=renamed, description="Renamed by integration test")
    eventually(yt, playlist_id, lambda p: p.get("title") == renamed)
    state["name"] = renamed


def test_05_add_tracks(yt, state):
    playlist_id = playlist_id_of(state)
    assert ops.add_tracks_to_playlist(yt, playlist_id, state["video_ids"][:2])["requested"] == 2
    playlist = eventually(yt, playlist_id, lambda p: len(p["tracks"]) == 2)
    assert [t["videoId"] for t in playlist["tracks"]] == state["video_ids"][:2]


def test_06_add_track_to_top(yt, state):
    playlist_id = playlist_id_of(state)
    ops.add_tracks_to_playlist(yt, playlist_id, [state["video_ids"][2]], add_to_top=True)
    playlist = eventually(yt, playlist_id, lambda p: len(p["tracks"]) == 3)
    assert playlist["tracks"][0]["videoId"] == state["video_ids"][2]


def test_07_reorder_track(yt, state):
    playlist_id = playlist_id_of(state)
    tracks = ops.get_playlist(yt, playlist_id)["tracks"]
    moved = tracks[-1]["setVideoId"]
    ops.reorder_playlist_track(yt, playlist_id, moved, tracks[0]["setVideoId"])
    eventually(yt, playlist_id, lambda p: p["tracks"][0]["setVideoId"] == moved)


def test_08_move_to_bottom_and_top(yt, state):
    playlist_id = playlist_id_of(state)
    moved = ops.get_playlist(yt, playlist_id)["tracks"][0]["setVideoId"]
    ops.move_track_to_bottom(yt, playlist_id, moved)
    eventually(yt, playlist_id, lambda p: p["tracks"][-1]["setVideoId"] == moved)
    ops.move_track_to_top(yt, playlist_id, moved)
    eventually(yt, playlist_id, lambda p: p["tracks"][0]["setVideoId"] == moved)


def test_09_remove_track(yt, state):
    playlist_id = playlist_id_of(state)
    removed = ops.get_playlist(yt, playlist_id)["tracks"][0]["setVideoId"]
    assert ops.remove_tracks_from_playlist(yt, playlist_id, [removed])["removed"] == 1
    playlist = eventually(yt, playlist_id, lambda p: all(t["setVideoId"] != removed for t in p["tracks"]))
    assert len(playlist["tracks"]) == 2


def test_10_delete_playlist(yt, state):
    playlist_id = playlist_id_of(state)
    assert ops.delete_playlist(yt, playlist_id) == {"deleted": playlist_id}
    state["id"] = None
