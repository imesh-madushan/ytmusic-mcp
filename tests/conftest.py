"""Safety controls for tests that mutate a real YouTube Music account."""

import os

import pytest

# This is set while conftest loads, before test modules import server.py.
os.environ["MCP_TRANSPORT"] = "stdio"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-youtube-integration",
        action="store_true",
        help="run tests that create, edit, and delete a real YouTube Music playlist",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-youtube-integration"):
        return
    skip = pytest.mark.skip(reason="requires --run-youtube-integration and real browser.json credentials")
    for item in items:
        if "youtube_integration" in item.keywords:
            item.add_marker(skip)

