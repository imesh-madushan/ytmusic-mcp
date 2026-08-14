# YouTube Music MCP Server (personal, single-user)

An MCP server wrapping `ytmusicapi` so Claude (and optionally ChatGPT) can
search YouTube Music and manage playlists: create/delete, add/remove tracks,
reorder tracks (top/bottom/before another track). UI-capable MCP clients can
also render a responsive, fast, filterable playlist track browser.

**Scope of this setup:** built for exactly one user (you), hosted on your
own Ubuntu box, protected by an OAuth 2.1 login. Recommended to
run against a **secondary Google account**, not your main one, since a
leaked token means full playlist read/write access to whatever account is
authenticated.

---

## 1. Install

```bash
pip install -r requirements.txt
```

## 2. Auth against YouTube Music (secondary account)

Log into your **secondary Gmail/YouTube account** in the browser you use for
this, then run:

```bash
python3 setup_auth.py
```

Follow the printed steps (DevTools → Network tab → copy request headers
from a music.youtube.com request). This writes `browser.json`, which
`server.py` reads automatically. Cookie-based auth can go stale — re-run
this if tools start failing.

Paste the headers, then press Enter on one additional empty line. Firefox's
structured request-header JSON export is also accepted. This project uses
browser-cookie authentication only; OAuth credentials are rejected.

## Playlist manager UI

Call `open_music_manager_ui` to open the full manager, or
`get_playlist_tracks_ui` with a playlist ID to open a playlist directly. The
React app supports listing, creating, editing, and deleting playlists; searching
and adding music; filtering, bulk removal, and track reordering. It advertises
standard MCP Apps metadata and ChatGPT Apps compatibility. Hosts without UI support receive text and structured data.

Artwork is limited to small thumbnails and is loaded lazily only for virtualized
visible rows. To change the app, work in `widgets/playlist-manager`, run
`npm run build`, and commit `dist/index.html`. Restart the service and reconnect
the connector after UI/tool metadata changes so the host refreshes `tools/list`.

## 3. Local test (optional, before deploying)

```bash
python3 -c "from server import get_client; print(get_client().get_library_playlists(limit=5))"
```

---

## 4. Deploy on your Ubuntu server

### 4.1 Copy the project over

```bash
sudo mkdir -p /opt/ytmusic-mcp
sudo cp server.py oauth_provider.py oauth_store.py setup_auth.py requirements.txt .env.example /opt/ytmusic-mcp/
sudo mkdir -p /opt/ytmusic-mcp/widgets/playlist-manager/dist
sudo cp widgets/playlist-manager/dist/index.html /opt/ytmusic-mcp/widgets/playlist-manager/dist/
sudo cp browser.json /opt/ytmusic-mcp/      # the file setup_auth.py made
sudo mkdir -p /home/ubuntu/prj/ytmusic-mcp/data
sudo chown ubuntu:ubuntu /home/ubuntu/prj/ytmusic-mcp/data  # use the service User/Group
cd /opt/ytmusic-mcp
sudo pip install -r requirements.txt --break-system-packages
```

### 4.2 Configure

```bash
sudo cp .env.example /opt/ytmusic-mcp/.env
openssl rand -base64 32                       # generate the OAuth login password
sudo nano /opt/ytmusic-mcp/.env               # set public URL, username, and password
```

Leave `MCP_HOST=127.0.0.1` — the app must never bind directly to the public
interface. nginx is the only thing facing the internet.

Both direct Python runs and the systemd unit load this same `.env` file.
Values already exported by the shell or supplied by systemd take precedence.

`OAUTH_DB_PATH` stores dynamically registered clients and issued access/refresh
tokens. Keep it on persistent local storage in a directory writable only by the
service user. Restarting or upgrading the service will then preserve connector
registrations and active sessions.
nginx does not read dotenv files, so if you change `MCP_PORT` from `8787`,
also change the port in `proxy_pass` in `nginx-ytmusic-mcp.conf`.

Set `YTMUSIC_LOG_LEVEL=DEBUG` for detailed application diagnostics. Dependency
protocol logs remain at `MCP_LIBRARY_LOG_LEVEL=INFO` by default so journald does
not print raw SSE/JSON-RPC payloads. Tool failures are logged with tracebacks and
are also returned to the MCP client as tool errors.

### 4.3 Run as a systemd service

```bash
sudo cp ytmusic-mcp.service /etc/systemd/system/
sudo nano /etc/systemd/system/ytmusic-mcp.service   # set User= to your linux user
sudo systemctl daemon-reload
sudo systemctl enable --now ytmusic-mcp
sudo systemctl status ytmusic-mcp
journalctl -u ytmusic-mcp -f                        # tail logs
```

