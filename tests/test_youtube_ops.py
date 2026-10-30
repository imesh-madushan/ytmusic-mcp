"""The playlist operations, against a mocked YTMusic client."""

from unittest.mock import MagicMock

import pytest

from ytmusic_mcp import youtube_ops as ops


@pytest.fixture
def yt():
    return MagicMock()


def test_search_clamps_the_limit_and_summarizes(yt):
    yt.search.return_value = [
        {
            "resultType": "song",
            "videoId": "v1",
            "title": "T",
            "artists": [{"name": "A"}, {"name": "B"}],
            "duration": "3:00",
        }
    ]
    results = ops.search_music(yt, "query", limit=10_000)
    yt.search.assert_called_once_with("query", filter="songs", limit=ops.MAX_RESULTS)
    assert results[0]["artists"] == "A, B"
    assert results[0]["videoId"] == "v1"


def test_get_playlist_summarizes_tracks(yt):
    yt.get_playlist.return_value = {
        "id": "PL1",
        "title": "Mix",
        "tracks": [
            {
                "videoId": "v",
                "setVideoId": "s",
                "title": "T",
                "artists": [{"name": "A"}],
                "album": {"name": "Al"},
            }
        ],
    }
    playlist = ops.get_playlist(yt, "PL1")
    assert playlist["tracks"][0] == {
        "videoId": "v",
        "setVideoId": "s",
        "title": "T",
        "artists": "A",
        "album": "Al",
        "duration": None,
    }


def test_get_playlist_recovers_a_new_empty_playlist_from_the_library(yt):
    yt.get_playlist.side_effect = KeyError("Unable to find 'contents' using path ...")
    yt.get_library_playlists.return_value = [{"playlistId": "PL1", "title": "Fresh"}]
    playlist = ops.get_playlist(yt, "PL1")
    assert (playlist["title"], playlist["tracks"], playlist["trackCount"]) == ("Fresh", [], 0)


def test_get_playlist_does_not_hide_other_key_errors(yt):
    yt.get_playlist.side_effect = KeyError("something else")
    with pytest.raises(KeyError):
        ops.get_playlist(yt, "PL1")


def test_add_to_top_is_switched_off_again_even_on_failure(yt):
    yt.add_playlist_items.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        ops.add_tracks_to_playlist(yt, "PL1", ["v1"], add_to_top=True)
    assert [c.kwargs for c in yt.edit_playlist.call_args_list] == [{"addToTop": True}, {"addToTop": False}]


def test_add_tracks_reports_status(yt):
    yt.add_playlist_items.return_value = {"status": "STATUS_SUCCEEDED"}
    assert ops.add_tracks_to_playlist(yt, "PL1", ["a", "b"]) == {"requested": 2, "status": "STATUS_SUCCEEDED"}


def test_batches_are_limited(yt):
    with pytest.raises(ValueError):
        ops.add_tracks_to_playlist(yt, "PL1", [])
    with pytest.raises(ValueError):
        ops.add_tracks_to_playlist(yt, "PL1", ["v"] * (ops.MAX_TRACKS_PER_CALL + 1))


def test_remove_maps_set_ids_and_reports_unknown_ones(yt):
    yt.get_playlist.return_value = {
        "tracks": [{"videoId": "v1", "setVideoId": "s1"}, {"videoId": "v2", "setVideoId": "s2"}]
    }
    result = ops.remove_tracks_from_playlist(yt, "PL1", ["s2", "ghost"])
    yt.remove_playlist_items.assert_called_once_with("PL1", [{"videoId": "v2", "setVideoId": "s2"}])
    assert result == {"removed": 1, "notFound": ["ghost"]}


def test_remove_skips_the_api_call_when_nothing_matches(yt):
    yt.get_playlist.return_value = {"tracks": []}
    assert ops.remove_tracks_from_playlist(yt, "PL1", ["ghost"])["removed"] == 0
    yt.remove_playlist_items.assert_not_called()


def test_move_retries_on_http_409(yt, monkeypatch):
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    yt.edit_playlist.side_effect = [Exception("HTTP 409"), None]
    ops.reorder_playlist_track(yt, "PL1", "s1", "s2")
    assert yt.edit_playlist.call_count == 2
    yt.edit_playlist.assert_called_with("PL1", moveItem=("s1", "s2"))


def test_move_gives_up_after_three_attempts_and_ignores_other_errors(yt, monkeypatch):
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    yt.edit_playlist.side_effect = Exception("HTTP 409")
    with pytest.raises(Exception, match="409"):
        ops.move_track_to_bottom(yt, "PL1", "s1")
    assert yt.edit_playlist.call_count == ops.MOVE_ATTEMPTS

    yt.edit_playlist.reset_mock(side_effect=True)
    yt.edit_playlist.side_effect = Exception("HTTP 500")
    with pytest.raises(Exception, match="500"):
        ops.move_track_to_bottom(yt, "PL1", "s1")
    assert yt.edit_playlist.call_count == 1


def test_move_to_top_inserts_before_the_first_track(yt):
    yt.get_playlist.return_value = {"tracks": [{"setVideoId": "first"}]}
    ops.move_track_to_top(yt, "PL1", "other")
    yt.edit_playlist.assert_called_once_with("PL1", moveItem=("other", "first"))


def test_move_to_top_is_a_no_op_when_already_first(yt):
    yt.get_playlist.return_value = {"tracks": [{"setVideoId": "first"}]}
    assert ops.move_track_to_top(yt, "PL1", "first")["moved"] is None
    yt.edit_playlist.assert_not_called()
