"""Tool registration, the stdio/http server builders, and the admin CLI."""

import json

import pytest
from conftest import FAKE_HEADERS

from ytmusic_mcp import cli
from ytmusic_mcp.app import build_http_server, build_stdio_server
from ytmusic_mcp.config import settings_from_env
from ytmusic_mcp.security import generate_secret_key
from ytmusic_mcp.store import Store


@pytest.mark.anyio
async def test_tools_hide_the_client_parameter_and_declare_hints():
    tools = {tool.name: tool for tool in await build_stdio_server(settings_from_env({})).list_tools()}

    assert len(tools) == 11
    for tool in tools.values():
        assert "yt" not in tool.input_schema["properties"]
        assert tool.description
    assert tools["delete_playlist"].annotations.destructive_hint is True
    assert tools["search_music"].annotations.read_only_hint is True
    assert tools["search_music"].input_schema["properties"]["filter"]["enum"][0] == "songs"
    assert tools["create_playlist"].input_schema["properties"]["privacy_status"]["enum"] == [
        "PUBLIC",
        "PRIVATE",
        "UNLISTED",
    ]


@pytest.mark.anyio
async def test_http_server_builds_with_oauth_routes(tmp_path):
    settings = settings_from_env(
        {
            "MCP_TRANSPORT": "http",
            "MCP_PUBLIC_URL": "https://mcp.example.com/ytmusic-mcp",
            "YTMUSIC_SECRET_KEY": generate_secret_key(),
            "YTMUSIC_DB_PATH": str(tmp_path / "db.sqlite"),
        }
    )
    app = build_http_server(settings).streamable_http_app(host="127.0.0.1")
    paths = {route.path for route in app.routes}
    assert {"/login", "/login/callback", "/authorize", "/token", "/register"} <= paths


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
