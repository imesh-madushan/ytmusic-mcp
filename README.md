# YouTube Music MCP Server

An MCP server that wraps [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) so an
LLM client (Claude Desktop, Claude Code, etc.) can search YouTube Music and fully
manage your playlists: create/delete, add/remove tracks, and reorder tracks
(move to top, move to bottom, move before another track).

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

## 2. Run the browser-cookie auth setup

```bash
python3 setup_auth.py
```

It'll print step-by-step instructions: open music.youtube.com, open DevTools
→ Network tab, click a request, copy the request headers, paste them in.
This writes `browser.json` next to the script — that's what `server.py` reads.

Logs are written to stderr so they do not interfere with the MCP stdio protocol.
Set `YTMUSIC_LOG_LEVEL` to `DEBUG`, `INFO`, `WARNING`, or `ERROR` (default: `INFO`).

This project intentionally supports browser-cookie authentication only. It
does not accept OAuth client files or Google Cloud credentials. Copied headers
can go stale if you sign out or the browser session expires; re-run
`setup_auth.py` if `server.py` starts returning 401 errors.

## 3. Try it standalone (optional sanity check)

```bash
python3 -c "
from server import get_client
yt = get_client()
print(yt.get_library_playlists(limit=5))
"
```

## 4. Connect it to Claude Desktop / Claude Code

Add to your MCP config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "youtube-music": {
      "command": "python3",
      "args": ["/absolute/path/to/ytmusic-mcp/server.py"]
    }
  }
}
```

Restart the client. You should see the tools below available.

## Run tests

```bash
python -m unittest discover -s tests -v
```

These tests mock YouTube Music and do not change your account.

## Test real search and playlist adding

Run a read-only search against your account:

```powershell
.\.venv\Scripts\python.exe smoke_test.py "Daft Punk"
```

To offer to add the first result, copy the playlist ID from its YouTube Music
URL and run:

```powershell
.\.venv\Scripts\python.exe smoke_test.py "Daft Punk Get Lucky" --add-first-to PLAYLIST_ID
```

The command displays the selected track and asks for confirmation before it
changes the playlist. Add `--yes` only in automation where that mutation is
already intended.

## Test through an MCP client

Launch the official MCP Inspector from PowerShell:

```powershell
.\.venv\Scripts\mcp.exe dev server.py:mcp
```

Open the Inspector URL printed in the terminal, connect, choose **Tools**, and
first call `search_music` with arguments such as:

```json
{"query": "Daft Punk", "filter": "songs", "limit": 5}
```

Copy a returned `videoId`, then call `add_tracks_to_playlist`:

```json
{"playlist_id": "YOUR_PLAYLIST_ID", "video_ids": ["RETURNED_VIDEO_ID"]}
```

This is the closest local test to an AI client: the Inspector discovers the
tool schemas and invokes them over MCP rather than calling Python directly.

## Tools exposed

| Tool | What it does |
|---|---|
| `search_music` | Search songs/videos/albums/artists/playlists |
| `list_my_playlists` | List your playlists |
| `get_playlist` | Get a playlist's tracks (includes `setVideoId`, needed for remove/reorder) |
| `create_playlist` | Create a new playlist |
| `delete_playlist` | Delete a playlist |
| `rename_playlist` | Edit title/description/privacy |
| `add_tracks_to_playlist` | Add tracks by videoId, optionally to the top |
| `remove_tracks_from_playlist` | Remove tracks by setVideoId |
| `reorder_playlist_track` | Move a track before another track, or to the end |
| `move_track_to_top` | Move a track to position 1 |
| `move_track_to_bottom` | Move a track to the last position |

## Notes on IDs

- **`videoId`**: identifies a song/video itself (from `search_music`). Used to *add* tracks.
- **`setVideoId`**: identifies one specific occurrence of a track *inside a playlist*
  (from `get_playlist`). Used to *remove* or *reorder* tracks, since the same song
  could appear twice in a playlist with two different `setVideoId`s.

## Extending it

`ytmusicapi` covers a lot more than playlists — liking songs, library
management, uploads, podcasts, radio/queue generation, artist/album browsing.
Any of its methods can be wrapped the same way: add a new `@mcp.tool()`
function in `server.py` that calls the corresponding `yt.<method>(...)`.
