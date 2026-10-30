"""Tool registration, the stdio/http server builders, and the admin CLI."""

import json

import pytest
from conftest import FAKE_HEADERS

from ytmusic_mcp import app, cli
from ytmusic_mcp.app import build_http_server, build_stdio_server
from ytmusic_mcp.config import settings_from_env
from ytmusic_mcp.security import generate_secret_key
from ytmusic_mcp.store import Store


def http_settings(tmp_path):
    return settings_from_env(
        {
            "MCP_TRANSPORT": "http",
            "MCP_PUBLIC_URL": "https://mcp.example.com/ytmusic-mcp",
            "YTMUSIC_SECRET_KEY": generate_secret_key(),
            "YTMUSIC_DB_PATH": str(tmp_path / "db.sqlite"),
        }
    )


@pytest.mark.anyio
async def test_tools_hide_the_client_parameter_and_declare_hints():
    tools = {tool.name: tool for tool in await build_stdio_server(settings_from_env({})).list_tools()}

    assert len(tools) == 13
    for tool in tools.values():
        assert "yt" not in tool.inputSchema["properties"]
        assert tool.description
    assert tools["delete_playlist"].annotations.destructiveHint is True
    assert tools["remove_tracks_from_playlist"].annotations.destructiveHint is True
    assert tools["search_music"].annotations.readOnlyHint is True
    assert tools["search_music"].inputSchema["properties"]["filter"]["enum"][0] == "songs"
    assert tools["create_playlist"].inputSchema["properties"]["privacy_status"]["enum"] == [
        "PUBLIC",
        "PRIVATE",
        "UNLISTED",
    ]
    assert "reconnect_youtube_music" not in tools  # only meaningful when users connect via the browser


@pytest.mark.anyio
async def test_http_server_adds_the_reconnect_tool_and_login_routes(tmp_path):
    mcp = build_http_server(http_settings(tmp_path))
    assert "reconnect_youtube_music" in {tool.name for tool in await mcp.list_tools()}
    paths = {route.path for route in mcp.streamable_http_app().routes}
    assert {"/login", "/login/callback", "/youtube-auth", "/youtube-auth/callback"} <= paths
    assert {"/authorize", "/token", "/register", "/revoke"} <= paths


def test_force_ipv4_is_applied_unless_disabled(monkeypatch):
    import socket

    from urllib3.util import connection

    monkeypatch.setattr(connection, "allowed_gai_family", connection.allowed_gai_family)
    app.configure_runtime(settings_from_env({"MCP_FORCE_IPV4": "1"}))
    assert connection.allowed_gai_family() == socket.AF_INET


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("YTMUSIC_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("YTMUSIC_SECRET_KEY", generate_secret_key())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "a-long-enough-password")
    return tmp_path


def test_cli_user_lifecycle_keeps_cookies_encrypted(cli_env, capsys):
    headers_file = cli_env / "headers.json"
    headers_file.write_text(json.dumps(FAKE_HEADERS))

    assert cli.main(["add-user", "alice"]) == 0
    assert cli.main(["set-cookies", "alice", "--file", str(headers_file)]) == 0
    capsys.readouterr()
    assert cli.main(["list-users"]) == 0
    assert "alice\tcookies updated" in capsys.readouterr().out

    blob = Store(cli_env / "cli.db").get_user("alice").cookies
    assert b"abc123" not in blob

    assert cli.main(["delete-user", "alice"]) == 0
    assert Store(cli_env / "cli.db").get_user("alice") is None


def test_cli_reports_problems_without_a_traceback(cli_env, capsys):
    assert cli.main(["add-user", "alice"]) == 0
    assert cli.main(["add-user", "alice"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["set-cookies", "nobody"]) == 1


def test_cli_requires_a_secret_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YTMUSIC_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.delenv("YTMUSIC_SECRET_KEY", raising=False)
    assert cli.main(["list-users"]) == 1
    assert "YTMUSIC_SECRET_KEY" in capsys.readouterr().err


def test_cli_local_cookies_writes_the_stdio_file(monkeypatch, tmp_path):
    target = tmp_path / "browser.json"
    monkeypatch.setenv("YTMUSIC_AUTH_FILE", str(target))
    source = tmp_path / "in.json"
    source.write_text(json.dumps(FAKE_HEADERS))
    assert cli.main(["local-cookies", "--file", str(source)]) == 0
    assert json.loads(target.read_text())["cookie"] == FAKE_HEADERS["cookie"]
