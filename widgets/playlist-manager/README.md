# YouTube Music MCP playlist manager

A self-contained Vite + React MCP App for the server's playlist workflows. It uses the official MCP Apps bridge, retains ChatGPT bridge compatibility, and builds to one HTML resource that Python serves directly.

```bash
npm install
npm run dev
npm run lint
npm run build
```

Commit `dist/index.html` after UI changes: it is the runtime artifact served directly by `server.py`. Artwork uses small backend-selected thumbnails, native lazy loading, and virtualized track rows so large playlists do not request every image at once.
