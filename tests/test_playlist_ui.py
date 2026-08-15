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
        assert tool.meta["ui"]["resourceUri"] == "ui://ytmusic/playlist-manager-v4.html"
        assert tool.meta["ui/resourceUri"] == "ui://ytmusic/playlist-manager-v4.html"
        assert tool.meta["openai/outputTemplate"] == "ui://ytmusic/playlist-manager-v4-openai.html"


def test_modern_and_chatgpt_react_resources_are_registered() -> None:
    resources = {str(r.uri): r for r in server.mcp._resource_manager.list_resources()}
    modern = resources["ui://ytmusic/playlist-manager-v4.html"]
    chatgpt = resources["ui://ytmusic/playlist-manager-v4-openai.html"]
    assert modern.mime_type == "text/html;profile=mcp-app"
    assert modern.meta["ui"]["prefersBorder"] is True
    assert "https://i.ytimg.com" in modern.meta["ui"]["csp"]["resourceDomains"]
    assert chatgpt.mime_type == "text/html+skybridge"
    assert chatgpt.meta["openai/widgetPrefersBorder"] is True


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
