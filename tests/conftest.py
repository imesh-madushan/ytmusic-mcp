"""Shared fixtures, plus the safety switch for tests that touch a real YouTube account."""

import pytest

from ytmusic_mcp.security import CookieVault, generate_secret_key
from ytmusic_mcp.store import Store

# A syntactically valid set of browser-cookie headers (not a real account).
FAKE_HEADERS = {
    "cookie": "__Secure-3PAPISID=abc123; SID=xyz",
    "x-goog-authuser": "0",
    "origin": "https://music.youtube.com",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-youtube-integration",
        action="store_true",
        help="run tests that create, edit, and delete a real YouTube Music playlist",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-youtube-integration"):
        return
    skip = pytest.mark.skip(reason="requires --run-youtube-integration and real cookie credentials")
    for item in items:
        if "youtube_integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


@pytest.fixture
def vault() -> CookieVault:
    return CookieVault(generate_secret_key())
