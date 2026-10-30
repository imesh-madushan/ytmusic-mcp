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

## Copying your YouTube headers

Playlist access uses the request headers of a logged-in YouTube Music session. **Where you paste them depends on the mode:**

- **Server mode (http):** nothing to do up front, and no command line. The first time you connect a client, the sign-in page asks for the headers, checks them and stores them encrypted.
- **Local mode (stdio):** there is no sign-in page, so you save them once with a command (see Option A).

To copy them:

1. Open [music.youtube.com](https://music.youtube.com) and sign in.
2. Open DevTools (F12) → **Network**, filter by `youtubei`, then play a song or reload the page.
3. Right-click a `youtubei/v1/...` request (`browse` or `next`) → **Copy** → **Copy as cURL (bash)** (in Firefox: Copy Value → Copy Request Headers).

Raw headers, a JSON header list, Firefox/Chrome request JSON, HAR and copied cURL are all accepted. Cookies go stale eventually. In server mode the failing tool returns a one-time link where you paste fresh headers; in local mode, run the command again.

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

### 1. Run the guided setup (Linux server)

You need: a Linux server with Python 3.10+ (`python3-venv` on Debian/Ubuntu), a domain name that already points at it, and ports 80 and 443 open.

```bash
git clone https://github.com/imesh-madushan/ytmusic-mcp && cd ytmusic-mcp
./deploy/install.sh
```

The script prepares a Python environment and starts a setup wizard that asks for your domain, a username and a password, shows what it is about to do, and then does it step by step:

1. **Checks the machine:** Python, memory, whether the database folder is on a network disk (SQLite must not be), DNS, and which web server already uses ports 80/443.
2. **Configures the app:** writes `.env` (with a freshly generated encryption key), creates your first user, and can optionally store your YouTube headers right away (normally you just paste them on the sign-in page the first time you connect).
3. **Sets up HTTPS and the web server:**
   - **Caddy** (recommended; it gets certificates itself): your site is added to the Caddyfile. If Caddy is already serving other sites, nothing existing is changed: the file is backed up, the change is validated first, applied with a graceful reload, checked, and **restored automatically** if anything looks wrong. If no web server is installed, it offers to install Caddy (Debian/Ubuntu).
   - **nginx:** gets the certificate first, then installs the site.
   - **Anything else:** it prints exactly which paths to forward, and a ready-made Caddy/nginx example is in `deploy/`.
4. **Installs the systemd service** and starts it.
5. **Checks it from the outside** and prints how to connect your client.

It is safe to run again, and it never edits system files without showing the command first. Useful options (see `python -m ytmusic_mcp.cli setup --help`):

| Option | What it does |
|---|---|
| `--dry-run` | Show everything it would do and change nothing |
| `--domain`, `--username` | Answer those questions up front |
| `--proxy caddy\|nginx\|none` | Choose the web server instead of auto-detecting |
| `--yes` | No questions (needs `--domain`, `--username` and the password in `YTMUSIC_SETUP_PASSWORD`) |
| `--replace-site` | Allow replacing an existing Caddy site for the same domain (a backup is kept) |
| `--caddyfile PATH` | Use a Caddyfile other than `/etc/caddy/Caddyfile` |
| `--skip-service` | Do not install the systemd service |

The wizard is for Linux servers. For Docker see below; for your own computer see Option A.

**Back up `YTMUSIC_SECRET_KEY`** (in `.env`). It encrypts every stored cookie; without it users must reconnect YouTube.

### 2. Add more users

```bash
.venv/bin/python -m ytmusic_mcp.cli add-user alice          # asks for a password (12+ chars)
```

That is all you need to do. The first time alice connects a client, the sign-in page asks her to paste her YouTube headers, checks them, and stores them encrypted. When YouTube later expires the session, the failing tool call returns a one-time link (valid 10 minutes) where only she can paste fresh headers; the `reconnect_youtube_music` tool returns the same link on request.

Other admin commands: `list-users`, `set-password <name>`, `delete-user <name>` (also revokes their tokens).

**Optional shortcut:** if someone cannot use the sign-in page, you can store their headers from the command line instead. Changed cookies are picked up immediately; no restart needed.

```bash
.venv/bin/python -m ytmusic_mcp.cli set-cookies alice       # paste headers, or --file headers.txt
.venv/bin/python -m ytmusic_mcp.cli check-cookies alice     # confirm YouTube accepts them
```

### 3. Keep it up to date

Once the server is set up, `./deploy/install.sh` takes a command instead of asking setup questions. There are two ways to update, plus two small helpers:

| Command | Use it for | What it does |
|---|---|---|
| `./deploy/install.sh update` | **Normal updates (quick)** | Pulls the latest code (fast-forward only), updates the Python packages, restarts the service, and checks it from the outside. No UI build, no config changes. |
| `./deploy/install.sh rebuild` | **Full rebuild** | Updates the Python packages, **builds the Vite UI apps**, re-applies the systemd unit and the web server config (with the same backup, validation and automatic rollback as the setup), restarts, and checks. It works on the code that is already checked out; run `update` first if you want the newest code. |
| `./deploy/install.sh restart` | After editing `.env` | Restarts the service and checks it answers. |
| `./deploy/install.sh status` | Any time | Shows the version and whether the service, app, web server and public site are healthy. Needs no network access to GitHub, and exits with an error code when something is wrong, so it works from cron or monitoring. |

`update` tells you when a rebuild is worthwhile: if the UI source or the files in `deploy/` changed, or when tools or the playlist UI changed and you should refresh the connector in your client.

Good to know:

- **Add `--dry-run`** to `update` or `rebuild` to see every step without changing anything.
- **`update` is careful:** it refuses to run over local edits, stops with advice if the remote history was rewritten, and never merges.
- **`rebuild` and the UI apps:** it needs Node.js 18+ (`npm`) to build. It builds only the apps whose build output is not committed to git (the sign-in page); the playlist UI ships prebuilt. Use `--build-all-ui` to rebuild that one too, or `--skip-ui` to skip building. On a small server (under 1.5 GB RAM with no swap) it warns first, because a Vite build can run out of memory; you can build on another computer and copy `widgets/<app>/dist/index.html` over.
- **Other web servers:** `rebuild --proxy caddy|nginx|none` chooses what to re-apply; a web server it does not manage is left alone, and so are other people's Caddy sites.

### Or: Docker

```bash
cp .env.example .env     # set MCP_PUBLIC_URL and YTMUSIC_SECRET_KEY (docker compose run --rm ytmusic-mcp python -m ytmusic_mcp.cli genkey)
docker compose up -d --build
docker compose exec ytmusic-mcp python -m ytmusic_mcp.cli add-user alice
```

The port is published on `127.0.0.1` only; put Caddy, nginx or another reverse proxy in front for HTTPS (see `deploy/`). The database lives in the `ytmusic-data` volume.

### 4. Connect from Claude or ChatGPT

1. **Settings → Connectors → Add custom connector.**
2. URL: `https://mcp.example.com/ytmusic-mcp` (the `MCP_PUBLIC_URL` you configured).
3. Leave Client ID and Secret empty (the client uses Dynamic Client Registration), or configure the `OAUTH_CLIENT_*` variables to use fixed ones.
4. On the sign-in page, check that the client name and redirect host look right, sign in, and (first time only) paste your YouTube headers.

Any client that supports remote MCP servers with OAuth should work the same way; the server answers the extra OAuth discovery URLs ChatGPT probes.

### Sign-in page

The React sign-in app is prebuilt and committed (`widgets/oauth-login/dist/index.html`), so a fresh clone shows it with no Node.js needed. To change it, edit `widgets/oauth-login/src`, run `npm run build` there, and commit the new `dist/index.html`. If that file is ever missing, a plain server-rendered page with the same fields is used instead.

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
