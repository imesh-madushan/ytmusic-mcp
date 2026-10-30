# YouTube Music MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client, including ChatGPT connectors) search YouTube Music and manage playlists: create, rename and delete playlists, add and remove tracks, and reorder them. UI-capable clients can also open an interactive playlist manager.

It runs in two modes:

| Mode | For | Auth |
|---|---|---|
| **stdio** | Claude Desktop on your own machine | your YouTube cookies in a local file |
| **http** | a server shared by one or more people | OAuth 2.1 login per user; each user's YouTube cookies are stored encrypted in SQLite |

> **Disclaimer.** This project is not affiliated with or endorsed by YouTube or Google. It uses the unofficial [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) library and your browser session cookies, which may go against YouTube's Terms of Service. Use it at your own risk, and prefer a secondary Google account: anyone who gets a user's stored cookies can act as that account.

## Tools

| Tool | What it does |
|---|---|
| `search_music` | Search songs, videos, albums, artists or playlists |
| `list_my_playlists` | List your playlists |
| `get_playlist` | Get a playlist's tracks (includes `setVideoId`, needed to remove or reorder) |
| `open_music_manager_ui` | Open the interactive playlist manager |
| `get_playlist_tracks_ui` | Open one playlist in the interactive manager |
| `create_playlist` | Create a playlist (`PRIVATE` by default) |
| `rename_playlist` | Edit title, description or privacy |
| `delete_playlist` | Delete a playlist *(marked destructive)* |
| `add_tracks_to_playlist` | Add tracks by `videoId`, optionally to the top |
| `remove_tracks_from_playlist` | Remove tracks by `setVideoId` *(marked destructive)* |
| `reorder_playlist_track` | Move a track before another track, or to the end |
| `move_track_to_top` / `move_track_to_bottom` | Move a track to either end |
| `reconnect_youtube_music` | *(http mode)* Get a private link to paste fresh YouTube headers |

`videoId` identifies a song (used to add it). `setVideoId` identifies one occurrence of a song inside a playlist (used to remove or reorder), because the same song can appear twice. You never look these up yourself: the assistant calls `get_playlist` first.

### Playlist manager UI

`open_music_manager_ui` and `get_playlist_tracks_ui` render a React app (MCP Apps, with ChatGPT Apps compatibility) for listing, creating, editing and deleting playlists, searching and adding music, filtering, bulk removal and reordering. Hosts without UI support still get text and structured data.

The built widget is committed (`widgets/playlist-manager/dist/index.html`), so running the server needs no Node. To change it, edit `widgets/playlist-manager`, run `npm run build` there, and commit the new `dist/index.html`. After changing tool or UI metadata, restart the service and reconnect the connector so the host refreshes its tool list.

## Getting your YouTube cookies

Both modes need the request headers of a logged-in YouTube Music session:

