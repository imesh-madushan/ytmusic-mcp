"""The interactive playlist manager: UI tools, resources, and the compiled widget."""

from unittest.mock import MagicMock

import pytest

from ytmusic_mcp import ui
from ytmusic_mcp.app import build_stdio_server
from ytmusic_mcp.config import settings_from_env
from ytmusic_mcp.youtube_ops import thumbnail_url

MODERN_URI = "ui://ytmusic/playlist-manager-v4.html"
OPENAI_URI = "ui://ytmusic/playlist-manager-v4-openai.html"
LEGACY_URI = "ui://ytmusic/playlist-tracks"


@pytest.fixture(scope="module")
def mcp():
    return build_stdio_server(settings_from_env({}))


def sample_playlist() -> dict:
    return {
        "id": "PL123",
        "title": "Night Mix",
        "description": "Favorites",
        "privacy": "PRIVATE",
        "trackCount": 2,
        "tracks": [
            {
                "videoId": "video-one",
                "setVideoId": "set-one",
                "title": "First song",
                "artists": [{"name": "Artist One"}],
                "album": {"name": "Album One"},
                "duration": "3:12",
                "thumbnails": [{"url": "https://i.ytimg.com/vi/video-one/default.jpg", "width": 120}],
            },
            {"videoId": "video-two", "setVideoId": "set-two", "title": "Second song", "artists": []},
        ],
    }


def test_playlist_ui_tool_returns_text_and_structured_data():
    yt = MagicMock()
    yt.get_playlist.return_value = sample_playlist()
    result = ui.get_playlist_tracks_ui(yt, "PL123")
    assert result.content[0].text == "Playlist Night Mix: 2 tracks."
    assert (
        result.structuredContent["tracks"][0]["thumbnail"] == "https://i.ytimg.com/vi/video-one/default.jpg"
    )
    assert result.structuredContent["tracks"][1]["thumbnail"] is None
    yt.get_playlist.assert_called_once_with("PL123", limit=None)  # the widget needs every track


def test_manager_tool_returns_playlists_as_structured_data():
    yt = MagicMock()
    yt.get_library_playlists.return_value = [{"playlistId": "PL123", "title": "Night Mix", "count": 2}]
    result = ui.open_music_manager_ui(yt)
    assert result.structuredContent == {
        "view": "manager",
        "playlists": [{"playlistId": "PL123", "title": "Night Mix", "count": 2}],
    }
    assert result.content[0].type == "text"


def test_ui_tools_advertise_mcp_apps_and_chatgpt_metadata(mcp):
    for name in ("open_music_manager_ui", "get_playlist_tracks_ui"):
        meta = mcp._tool_manager.get_tool(name).meta
        assert meta["ui"]["resourceUri"] == MODERN_URI
        assert meta["ui/resourceUri"] == MODERN_URI
        assert meta["openai/outputTemplate"] == OPENAI_URI


def test_server_advertises_the_connector_icon(mcp):
    icons = mcp._mcp_server.icons
    assert icons[0].src.startswith("data:image/png;base64,")
    assert (icons[0].mimeType, icons[0].sizes) == ("image/png", ["96x96"])


def test_server_starts_without_the_icon_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "CONNECTOR_ICON_PATH", tmp_path / "missing.png")
    assert ui.connector_icons() == []


def test_widget_resources_are_registered_for_every_host(mcp):
    resources = {str(r.uri): r for r in mcp._resource_manager.list_resources()}
    modern, chatgpt, legacy = resources[MODERN_URI], resources[OPENAI_URI], resources[LEGACY_URI]
    assert modern.mime_type == "text/html;profile=mcp-app"
    assert modern.meta["ui"]["prefersBorder"] is True
    assert "https://i.ytimg.com" in modern.meta["ui"]["csp"]["resourceDomains"]
    assert chatgpt.mime_type == legacy.mime_type == "text/html+skybridge"
    assert chatgpt.meta["openai/widgetPrefersBorder"] is True
    assert "https://yt3.googleusercontent.com" in legacy.meta["openai/widgetCSP"]["resource_domains"]


def test_compiled_widget_has_host_bridges_and_virtual_rows():
    html = ui.playlist_manager_html()
    for marker in ("ui/initialize", "ui/notifications/tool-result", "openai:set_globals", "callServerTool"):
        assert marker in html
    assert "getVirtualItems" in html
    assert "__YTMUSIC_INITIAL_DATA__" not in html


def test_missing_widget_build_explains_how_to_build_it(monkeypatch, tmp_path):
    monkeypatch.setattr(ui, "WIDGET_PATH", tmp_path / "missing.html")
    ui._read_widget.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="npm run build"):
            ui.playlist_manager_html()
    finally:
        ui._read_widget.cache_clear()


def test_thumbnail_picker_chooses_the_smallest_sufficient_image():
    item = {
        "thumbnails": [
            {"url": "small", "width": 60},
            {"url": "ideal", "width": 120},
            {"url": "huge", "width": 1000},
        ]
    }
    assert thumbnail_url(item) == "ideal"
    assert (
        thumbnail_url({"thumbnails": [{"url": "tiny", "width": 40}, {"url": "small", "width": 60}]})
        == "small"
    )
    assert thumbnail_url({"thumbnails": [{"url": "no-size"}]}) == "no-size"
    assert thumbnail_url({}) is None


def test_thumbnail_picker_supports_song_video_details():
    song = {
        "videoDetails": {"thumbnail": {"thumbnails": [{"url": "a", "width": 60}, {"url": "b", "width": 120}]}}
    }
    assert thumbnail_url(song) == "b"
