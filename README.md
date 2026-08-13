# YouTube Music MCP Server (personal, single-user)

An MCP server wrapping `ytmusicapi` so Claude (and optionally ChatGPT) can
search YouTube Music and manage playlists: create/delete, add/remove tracks,
reorder tracks (top/bottom/before another track).

**Scope of this setup:** built for exactly one user (you), hosted on your
own Ubuntu box, reachable only with a secret bearer token. Recommended to
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

## 3. Local test (optional, before deploying)

```bash
python3 -c "from server import get_client; print(get_client().get_library_playlists(limit=5))"
```

---

## 4. Deploy on your Ubuntu server

### 4.1 Copy the project over

```bash
sudo mkdir -p /opt/ytmusic-mcp
sudo cp server.py setup_auth.py requirements.txt .env.example /opt/ytmusic-mcp/
sudo cp browser.json /opt/ytmusic-mcp/      # the file setup_auth.py made
cd /opt/ytmusic-mcp
sudo pip install -r requirements.txt --break-system-packages
```

### 4.2 Configure

```bash
sudo cp .env.example /opt/ytmusic-mcp/.env
openssl rand -hex 32                          # generate your token
sudo nano /opt/ytmusic-mcp/.env               # paste it into MCP_BEARER_TOKEN
```

Leave `MCP_HOST=127.0.0.1` — the app must never bind directly to the public
interface. nginx is the only thing facing the internet.

Both direct Python runs and the systemd unit load this same `.env` file.
Values already exported by the shell or supplied by systemd take precedence.
nginx does not read dotenv files, so if you change `MCP_PORT` from `8787`,
also change the port in `proxy_pass` in `nginx-ytmusic-mcp.conf`.

### 4.3 Run as a systemd service

```bash
sudo cp ytmusic-mcp.service /etc/systemd/system/
sudo nano /etc/systemd/system/ytmusic-mcp.service   # set User= to your linux user
sudo systemctl daemon-reload
sudo systemctl enable --now ytmusic-mcp
sudo systemctl status ytmusic-mcp
journalctl -u ytmusic-mcp -f                        # tail logs
```

### 4.4 nginx — only port 443, only path /mcp

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

This config makes **only `/mcp` reachable**; every other path (`/`,
`/admin`, `/.env`, etc.) returns a bare 404 before hitting your app at all.
It also rate-limits requests (30/min, burst 10) to blunt brute-force/scan
traffic.

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
curl -i https://mcp.yourdomain.com/mcp      # expect 401 (reachable, but locked)
curl -i https://mcp.yourdomain.com/anything # expect 404 (nothing else exposed)
```

Your MCP endpoint: **`https://mcp.yourdomain.com/mcp`**

---

## 5. Connect from Claude.ai

1. **Settings → Connectors → Add custom connector.**
2. URL: `https://mcp.yourdomain.com/mcp`
3. Advanced settings → add header: `Authorization: Bearer <your MCP_BEARER_TOKEN>`
4. Save, then enable it in a conversation via **+ → Connectors**.

## 6. Connect from ChatGPT

ChatGPT's remote-connector/custom-header support has been shifting between
plan tiers and connector types — worth checking OpenAI's current docs for
the exact click path when you get there. The server side doesn't change:
it's a standard endpoint at `https://mcp.yourdomain.com/mcp` expecting
`Authorization: Bearer <token>`. If their UI doesn't support custom headers
for a given connector type, that combination just won't work until it does
— nothing to fix on your end in that case.

---

## Security summary

- Token is the only thing standing between the internet and your YouTube
  account (via `browser.json`) — treat it like a root password.
- App binds `127.0.0.1` only; nginx is the sole public-facing surface, and
  only exposes `/mcp` on `:443`.
- Rate limiting + fail2ban blunt scanning/brute-force attempts.
- Using a secondary Google account caps the blast radius of a leak to that
  account's playlists, not your main one.
- Rotate the token any time you suspect exposure: regenerate, update `.env`,
  `sudo systemctl restart ytmusic-mcp`.
- `browser.json` can expire — re-run `setup_auth.py` (on your dev machine,
  then re-copy to the server) if calls start failing with auth errors.

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
