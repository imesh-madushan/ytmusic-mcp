"""Tests for the React MCP playlist manager integration."""

from __future__ import annotations

import server


def sample_playlist() -> dict:
    return {
        "playlistId": "PL123", "title": "Night Mix", "description": "Favorites",
        "privacy": "PRIVATE", "trackCount": 2,
        "tracks": [
            {"videoId": "video-one", "setVideoId": "set-one", "title": "First song",
             "artists": "Artist One", "album": "Album One", "duration": "3:12",
             "thumbnail": "https://i.ytimg.com/vi/video-one/default.jpg"},
            {"videoId": "video-two", "setVideoId": "set-two", "title": "Second song",
             "artists": "Artist Two", "album": None, "duration": "4:01", "thumbnail": None},
        ],
    }


def test_ui_tool_returns_text_and_structured_data(monkeypatch) -> None:
    playlist = sample_playlist()
    monkeypatch.setattr(server, "get_playlist", lambda playlist_id, limit=None: playlist)
    result = server.get_playlist_tracks_ui("PL123")
    assert result.structuredContent == playlist
    assert len(result.content) == 1
    assert result.content[0].text == "Playlist Night Mix: 2 tracks."


def test_manager_tool_returns_playlists_as_structured_data(monkeypatch) -> None:
    playlists = [{"playlistId": "PL123", "title": "Night Mix", "count": 2}]
    monkeypatch.setattr(server, "list_my_playlists", lambda limit=100: playlists)
    result = server.open_music_manager_ui()
    assert result.structuredContent == {"view": "manager", "playlists": playlists}
    assert len(result.content) == 1
    assert result.content[0].type == "text"


def test_tools_advertise_mcp_apps_and_chatgpt_metadata() -> None:
    for name in ("open_music_manager_ui", "get_playlist_tracks_ui"):
        tool = server.mcp._tool_manager.get_tool(name)
        assert tool.meta["ui"]["resourceUri"] == "ui://ytmusic/playlist-manager-v5.html"
        assert tool.meta["ui/resourceUri"] == "ui://ytmusic/playlist-manager-v5.html"
        assert tool.meta["openai/outputTemplate"] == "ui://ytmusic/playlist-manager-v5-openai.html"


def test_server_advertises_connector_icon() -> None:
    icons = server.mcp._mcp_server.icons
    assert icons and icons[0].src.startswith("data:image/png;base64,")
    assert icons[0].mimeType == "image/png"
    assert icons[0].sizes == ["96x96"]


def test_modern_and_chatgpt_react_resources_are_registered() -> None:
    resources = {str(r.uri): r for r in server.mcp._resource_manager.list_resources()}
    modern = resources["ui://ytmusic/playlist-manager-v5.html"]
    chatgpt = resources["ui://ytmusic/playlist-manager-v5-openai.html"]
    assert modern.mime_type == "text/html;profile=mcp-app"
    assert modern.meta["ui"]["prefersBorder"] is True
    assert "https://i.ytimg.com" in modern.meta["ui"]["csp"]["resourceDomains"]
    assert chatgpt.mime_type == "text/html+skybridge"
    assert chatgpt.meta["openai/widgetPrefersBorder"] is True
    legacy = resources["ui://ytmusic/playlist-tracks"]
    assert legacy.mime_type == "text/html+skybridge"
    assert "https://i.ytimg.com" in legacy.meta["openai/widgetCSP"]["resource_domains"]
    assert "https://yt3.googleusercontent.com" in legacy.meta["openai/widgetCSP"]["resource_domains"]


def test_compiled_react_app_has_host_bridges_and_no_python_data_marker() -> None:
    html = server.playlist_manager_html()
    assert "ui/initialize" in html
    assert "ui/notifications/tool-result" in html
    assert "openai:set_globals" in html
    assert "toolOutput" in html
    assert "callServerTool" in html
    assert "__YTMUSIC_INITIAL_DATA__" not in html


def test_widget_uses_virtual_rows_and_lazy_artwork() -> None:
    html = server.playlist_manager_html()
    assert 'loading=\"lazy\"' in html or 'loading:"lazy"' in html
    assert "getVirtualItems" in html


