"""Registers the YouTube Music operations as MCP tools.

The operations in youtube_ops.py and ui.py take a YTMusic client as their first
argument. `register_tools` wraps each one in an async tool that picks the client
of the calling user and runs the (blocking) ytmusicapi call in a worker thread.
The `yt` parameter is hidden from the tool schema the model sees.
"""

import inspect
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import ui
from . import youtube_ops as ops
from .accounts import AccountRegistry

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

# (operation, annotations, MCP Apps / ChatGPT metadata or None)
TOOLS: list[tuple[Callable[..., Any], ToolAnnotations, dict[str, Any] | None]] = [
    (ops.search_music, READ_ONLY, None),
    (ops.list_my_playlists, READ_ONLY, None),
    (ops.get_playlist, READ_ONLY, None),
    (ui.open_music_manager_ui, READ_ONLY, ui.MANAGER_TOOL_META),
    (ui.get_playlist_tracks_ui, READ_ONLY, ui.PLAYLIST_TOOL_META),
    (ops.create_playlist, WRITE, None),
    (ops.delete_playlist, DESTRUCTIVE, None),
    (ops.rename_playlist, IDEMPOTENT_WRITE, None),
    (ops.add_tracks_to_playlist, WRITE, None),
    (ops.remove_tracks_from_playlist, DESTRUCTIVE, None),
    (ops.reorder_playlist_track, IDEMPOTENT_WRITE, None),
    (ops.move_track_to_top, IDEMPOTENT_WRITE, None),
    (ops.move_track_to_bottom, IDEMPOTENT_WRITE, None),
]


def _as_tool(operation: Callable[..., Any], accounts: AccountRegistry) -> Callable[..., Any]:
    """Wrap ``operation(yt, ...)`` as ``async tool(...)`` without the ``yt`` parameter."""

    async def tool(*args: Any, **kwargs: Any) -> Any:
        return await accounts.call(operation, *args, **kwargs)

    signature = inspect.signature(operation)
    client_param, *tool_params = signature.parameters.values()
    tool.__signature__ = signature.replace(parameters=tool_params)  # type: ignore[attr-defined]
    tool.__annotations__ = {k: v for k, v in operation.__annotations__.items() if k != client_param.name}
    tool.__name__ = operation.__name__
    tool.__doc__ = operation.__doc__
    return tool


def register_tools(mcp: FastMCP, accounts: AccountRegistry) -> None:
    for operation, annotations, meta in TOOLS:
        mcp.add_tool(_as_tool(operation, accounts), annotations=annotations, meta=meta)
    ui.register_resources(mcp)
    if accounts.reconnect_url is not None:
        _register_reconnect_tool(mcp, accounts)


def _register_reconnect_tool(mcp: FastMCP, accounts: AccountRegistry) -> None:
    def reconnect_youtube_music() -> dict[str, str]:
        """Get a private setup link when YouTube Music cookies are missing or expired."""
        username = accounts.current_username()
        if username is None:
            raise RuntimeError("An authenticated user is required.")
        return {
            "status": "connected" if accounts.has_cookies(username) else "reconnect_required",
            "setupUrl": accounts.reconnect_url(username),
            "instructions": (
                "Open setupUrl, copy fresh request headers from a music.youtube.com "
                "/youtubei/v1/ request, and paste them into the private form."
            ),
        }

    mcp.add_tool(reconnect_youtube_music, annotations=IDEMPOTENT_WRITE)