### 4.4 nginx — HTTPS MCP and OAuth routes

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo cp nginx-ytmusic-mcp.conf /etc/nginx/sites-available/ytmusic-mcp
sudo nano /etc/nginx/sites-available/ytmusic-mcp    # replace mcp.yourdomain.com
sudo ln -s /etc/nginx/sites-available/ytmusic-mcp /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d mcp.yourdomain.com          # gets the TLS cert, edits the config
```

You need a domain/subdomain pointed at your server's public IP (any DNS A
record works), and ports 80+443 open in your firewall. Port 80 exists only
to redirect to HTTPS and serve certbot's renewal challenge — nothing else
is served there.

This config exposes `/ytmusic-mcp` and the small set of OAuth discovery,
registration, authorization, token, and login routes it requires. Other paths
(`/`, `/admin`, `/.env`, etc.) return a bare 404 before hitting the app.
It rate-limits MCP and login attempts (30/min, burst 10).

### 4.5 fail2ban — auto-ban repeat offenders

```bash
sudo apt install fail2ban
sudo cp fail2ban-filter-ytmusic-mcp.conf /etc/fail2ban/filter.d/ytmusic-mcp.conf
sudo cp fail2ban-jail-ytmusic-mcp.conf /etc/fail2ban/jail.d/ytmusic-mcp.conf
sudo systemctl restart fail2ban
sudo fail2ban-client status ytmusic-mcp     # confirm the jail is active
```

Any IP racking up 5 unauthorized/probing requests in 10 minutes gets banned
for 24h.

### 4.6 Verify from outside

```bash
curl -i https://mcp.yourdomain.com/ytmusic-mcp # expect 401 with OAuth metadata
curl -i https://mcp.yourdomain.com/anything # expect 404 (nothing else exposed)
```

Your MCP endpoint: **`https://mcp.yourdomain.com/ytmusic-mcp`**

---

## 5. Connect from Claude.ai

1. **Settings → Connectors → Add custom connector.**
2. URL: `https://mcp.yourdomain.com/ytmusic-mcp`
3. Normally leave Client ID and Client Secret empty; Claude will use OAuth
   Dynamic Client Registration. To use the advanced fields instead, configure
   the matching `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, and Claude callback
   URI in `OAUTH_REDIRECT_URIS` first.
4. Save, complete the private username/password login, then enable it in a
   conversation via **+ → Connectors**.

## 6. Connect from ChatGPT

ChatGPT's remote-connector/custom-header support has been shifting between
plan tiers and connector types — worth checking OpenAI's current docs for
the exact click path when you get there. The server side doesn't change:
it's a standard endpoint at `https://mcp.yourdomain.com/ytmusic-mcp` using
the standard MCP OAuth discovery and authorization flow.

---

## Security summary

- The OAuth login protects access to your YouTube account (via `browser.json`)
  — use a generated password and keep it private.
- App binds `127.0.0.1` only; nginx is the sole public-facing surface and
  exposes only the MCP resource and required OAuth routes on `:443`.
- Rate limiting + fail2ban blunt scanning/brute-force attempts.
- Using a secondary Google account caps the blast radius of a leak to that
  account's playlists, not your main one.
- Rotate the OAuth password any time you suspect exposure: regenerate, update `.env`,
  `sudo systemctl restart ytmusic-mcp`. Rotating this login password does not revoke already issued OAuth tokens.
- To sign out every connector after a compromise, stop the service, move or delete
  the file configured by `OAUTH_DB_PATH`, and restart. This reset is destructive;
  every client must register and authorize again.
- Back up the OAuth database if preserving connector registrations across server
  migrations matters. Treat the backup as a secret because registered client
  credentials are stored in it.
- `browser.json` can expire — re-run `setup_auth.py` (on your dev machine,
  then re-copy to the server) if calls start failing with auth errors.

## Real-account integration tests

The integration suite uses `browser.json` and performs real YouTube Music
writes. It creates a uniquely named private playlist and deletes it during
cleanup, including when a test fails. Run it explicitly with:

```bash
python3 -m pip install -r requirements-test.txt
pytest --run-youtube-integration -v
```

Without `--run-youtube-integration`, these account-mutating tests are skipped.

---

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

**ID note:** `videoId` identifies a song (used to *add*); `setVideoId`
identifies one occurrence of a track *inside a playlist* (used to *remove*
or *reorder*, since the same song can appear twice with different
setVideoIds). You never need to look these up manually — ask by song name
and the assistant calls `get_playlist` first to resolve the right ID.