def test_thumbnail_picker_chooses_smallest_sufficient_image() -> None:
    item = {"thumbnails": [
        {"url": "small", "width": 60}, {"url": "ideal", "width": 120},
        {"url": "huge", "width": 1000},
    ]}
    assert server._thumbnail_url(item) == "ideal"


def test_thumbnail_picker_supports_get_song_video_details() -> None:
    song = {
        "videoDetails": {
            "thumbnail": {
                "thumbnails": [
                    {"url": "small", "width": 60},
                    {"url": "suitable", "width": 120},
                    {"url": "large", "width": 544},
                ]
            }
        }
    }
    assert server._thumbnail_url(song) == "suitable"


def test_media_summaries_include_youtube_music_links() -> None:
    track = server._track_summary({
        "videoId": "abc123", "title": "Song", "artists": [{"name": "Artist"}],
    })
    assert track["kind"] == "track"
    assert track["youtubeMusicUrl"] == "https://music.youtube.com/watch?v=abc123"
    assert track["playable"] is True


def test_play_music_keeps_requests_first_and_deduplicates_radio(monkeypatch) -> None:
    def fake_watch(video_id: str, limit: int, radio: bool = False) -> list[dict]:
        if not radio:
            return [{"kind": "track", "videoId": video_id, "title": video_id, "playable": True}]
        return [
            {"kind": "track", "videoId": "two", "title": "duplicate"},
            {"kind": "track", "videoId": "recommended", "title": "Recommended"},
        ]
    monkeypatch.setattr(server, "_watch_tracks", fake_watch)
    result = server.play_music(["one", "two"], recommendation_limit=5)
    assert result.structuredContent["requestedCount"] == 2
    assert [track["videoId"] for track in result.structuredContent["queue"]] == [
        "one", "two", "recommended",
    ]


def test_play_playlist_starts_at_requested_index_then_recommends(monkeypatch) -> None:
    playlist = sample_playlist()
    monkeypatch.setattr(server, "get_playlist", lambda playlist_id, limit=None: playlist)
    monkeypatch.setattr(server, "_watch_tracks", lambda *args, **kwargs: [
        {"kind": "track", "videoId": "video-two", "title": "duplicate"},
        {"kind": "track", "videoId": "radio-one", "title": "Radio"},
    ])
    result = server.play_playlist("PL123", start_index=1)
    assert result.structuredContent["requestedCount"] == 1
    assert [track["videoId"] for track in result.structuredContent["queue"]] == [
        "video-two", "radio-one",
    ]


def test_card_and_player_tools_advertise_widget() -> None:
    for name in ("show_music_ui", "play_music", "play_playlist"):
        tool = server.mcp._tool_manager.get_tool(name)
        assert tool.meta["ui"]["resourceUri"] == "ui://ytmusic/playlist-manager-v5.html"


def test_widget_csp_allows_youtube_player() -> None:
    resources = {str(r.uri): r for r in server.mcp._resource_manager.list_resources()}
    modern = resources["ui://ytmusic/playlist-manager-v5.html"]
    assert "https://www.youtube.com" in modern.meta["ui"]["csp"]["frameDomains"]


def test_v4_widget_resources_remain_available_for_cached_clients() -> None:
    resources = {str(r.uri): r for r in server.mcp._resource_manager.list_resources()}
    assert resources["ui://ytmusic/playlist-manager-v4.html"].mime_type == "text/html;profile=mcp-app"
    assert resources["ui://ytmusic/playlist-manager-v4-openai.html"].mime_type == "text/html+skybridge"


def test_widget_resources_declare_submission_domain() -> None:
    resources = {str(r.uri): r for r in server.mcp._resource_manager.list_resources()}
    modern = resources["ui://ytmusic/playlist-manager-v5.html"]
    chatgpt = resources["ui://ytmusic/playlist-manager-v5-openai.html"]
    assert modern.meta["ui"]["domain"] == "https://o2.devim.tech"
    assert chatgpt.meta["openai/widgetDomain"] == "https://o2.devim.tech"


def test_widget_has_bounded_height_and_non_looping_player_errors() -> None:
    html = server.playlist_manager_html()
    assert "height:720px" in html
    assert "YouTube could not play this embed" in html
    assert "onError" in html
