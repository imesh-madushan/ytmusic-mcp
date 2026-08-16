# YouTube Music MCP Server (personal, single-user)

An MCP server wrapping `ytmusicapi` so Claude (and optionally ChatGPT) can
search YouTube Music and manage playlists: create/delete, add/remove tracks,
reorder tracks (top/bottom/before another track). UI-capable MCP clients can
also render a responsive, fast, filterable playlist track browser.

**Scope of this setup:** supports isolated OAuth grants for multiple users, hosted on your
own Ubuntu box, protected by OAuth 2.1 and per-grant YouTube session validation. Recommended to
run against a **secondary Google account**, not your main one, since a
leaked token means full playlist read/write access to whatever account is
authenticated.

---

## 1. Install

```bash
pip install -r requirements.txt
```

## 2. Connect YouTube Music

Each OAuth authorization gets its own isolated YouTube Music session. During connector login, the server asks the user to copy request headers from a signed-in `music.youtube.com` browser tab. It validates them, encrypts them with `YTMUSIC_CREDENTIAL_KEY`, and stores them under that OAuth grant only. No shared or hardcoded `browser.json` is used.

If YouTube expires the browser session, a tool error supplies a short-lived private reconnect link. The `reconnect_youtube_music` tool can also generate that link. Open it, follow the same DevTools guidance, paste fresh headers, and retry the original request.

## Playlist manager UI

Call `open_music_manager_ui` to open the full manager, or
`get_playlist_tracks_ui` with a playlist ID to open a playlist directly. The
React app supports listing, creating, editing, and deleting playlists; searching
and adding music; filtering, bulk removal, and track reordering. Music and playlist
results also render as cards with Play, Copy link, and Open in YouTube Music actions.
Playback preserves requested order and appends deduplicated radio recommendations. It advertises
standard MCP Apps metadata and ChatGPT Apps compatibility. Hosts without UI support receive text and structured data.

Artwork is limited to small thumbnails and is loaded lazily only for virtualized
visible rows. To change the app, work in `widgets/playlist-manager`, run
`npm run build`, and commit `dist/index.html`. Restart the service and reconnect
the connector after UI/tool metadata changes so the host refreshes `tools/list`.

The OAuth sign-in page is a self-contained React app too. To change it, work in
`widgets/oauth-login`, run `npm run build`, and commit `dist/index.html`.

## 3. Local verification

Run `pytest -q`. YouTube Music calls require an authenticated HTTP MCP request and deliberately do not fall back to local shared cookies.

---

## 4. Deploy on your Ubuntu server

### 4.1 Copy the project over

```bash
sudo mkdir -p /opt/ytmusic-mcp
sudo cp server.py oauth_provider.py oauth_store.py setup_auth.py requirements.txt .env.example /opt/ytmusic-mcp/
sudo mkdir -p /opt/ytmusic-mcp/assets
sudo cp assets/connector-icon-96.png /opt/ytmusic-mcp/assets/
sudo mkdir -p /opt/ytmusic-mcp/widgets/playlist-manager/dist
sudo cp widgets/playlist-manager/dist/index.html /opt/ytmusic-mcp/widgets/playlist-manager/dist/
sudo mkdir -p /opt/ytmusic-mcp/widgets/oauth-login/dist
sudo cp widgets/oauth-login/dist/index.html /opt/ytmusic-mcp/widgets/oauth-login/dist/
sudo mkdir -p /home/ubuntu/prj/ytmusic-mcp/data
sudo chown ubuntu:ubuntu /home/ubuntu/prj/ytmusic-mcp/data  # use the service User/Group
cd /opt/ytmusic-mcp
sudo pip install -r requirements.txt --break-system-packages
```

### 4.2 Configure

```bash
sudo cp .env.example /opt/ytmusic-mcp/.env
openssl rand -base64 32                       # generate the OAuth login password
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" # credential key
sudo nano /opt/ytmusic-mcp/.env               # set the public URL and credential encryption key
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
4. Save, validate your private YouTube Music browser session, then enable it in a
   conversation via **+ → Connectors**.

## 6. Connect from ChatGPT

ChatGPT's remote-connector/custom-header support has been shifting between
plan tiers and connector types — worth checking OpenAI's current docs for
the exact click path when you get there. The server side doesn't change:
it's a standard endpoint at `https://mcp.yourdomain.com/ytmusic-mcp` using
the standard MCP OAuth discovery and authorization flow.

---

## Security summary

- The OAuth login protects access to each connector grant; every grant receives a unique subject and encrypted YouTube Music headers.
- `YTMUSIC_CREDENTIAL_KEY` decrypts all stored sessions. Keep it private and backed up; changing it invalidates existing sessions.
- App binds `127.0.0.1` only; nginx is the sole public-facing surface and
  exposes only the MCP resource and required OAuth routes on `:443`.
- Rate limiting + fail2ban blunt scanning/brute-force attempts.
- Using a secondary Google account caps the blast radius of a leak to that
  account's playlists, not your main one.
- Revoke individual connector grants when access should end; deleting the OAuth database signs out every connector.
- To sign out every connector after a compromise, stop the service, move or delete
  the file configured by `OAUTH_DB_PATH`, and restart. This reset is destructive;
  every client must register and authorize again.
- Back up the OAuth database if preserving connector registrations across server
  migrations matters. Treat the backup as a secret because registered client
  credentials are stored in it.
- YouTube cookies can expire. Tool errors provide a short-lived reconnect URL so that only the affected OAuth user replaces their own headers.

## Real-account integration tests

The integration suite performs real YouTube Music writes and must be adapted to an authenticated test OAuth subject before it is enabled. It creates a uniquely named private playlist and deletes it during
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
| `reconnect_youtube_music` | Return a short-lived private setup URL for missing or expired YouTube browser credentials |
| `search_music` | Search songs/videos/albums/artists/playlists |
| `show_music_ui` | Show track or playlist cards with Play, Copy link, and Open actions |
| `play_music` | Play requested video IDs in order, followed by optional recommendations |
| `play_playlist` | Play a playlist from a chosen index, followed by optional recommendations |
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