1. Open [music.youtube.com](https://music.youtube.com) and sign in.
2. Open DevTools (F12) → **Network**, then play a song or reload the page.
3. Select a `music.youtube.com/youtubei/v1/...` request and copy its request headers.

Raw headers, a JSON header list, Firefox/Chrome request JSON, HAR and copied cURL are all accepted. Cookies go stale eventually; when tools start failing with an "expired" error, repeat this.

## Option A: local use with Claude Desktop (stdio)

Requires Python 3.10+.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
python -m ytmusic_mcp.cli local-cookies      # paste the headers; writes browser.json (permissions 600)
```

Add to your Claude Desktop config (`claude_desktop_config.json`), using absolute paths:

```json
{
  "mcpServers": {
    "youtube-music": {
      "command": "/path/to/yt-mcp/.venv/bin/python",
      "args": ["-m", "ytmusic_mcp"]
    }
  }
}
```

## Option B: host it for yourself and others (http)

Each user gets their own username, password and YouTube account. Tool calls always run as the user who logged in.

### 1. Install on a Linux server

```bash
git clone <this repo> && cd yt-mcp
./deploy/install.sh --domain mcp.example.com
```

The script works from any directory and for any user. It creates a virtualenv, creates `.env` with a freshly generated `YTMUSIC_SECRET_KEY`, and renders the systemd and nginx files for your machine into `deploy/out/`. It prints the exact `sudo` commands for the remaining steps and never edits `/etc` itself.

**Back up `YTMUSIC_SECRET_KEY`.** It encrypts every stored cookie; without it users must reconnect YouTube.

### 2. Add users

```bash
.venv/bin/python -m ytmusic_mcp.cli add-user alice          # asks for a password (12+ chars)
```

That is all that is required. The first time alice connects a client, the sign-in page asks her to paste her YouTube headers, validates them, and stores them encrypted. If you would rather do it for her:

```bash
.venv/bin/python -m ytmusic_mcp.cli set-cookies alice       # paste headers, or --file headers.txt
.venv/bin/python -m ytmusic_mcp.cli check-cookies alice     # confirm YouTube accepts them
```

Other commands: `list-users`, `set-password <name>`, `delete-user <name>` (also revokes their tokens). Changed cookies are picked up immediately; no restart needed.

When YouTube expires a session, the failing tool call returns a one-time link (valid 10 minutes) where only that user can paste fresh headers. The `reconnect_youtube_music` tool returns the same link on request.

### 3. Start it

Follow the printed commands to enable the systemd service, get a certificate with certbot (before installing the nginx config, which refers to it), and put nginx in front. You need a domain pointing at the server and ports 80 and 443 open. The app listens on `127.0.0.1` only; nginx is the only public surface, rate-limits the OAuth, login and header-paste routes, and returns 404 for everything else.

Optional: install the fail2ban filter and jail (`deploy/fail2ban-*.conf`) to ban IPs with repeated failed logins.

### Or: Docker

```bash
cp .env.example .env     # set MCP_PUBLIC_URL and YTMUSIC_SECRET_KEY (docker compose run --rm ytmusic-mcp python -m ytmusic_mcp.cli genkey)
docker compose up -d --build
docker compose exec ytmusic-mcp python -m ytmusic_mcp.cli add-user alice
```

The port is published on `127.0.0.1` only; put nginx or Caddy in front for HTTPS (see `deploy/nginx.conf.template`). The database lives in the `ytmusic-data` volume.

### 4. Connect from Claude or ChatGPT

1. **Settings → Connectors → Add custom connector.**
2. URL: `https://mcp.example.com/ytmusic-mcp` (the `MCP_PUBLIC_URL` you configured).
3. Leave Client ID and Secret empty (the client uses Dynamic Client Registration), or configure the `OAUTH_CLIENT_*` variables to use fixed ones.
4. On the sign-in page, check that the client name and redirect host look right, sign in, and (first time only) paste your YouTube headers.

Any client that supports remote MCP servers with OAuth should work the same way; the server answers the extra OAuth discovery URLs ChatGPT probes.

### Sign-in page

If `widgets/oauth-login/dist/index.html` exists (build it with `npm run build` in `widgets/oauth-login`), the React sign-in app is served. Otherwise a plain server-rendered page with the same fields is used, so a fresh clone works without Node.

### Upgrading from the single-file layout

Earlier versions ran `python server.py` with `requirements.txt`, `OAUTH_DB_PATH` and `YTMUSIC_CREDENTIAL_KEY`. Now:

- Run `pip install -e .` and start with `python -m ytmusic_mcp` (the systemd unit rendered by `deploy/install.sh` does this).
- `YTMUSIC_CREDENTIAL_KEY` and `OAUTH_DB_PATH` are still read, but the new database layout is different. Point `YTMUSIC_DB_PATH` at a **new file**; the server refuses to start on an old one. Re-create your users with `add-user`, and have each reconnect once.
- Connectors must be added again, because registered clients and tokens are not migrated.

## Security notes

- **At rest:** passwords are scrypt-hashed, OAuth tokens and codes are stored as SHA-256 hashes, and YouTube cookies are Fernet-encrypted. The encryption key lives only in the environment, never in the database. The database file and its folder are created with owner-only permissions.
- **Login:** the page shows which client is asking and where it will redirect, forbids framing, allows 5 password attempts per login request, and compares timing-safe against unknown users.
- **Tokens:** access tokens last 1 hour, refresh tokens 30 days and rotate on use. Revoking one token revokes the whole session, and deleting a user revokes everything.
- **Tool safety:** inputs are type-checked (`filter` and `privacy_status` are enums), batch sizes are capped, and destructive tools carry the MCP `destructiveHint`.
- **Process:** the systemd unit is sandboxed (read-only system, writes only to the data directory).
- **Limits:** this is built for a small, trusted group. There is no admin web UI, and anyone with the server's shell, `.env` and database can use every user's account. To sign everyone out after a compromise, stop the service, delete the database file, and restart (this also removes users and cookies).

## Development

```bash
pip install -e ".[test]"
ruff check . && ruff format --check .
pytest
```

The unit tests use mocks and need no account. A separate suite creates and deletes a private playlist on a real account (stdio-mode cookie file); it is skipped unless you ask for it:

```bash
python -m ytmusic_mcp.cli local-cookies
pytest --run-youtube-integration -v
```

Layout: `ytmusic_mcp/youtube_ops.py` holds the playlist operations, `tools.py` registers them as MCP tools, `ui.py` serves the playlist manager widget, `oauth.py`, `pages.py` and `store.py` implement the multi-user login and SQLite storage, `accounts.py` serves each user's YouTube client, `headers.py` parses pasted headers, and `cli.py` is the admin tool.

## License

[MIT](LICENSE)
