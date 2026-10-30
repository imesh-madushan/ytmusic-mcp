"""Registers the youtube_ops functions as MCP tools.

The operations in youtube_ops.py take a YTMusic client as their first argument.
`register_tools` wraps each one in an async tool that picks the client of the
calling user and runs the (blocking) ytmusicapi call in a worker thread. The
`yt` parameter is hidden from the tool schema the model sees.
"""

import inspect
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import youtube_ops as ops
from .accounts import AccountRegistry

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

TOOLS: list[tuple[Callable[..., Any], ToolAnnotations]] = [
    (ops.search_music, READ_ONLY),
    (ops.list_my_playlists, READ_ONLY),
    (ops.get_playlist, READ_ONLY),
    (ops.create_playlist, WRITE),
    (ops.delete_playlist, DESTRUCTIVE),
    (ops.rename_playlist, IDEMPOTENT_WRITE),
    (ops.add_tracks_to_playlist, WRITE),
    (ops.remove_tracks_from_playlist, DESTRUCTIVE),
    (ops.reorder_playlist_track, IDEMPOTENT_WRITE),
    (ops.move_track_to_top, IDEMPOTENT_WRITE),
    (ops.move_track_to_bottom, IDEMPOTENT_WRITE),
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


def register_tools(mcp: MCPServer, accounts: AccountRegistry) -> None:
    for operation, annotations in TOOLS:
        mcp.add_tool(_as_tool(operation, accounts), annotations=annotations)
