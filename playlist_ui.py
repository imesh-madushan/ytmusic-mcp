"""Cross-host MCP playlist UI resources."""

from __future__ import annotations

import json
from typing import Any

from mcp_ui_server import UIMetadataKey, UIResource, create_ui_resource

PLAYLIST_APP_URI = "ui://ytmusic/playlist-tracks"
PLAYLIST_OPENAI_URI = "ui://ytmusic/playlist-tracks-openai"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
OPENAI_WIDGET_MIME_TYPE = "text/html+skybridge"


def _safe_json(value: Any) -> str:
    """Serialize data for an inline script without allowing HTML tag injection."""
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def playlist_tracks_html(playlist: dict[str, Any] | None = None) -> str:
    """Return one self-contained view supporting MCP Apps, ChatGPT, and legacy MCP-UI."""
    playlist_json = _safe_json(playlist)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube Music playlist</title>
<style>
:root {{ color-scheme: light dark; font-family: var(--font-sans, Inter, ui-sans-serif, system-ui, sans-serif); }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--color-background-primary, #09090b); color: var(--color-text-primary, #fafafa); }}
.shell {{ min-height: 100vh; padding: 12px; }}
header {{ display: flex; align-items: center; justify-content: space-between; gap: 12px;
  max-width: 960px; margin: 0 auto 10px; }}
.eyebrow {{ margin: 0 0 2px; color: #fb7185; font-size: 10px; font-weight: 750;
  letter-spacing: .12em; text-transform: uppercase; }}
h1 {{ margin: 0; font-size: 20px; line-height: 1.15; letter-spacing: -.02em; }}
.description {{ display: none; }}
.count {{ flex: 0 0 auto; color: var(--color-text-secondary, #d4d4d8); font-size: 12px; }}
.controls {{ max-width: 960px; margin: 0 auto 8px; }}
input {{ width: 100%; border: 1px solid var(--color-border-primary, #27272a); border-radius: 8px;
  padding: 8px 10px; background: var(--color-background-secondary, rgba(24,24,27,.88));
  color: inherit; font: inherit; outline: none; }}
input:focus {{ border-color: #fb7185; box-shadow: 0 0 0 3px rgba(251,113,133,.14); }}
.list {{ max-width: 960px; margin: auto; overflow: hidden;
  border: 1px solid var(--color-border-primary, #27272a); border-radius: 10px;
  background: var(--color-background-secondary, rgba(24,24,27,.76)); }}
.track {{ display: grid; grid-template-columns: 28px minmax(0,1fr) minmax(90px,.5fr) 42px;
  gap: 10px; align-items: center; min-height: 48px; padding: 6px 10px;
  border-bottom: 1px solid var(--color-border-primary, #27272a); text-decoration: none; color: inherit; }}
.track:last-child {{ border-bottom: 0; }}
.track:hover {{ background: var(--color-background-tertiary, rgba(63,63,70,.55)); }}
.position {{ color: var(--color-text-tertiary, #71717a); text-align: right; font-variant-numeric: tabular-nums; }}
.title, .artist, .album {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.title {{ font-size: 13px; font-weight: 650; }}
.artist, .album, .duration {{ margin-top: 2px; color: var(--color-text-secondary, #a1a1aa); font-size: 11px; }}
.duration {{ margin: 0; font-variant-numeric: tabular-nums; }}
.empty {{ padding: 48px 20px; color: var(--color-text-secondary, #a1a1aa); text-align: center; }}
.hidden {{ display: none; }}
@media (max-width: 680px) {{
  .shell {{ padding: 10px 8px; }}
  .track {{ grid-template-columns: 24px minmax(0,1fr) 38px; padding: 6px 8px; gap: 8px; }}
  .album {{ display: none; }}
}}
</style>
</head>
<body>
<main class="shell">
  <header>
    <div><p class="eyebrow">Playlist</p><h1 id="playlist-title">Loading playlist…</h1>
      <p class="description hidden" id="playlist-description"></p></div>
    <div class="count" id="track-count"></div>
  </header>
  <div class="controls"><input id="filter" type="search" placeholder="Filter tracks, artists, or albums…" aria-label="Filter playlist tracks" disabled></div>
  <section class="list" id="track-list" aria-live="polite"><div class="empty">Waiting for playlist data…</div></section>
</main>
<script>
let playlist = null;
let tracks = [];
const initialPlaylist = {playlist_json};
const title = document.getElementById('playlist-title');
const description = document.getElementById('playlist-description');
const count = document.getElementById('track-count');
const list = document.getElementById('track-list');
const filter = document.getElementById('filter');

function trackSearchText(track) {{
  return [track.title, track.artists, track.album].filter(Boolean).join(' ').toLowerCase();
}}
function render(query = '') {{
  const normalized = query.trim().toLowerCase();
  const visible = tracks.map((track, index) => ({{track, index}}))
    .filter(item => !normalized || trackSearchText(item.track).includes(normalized));
  list.replaceChildren();
  count.textContent = normalized ? `${{visible.length}} of ${{tracks.length}} tracks` : `${{tracks.length}} track${{tracks.length === 1 ? '' : 's'}}`;
  if (!visible.length) {{
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = tracks.length ? 'No tracks match this filter.' : 'This playlist is empty.';
    list.append(empty);
    return;
  }}
  for (const item of visible) {{
    const track = item.track;
    const row = document.createElement(track.videoId ? 'a' : 'div');
    row.className = 'track';
    if (track.videoId) {{
      row.href = `https://music.youtube.com/watch?v=${{encodeURIComponent(track.videoId)}}`;
      row.target = '_blank'; row.rel = 'noopener noreferrer';
    }}
    const position = document.createElement('span');
    position.className = 'position'; position.textContent = String(item.index + 1);
    const main = document.createElement('div');
    const trackTitle = document.createElement('div');
    trackTitle.className = 'title'; trackTitle.textContent = track.title || 'Unknown track';
    const artist = document.createElement('div');
    artist.className = 'artist'; artist.textContent = track.artists || 'Unknown artist';
    main.append(trackTitle, artist);
    const album = document.createElement('div');
    album.className = 'album'; album.textContent = track.album || '—';
    const duration = document.createElement('div');
    duration.className = 'duration'; duration.textContent = track.duration || '';
    row.append(position, main, album, duration);
    list.append(row);
  }}
}}
function applyPlaylist(value) {{
  if (!value || !Array.isArray(value.tracks)) return;
  playlist = value; tracks = value.tracks;
  title.textContent = value.title || 'Untitled playlist';
  description.textContent = value.description || '';
  description.classList.toggle('hidden', !value.description);
  filter.disabled = false;
  render(filter.value);
}}
function acceptToolResult(result) {{
  applyPlaylist(result && (result.structuredContent || result));
}}
filter.addEventListener('input', event => render(event.target.value));

// Modern MCP Apps (SEP-1865): initialize the View and receive structured tool results.
const initId = 'ytmusic-playlist-init';
function post(message) {{ window.parent.postMessage(message, '*'); }}
window.addEventListener('message', event => {{
  const message = event.data;
  if (!message || message.jsonrpc !== '2.0') {{
    if (message && message.type === 'openai:set_globals')
      acceptToolResult(message.globals && message.globals.toolOutput);
    return;
  }}
  if (message.id === initId && message.result) {{
    const variables = message.result.hostContext && message.result.hostContext.styles && message.result.hostContext.styles.variables;
    if (variables) for (const [key, value] of Object.entries(variables)) if (value) document.documentElement.style.setProperty(key, value);
    post({{jsonrpc:'2.0', method:'ui/notifications/initialized', params:{{}}}});
  }} else if (message.method === 'ui/notifications/tool-result') {{
    acceptToolResult(message.params);
  }} else if (message.method === 'ui/resource-teardown' && message.id !== undefined) {{
    post({{jsonrpc:'2.0', id:message.id, result:{{}}}});
  }}
}});
post({{
  jsonrpc: '2.0', id: initId, method: 'ui/initialize',
  params: {{
    protocolVersion: '2026-01-26',
    appInfo: {{name:'ytmusic-playlist-tracks', title:'YouTube Music Playlist', version:'1.0.0'}},
    appCapabilities: {{availableDisplayModes:['inline','fullscreen']}}
  }}
}});

// ChatGPT Apps bridge and legacy embedded MCP-UI both remain supported.
window.addEventListener('openai:set_globals', event => {{
  const globals = event.detail && event.detail.globals;
  if (globals) acceptToolResult(globals.toolOutput);
}});
if (window.openai && window.openai.toolOutput) acceptToolResult(window.openai.toolOutput);
if (initialPlaylist) applyPlaylist(initialPlaylist);
</script>
</body>
</html>"""


def create_playlist_tracks_ui(playlist: dict[str, Any]) -> UIResource:
    """Create a legacy embedded MCP-UI resource with the current playlist data."""
    return create_ui_resource(
        {
            "uri": PLAYLIST_APP_URI,
            "content": {"type": "rawHtml", "htmlString": playlist_tracks_html(playlist)},
            "encoding": "text",
            "uiMetadata": {
                UIMetadataKey.PREFERRED_FRAME_SIZE: ["100%", "680px"],
                UIMetadataKey.INITIAL_RENDER_DATA: playlist,
            },
        }
    )
