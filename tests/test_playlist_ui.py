"""Tests for the MCP-UI playlist track browser."""

from __future__ import annotations

import server
from playlist_ui import create_playlist_tracks_ui


def sample_playlist() -> dict:
    return {
        "playlistId": "PL123",
        "title": "Night <Mix>",
        "description": "Favorites & more",
        "privacy": "PRIVATE",
        "trackCount": 2,
        "tracks": [
            {
                "videoId": "video-one",
                "setVideoId": "set-one",
                "title": "First </script> song",
                "artists": "Artist One",
                "album": "Album One",
                "duration": "3:12",
                "thumbnail": "https://i.ytimg.com/vi/video-one/default.jpg",
            },
            {
                "videoId": "video-two",
                "setVideoId": "set-two",
                "title": "Second song",
                "artists": "Artist Two",
                "album": None,
                "duration": "4:01",
                "thumbnail": None,
            },
        ],
    }


def test_widget_contains_playlist_data_and_escapes_script_tags() -> None:
    resource = create_playlist_tracks_ui(sample_playlist())

    assert str(resource.resource.uri) == "ui://ytmusic/playlist-tracks"
    assert resource.resource.mimeType == "text/html"
    assert "First </script> song" not in resource.resource.text
    assert "First \\u003c/script\\u003e song" in resource.resource.text
    assert "Filter playlist tracks" in resource.resource.text
    assert resource.resource.meta["mcpui.dev/ui-preferred-frame-size"] == ["100%", "680px"]


def test_ui_tool_returns_fallback_and_embedded_resource(monkeypatch) -> None:
    playlist = sample_playlist()
    monkeypatch.setattr(server, "get_playlist", lambda playlist_id, limit=200: playlist)

    result = server.get_playlist_tracks_ui("PL123")

    assert result.structuredContent == playlist
    assert result.content[0].type == "text"
    assert result.content[0].text == "Playlist Night <Mix>: 2 tracks."
    assert result.content[1].type == "resource"
    assert result.content[1].resource.mimeType == "text/html"



def test_tool_advertises_modern_legacy_and_chatgpt_ui_metadata() -> None:
    tool = server.mcp._tool_manager.get_tool("get_playlist_tracks_ui")

    assert tool is not None
    assert tool.meta["ui"]["resourceUri"] == "ui://ytmusic/playlist-tracks"
    assert tool.meta["ui/resourceUri"] == "ui://ytmusic/playlist-tracks"
    assert tool.meta["openai/outputTemplate"] == "ui://ytmusic/playlist-tracks-openai"


def test_modern_and_chatgpt_resources_are_registered() -> None:
    resources = {
        str(resource.uri): resource for resource in server.mcp._resource_manager.list_resources()
    }
    modern = resources["ui://ytmusic/playlist-tracks"]
    chatgpt = resources["ui://ytmusic/playlist-tracks-openai"]

    assert modern.mime_type == "text/html;profile=mcp-app"
    assert modern.meta["ui"]["prefersBorder"] is True
    assert chatgpt.mime_type == "text/html+skybridge"
    assert chatgpt.meta["openai/widgetPrefersBorder"] is True


def test_static_app_template_accepts_all_result_bridges() -> None:
    html = server.playlist_tracks_app_resource()

    assert "ui/initialize" in html
    assert "ui/notifications/tool-result" in html
    assert "ui/notifications/initialized" in html
    assert "window.openai.toolOutput" in html
    assert "openai:set_globals" in html
    assert "const initialPlaylist = null" in html


def test_widget_does_not_load_external_images() -> None:
    html = create_playlist_tracks_ui(sample_playlist()).resource.text

    assert "document.createElement('img')" not in html
    assert "track.thumbnail" not in html
