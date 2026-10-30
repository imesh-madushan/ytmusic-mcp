# YouTube Music MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) search YouTube Music and manage playlists: create, rename and delete playlists, add and remove tracks, and reorder them.

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
| `create_playlist` | Create a playlist (`PRIVATE` by default) |
| `rename_playlist` | Edit title, description or privacy |
| `delete_playlist` | Delete a playlist *(marked destructive)* |
| `add_tracks_to_playlist` | Add tracks by `videoId`, optionally to the top |
| `remove_tracks_from_playlist` | Remove tracks by `setVideoId` *(marked destructive)* |
| `reorder_playlist_track` | Move a track before another track, or to the end |
| `move_track_to_top` / `move_track_to_bottom` | Move a track to either end |

`videoId` identifies a song (used to add it). `setVideoId` identifies one occurrence of a song inside a playlist (used to remove or reorder), because the same song can appear twice. You never look these up yourself: the assistant calls `get_playlist` first.

## Getting your YouTube cookies

Both modes need the request headers of a logged-in YouTube Music session:

1. Open [music.youtube.com](https://music.youtube.com) and sign in.
2. Open DevTools (F12) → **Network**, then reload the page.
3. Select a `music.youtube.com/youtubei/v1/browse` request and copy its request headers.

Paste them when the commands below ask (finish with an empty line), or save them to a file and pass `--file headers.txt`. Cookies go stale eventually; when tools start failing with an "expired" error, repeat this and re-run the same command.

## Option A: local use with Claude Desktop (stdio)

Requires Python 3.10+.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install .
python -m ytmusic_mcp.cli local-cookies      # writes browser.json (permissions 600)
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

**Back up `YTMUSIC_SECRET_KEY`.** It encrypts every stored cookie; without it users must re-add their cookies.

### 2. Add users

```bash
.venv/bin/python -m ytmusic_mcp.cli add-user alice          # asks for a password (12+ chars)
.venv/bin/python -m ytmusic_mcp.cli set-cookies alice       # paste headers, or --file headers.txt
.venv/bin/python -m ytmusic_mcp.cli check-cookies alice     # confirm YouTube accepts them
```

Other commands: `list-users`, `set-password <name>`, `delete-user <name>` (also revokes their tokens). Changed cookies are picked up immediately; no restart needed.

### 3. Start it

Follow the printed commands to enable the systemd service, put nginx in front and get a certificate with certbot. You need a domain pointing at the server and ports 80 and 443 open. The app listens on `127.0.0.1` only; nginx is the only public surface, rate-limits the OAuth and login routes, and returns 404 for everything else.

Optional: install the fail2ban filter and jail (`deploy/fail2ban-*.conf`) to ban IPs with repeated failed logins.

### Or: Docker

```bash
cp .env.example .env     # set MCP_PUBLIC_URL and YTMUSIC_SECRET_KEY (docker compose run --rm ytmusic-mcp python -m ytmusic_mcp.cli genkey)
docker compose up -d --build
docker compose exec ytmusic-mcp python -m ytmusic_mcp.cli add-user alice
docker compose exec ytmusic-mcp python -m ytmusic_mcp.cli set-cookies alice
```

The port is published on `127.0.0.1` only; put nginx or Caddy in front for HTTPS (see `deploy/nginx.conf.template`). The database lives in the `ytmusic-data` volume.

### 4. Connect from Claude

1. **Settings → Connectors → Add custom connector.**
2. URL: `https://mcp.example.com/ytmusic-mcp` (the `MCP_PUBLIC_URL` you configured).
3. Leave Client ID and Secret empty (Claude uses Dynamic Client Registration), or configure the `OAUTH_CLIENT_*` variables to use fixed ones.
4. On the login page, check that the client name and redirect host look right, then sign in with your username and password.

Any client that supports remote MCP servers with OAuth should work the same way; only Claude has been tested.

## Security notes

- **At rest:** passwords are scrypt-hashed, OAuth tokens and codes are stored as SHA-256 hashes, and YouTube cookies are Fernet-encrypted. The encryption key lives only in the environment, never in the database. The database file and its folder are created with owner-only permissions.
- **Login:** the page shows which client is asking and where it will redirect, forbids framing, allows 5 password attempts per login request, and compares timing-safe against unknown users.
- **Tokens:** access tokens last 1 hour, refresh tokens 30 days and rotate on use. Revoking one token revokes the whole session, and deleting a user revokes everything.
- **Tool safety:** inputs are type-checked (`filter` and `privacy_status` are enums), batch sizes are capped, and destructive tools carry the MCP `destructiveHint`.
- **Process:** the systemd unit is sandboxed (read-only system, writes only to the data directory).
- **Limits:** this is built for a small, trusted group. There is no admin web UI, and anyone with the server's shell, `.env` and database can use every user's account.

## Development

```bash
pip install -e ".[test]"
ruff check . && ruff format --check .
pytest
```

The unit tests use mocks and need no account. A separate suite creates and deletes a private playlist on a real account; it is skipped unless you ask for it:

```bash
python -m ytmusic_mcp.cli local-cookies
pytest --run-youtube-integration -v
```

Layout: `ytmusic_mcp/youtube_ops.py` holds the playlist operations, `tools.py` registers them as MCP tools, `oauth.py` and `store.py` implement the multi-user login and SQLite storage, `accounts.py` serves each user's YouTube client, and `cli.py` is the admin tool.

## License

[MIT](LICENSE)
