# AGENTS.md

## What this repo is

Two separate projects coexist here:

1. **Crop tool (legacy, stable)** — `index.html` at the repo root: a fully client-side,
   single-file web app for cropping photos out of flatbed scans. No build, no deps.
   Documented by `README.md` and `spec.md` (both describe only this app).
2. **Book viewer (actively developed)** — `server/`: a FastAPI tile server + an
   ES-module JS viewer (`server/static/`). DeepZoom-style pan/zoom over scanned book
   pages, OCR text search, name filter. This is where all recent commits land.

Supporting files:

- `open.py` — stdlib-only server that serves one scan over localhost and opens the crop
  tool; auto-exits after the page loads (`PHOTO_CROP_BROWSER` overrides the browser).
- `scan-crop` — bash wrapper: scans the Epson ET-7750 (color/1200dpi/PNG) into an
  ingest folder (`SCAN_CROP_INGEST`, default `~/Docker-Server/copyparty/data/private/Photos/Scans/Ingest/`),
  then opens the result via `open.py`.
- `book.html` — **legacy single-file snapshot** of the book viewer, superseded by
  `server/static/`. Do not edit it for new work; the module-based viewer in
  `server/static/` is the live one.

Note: `README.md` and `spec.md` predate the server and say "no server, no build step".
The server contradicts that. Trust the code, not the docs.

## Commands

- **Run the server** (from the repo root, package-relative imports require this):
  `python -m server.main` (equivalently `uvicorn server.main:app`). Honors `HOST`/`PORT`.
- **Deps**: `pip install -r server/requirements.txt`. Includes fastapi, uvicorn, pydantic,
  opencv-contrib-python-headless, numpy, pillow, diskcache, pytesseract.
- **Docker**: `docker build -f server/Dockerfile .` (runs `python -m server.main`;
  installs tesseract + eng language data and the OpenCL ICD loader; NVIDIA OpenCL is
  injected at runtime with `--gpus all`, otherwise it transparently falls back to CPU).
- **JS syntax checks**: `node` is NOT installed. Use `bun` (`/home/kuno/.bin/bun`).
  The viewer is plain browser ES modules, so a quick import/syntax smoke test is
  `bun build server/static/js/main.js --outdir /tmp/check --target browser`.
- **No tests, no linter config, no Makefile, no pyproject.toml** exist. There is no test
  suite to run.

## Server architecture (`server/`)

FastAPI app assembled in `server/app.py` `create_app()`: services are constructed once and
exposed on `app.state` (`.settings`, `.tiles`, `.ocr`, `.locations`, `.catalog`); routers
fetch them from `request.app.state` — never construct their own. Static viewer is mounted
at `/` with `no-cache` headers (dev-friendly; tiles are separate and immutable).

### Config — `server/config.py`

Single `Settings` (pydantic-settings) reads env vars (`.env` also supported); the only
module that knows env names. Defaults target `/archive/Library` (books root) and
`/archive/cache`. Key knobs: `ARCHIVE_ROOT`, `CACHE_DIR`, `CACHE_GB` (encoded-tile disk
budget), `PAGE_CACHE_BYTES` (decoded-page RAM budget), `PAGE_IDLE_SECONDS` (default 10),
`TILE_SIZE` (256), `JPEG_QUALITY`/`JPEG_PROGRESSIVE`, `OPENCL`, `OCR_*` (cache dir, max dim
3000, lang, conf threshold 40), `HOST`, `PORT`.

### Errors — `server/errors.py`

Lower layers raise `AppError` subclasses (`NotFound` → 404, `BadRequest` → 400) without
knowing about HTTP; `register_error_handlers` maps them to `{"error": message}` JSON.
All domain errors should use these, never bare HTTP exceptions.

### Books — `server/books/`

- `scanner.py` — the only module that knows the archive layout (one subfolder per book,
  images as pages). `Catalog` has a **10-minute TTL cache** for listings; `force=True`
  re-scans (the client's Reload button sends `force=1`). Listings return a `signature`
  hash so the client skips rebuilds when nothing changed. `book_dir`/`page_path` enforce
  path-traversal safety (resolve + `is_relative_to`). Page scan skips undecodable images
  and groups non-conventional filenames into a trailing "extra" group.
- `naming.py` — page convention `^(\d+)_(\d+)-(.*)$` (e.g. `2_001-Page.jpg`) → group/order/name.
  Non-matching files become "extra" pages with a synthetic trailing group.
- `dimensions.py` — header-only dims via Pillow (`Image.open().size`, no decode), so
  listings stay fast over thousands of pages.
- `locations.py` — persistent base62 short-ID registry for `(book, page)` pairs, used in
  the URL path as share links (`/93050a0`). Persisted as JSON at
  `cache_dir/locations.json`; survives restarts; ID collision handled by incrementing.
  In-process thread-safe.

### Tiles — `server/tiles/`

Pipeline: **encoded disk cache → decoded-page RAM cache → mipmap level → crop →
resample → progressive-JPEG encode → store in disk cache** (`manager.py`).

- `geometry.py` — pure math, no I/O. Level 0 = 1:1; `max_level = ceil(log2(max(w,h)/tile_size))`
  is the whole-image-on-one-tile level. The client **must** agree with this formula and
  with `tile_size` (see `server/static/js/config.js` `TILE = 256`).
- `mipmap.py` — lazily builds each level by repeated halving (`resize_area`), cached for
  the page's lifetime; never skips source pixels.
- `resampler.py` — the only module aware of acceleration: OpenCL via `cv2.UMat` when
  available, else CPU `INTER_AREA`. Callers always use `resize_area` and never check the
  backend themselves.
- `decoder.py` — `cv2.imdecode` → BGR uint8 ndarray.
- `encoder.py` — progressive (SOF2) JPEG via OpenCV.
- `cache.py` — diskcache-backed LRU of encoded tiles, byte-limited; key includes the page
  mtime ("version"), so re-saved pages get fresh keys and stale tiles are never served.
- `page_cache.py` — RAM LRU of decoded mipmaps with a **background idle sweeper**:
  pages are dropped `page_idle_seconds` (default 10 s) after last access so RAM falls
  back near zero when idle.
- `locks.py` — `KeyedLock`: per-key `asyncio.Lock`s with refcounting, dedupes concurrent
  identical tile renders. Decoded-page dedupe is separate: per-page `threading.Lock`s in
  the manager, so one page is decoded once under concurrent tile storms.
- `router.py` — `GET /tiles/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg`. Tile responses
  carry `public, max-age=31536000, immutable` (content-addressed by version); the client
  relies on the browser HTTP cache for tile reuse.

### OCR — `server/ocr/`

- `engine.py` — the only module that talks to Tesseract (`pytesseract`, `--oem 3 --psm 3`).
  Downscales pages to `ocr_max_dim` (3000) first, filters words below
  `ocr_conf_threshold`, groups words into lines by Tesseract's (block, par, line) ids, and
  scales every box back to source-pixel coordinates.
- `service.py` — OCR is fully off the request path: a single daemon worker thread drains a
  `PriorityQueue`; interactive single-page requests jump ahead of bulk search prefetch
  (priority 0 vs 1). Results cached to disk as JSON keyed by page version
  (`cache_dir/{book}/{page}/{version}.json`), written atomically (tmp + `os.replace`).
  Search matches only against cached pages, submits the rest to the worker, and returns a
  `pending` count so the client polls for progressive results.
- `router.py` — OCR/search routes are `async` and wrap blocking work in
  `asyncio.to_thread` so the event loop (and tile serving) never blocks.

### Social — `server/social.py`, `server/pages.py`

Link previews for iMessage/Discord/Reddit, the SPA fallback page, and the
crawler-facing HTML.

- The viewer page is served for **every** path: `app.py`'s `NoCacheStaticFiles`
  serves real assets as files and hands everything else (the root, any
  `/93050a0` share link, unknown paths) to `social.spa_response`, which injects
  content and Open Graph meta tags into `index.html` and returns it with
  `no-cache`.
- `pages.py` builds the **server-rendered body fragment** per path (the crawlable
  content): the root renders a link to every book, a book's short-id path renders
  a link to every page (the crawl hub — one hop to anything), and a page's
  short-id path renders its OCR text plus prev/next/back links. Same markup for
  every client, JS or not (no cloaking). The fragment goes into `#seo-content`
  in the shell, visible only before the viewer boots (`html.no-js`); `main.js`
  intercepts clicks on those real links and routes them through the SPA
  (`nav.navigateToPath`), so users never see a full reload.
- OG meta: `og:title` varies by location (site name / book name / `Book • Page N`),
  `og:description` is a fixed site blurb, `og:image` is an absolute URL built from
  `request.base_url` (picks up the public hostname behind the Cloudflare tunnel).
- Page HTML uses `OCRService.get_page_ocr_cached` — a pure cache read that never
  enqueues or blocks, so crawler requests never trigger OCR work.
- `GET /og/{book}/{page}/{mtime}.jpg` renders the 1200×630 preview **from the
  existing tile pipeline**: picks the finest level whose image still covers the
  target, fetches that level's full grid via `TileService.get_tile` (reusing the
  encoded disk cache, decoded-page cache, and mipmaps), stitches, area-downscales,
  centers on white, and progressive-JPEG-encodes. Content-addressed by mtime →
  `immutable`; small in-process cache (≤32).

### HTTP API

| Endpoint | Notes |
|---|---|
| `GET /api/books[?force=1]` | book summaries + covers + `signature` |
| `GET /api/books/{book}/pages[?force=1]` | pages sorted by (group, order) + `signature` |
| `GET /api/books/{book}/pages/{page}/info` | dims, `max_level`, file size, sha256 |
| `GET /api/locations?book=&page=` | create/fetch short id → `{id}` |
| `GET /api/locations/{id}` | resolve short id → `{book, page}` |
| `GET /tiles/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg` | immutable progressive JPEG |
| `GET /og/{book}/{page}/{version}.jpg` | 1200×630 social preview, stitched from cached tiles |
| `GET /api/books/{book}/pages/{page}/ocr` | word/line boxes in source px |
| `GET /api/search?book=&q=&regex=` | matches + `pending` count |

Response schemas in `server/models.py` are the server↔client contract: listings include
each image's dimensions and `max_level` so the client computes tile geometry without
ever decoding an image.

## Client architecture (`server/static/js/`)

Plain ES modules, **no bundler** — imports are relative paths with explicit `.js`
extensions, loaded via `<script type="module" src="js/main.js">`. Edits to module imports
must keep the `.js` extension or the browser breaks.

`main.js` is the only module that imports everything and wires the object graph
(TileCache → TileQueue → scheduler → render → interaction → keys → ui → OCR overlay →
search → nameFilter → help), then restores the location from the launch path. No domain
logic there.

Data/control flow: launch path → `resolveLocation` → `nav.enterBook` → `fetchPages` →
`buildLayout` → `scheduler.reconcile` (decides what to fetch) → `queue.request` →
`fetchTile` → `cache.set` → `handleTile` → `reconcile` + `requestRender` → rAF frame →
`compositor.drawImageTiles` (coarse-to-fine with coverage culling).

- `state.js` — shared mutable singleton + tiny event bus (`on`/`emit`). Modules never
  import each other for data; they read/write `state` and subscribe to events. Event names
  are documented in a comment at the bottom of the file — new events should be added there.
- `config.js` — central tunables (TILE 256, MAX_INFLIGHT 6, SETTLE_MS 500,
  MAX_DISPLAYED_TILES 100, cache budgets, layout metrics, debug colors). UI-adjustable
  tile budget via live-exported `MAX_DISPLAYED_TILES`.
- `layout.js` — square-cell grid grouped by `group`. Images get a `stableKey`
  (`b:{book}` / `p:{book}:{page}`); existing objects are **reused across reloads** so
  cached tiles survive, but an object is recreated when its `version` (mtime) changes so
  stale tiles are dropped. Emits `images-changed` / `images-removed`.
- `tiles/tileCache.js` — decoded ImageBitmap LRU keyed `id:level:tx:ty`; the root tile
  (whole image, level `max_level`) is **pinned** so every image renders instantly on first
  appearance. `pruneImage` keeps only the pinned root when an image scrolls off-screen;
  `pruneToLevel` drops finer/coarser leftovers once the target level is complete.
- `tiles/queue.js` — priority fetch queue, `MAX_INFLIGHT` concurrent, nearest-to-cursor
  first. Network I/O only; no policy.
- `tiles/scheduler.js` — the policy. `reconcile()` rebuilds the desired tile set: pin root
  tiles, prune off-screen images, choose a **global coarsening offset K** across all
  visible images (same on-screen tile size everywhere, under the budget), then request the
  next refinement step per area (at most `PROGRESSIVE_STEP` levels finer than the finest
  cached ancestor). Zoom-in is budget-limited; zoom-out requests freely because each
  coarse tile arrival frees its fine descendants. `PREFETCH_NEIGHBORS` warms root tiles
  of adjacent images when idle.
- `tiles/levelSelect.js` — per-image 1:1 base level (never show a 256 px tile smaller than
  256 device px) plus the shared coarsening offset K.
- `compositor.js` — draws one image from cached tiles coarse→fine so finer tiles overpaint
  parents; recursive `tileCovered` culling; 0.5 px bleed on each tile to kill sub-pixel
  seams.
- `viewport.js` — pure scene↔device transform math (`sceneToDev`/`devToScene`, fit
  helpers). `render.js` — rAF loop, canvas DPR sizing, grey `#808080` background, labels,
  tile-debug overlay; delegates per-image drawing to the compositor.
- `interaction.js` — wheel zoom (Ctrl+wheel = pinch; the 0.036 vs 0.0012 factors matter,
  see the comment — trackpad pinches arrive as tiny ctrl+deltas), pointer pan, two-finger
  pinch, click/double-click/right-click; pan only on left button; marks interaction and
  reconciles after `SETTLE_MS` of quiet.
- `keys.js` — F fit, R reload, D tile debug, Space toggle 1:1/fit, arrows navigate,
  Escape/Backspace back. Shortcuts ignore INPUT/TEXTAREA focus.
- `nav.js` — showBooks / enterBook / reload (keeps the view), focusPage; syncs the URL
  path with the server-assigned short id via `history.replaceState` (never spams history).
- `ocr/overlay.js` — OCR text as transparent DOM spans over the canvas (selectable when
  Ctrl is held; Ctrl toggles pointer-events between overlay and canvas). One CSS transform
  on the scene element handles pan/zoom. OCR fetches lazily once an image is on screen and
  wide enough (`OCR_LOAD_MIN_PX`).
- `ocr/search.js` — dims non-matching areas (`SEARCH_DIM_ALPHA`), punches holes at hit
  boxes, re-polls while `pending > 0`. Rebuilds the layout with only matching pages.
- `nameFilter.js` — client-side substring filter on raw file name (matches the
  `2_123-Page.jpg` prefix too). **Mutually exclusive with OCR search**: starting one
  clears the other.

## Gotchas and non-obvious facts

- **Two index.html files**: root `index.html` (crop tool) vs `server/static/index.html`
  (viewer). Don't confuse them.
- **Share links are path-based, not hash-based**: `https://host/<short-id>` (e.g.
  `93050a0`). The server serves the same viewer page for every path (`app.py`
  `NoCacheStaticFiles` SPA fallback) and injects Open Graph tags (`social.py`) so
  iMessage/Discord/Reddit crawlers (which never run JS or see hashes) get previews.
  There is no `#` scheme and no legacy-hash support.
- **Restart the viewer via docker compose, never bare `docker restart`**: the deployed
  `bookviewer` container lives in the copyparty compose project
  (`~/Docker-Server/copyparty/docker-compose.yml`, same project as the Cloudflare tunnel
  reverse proxy). Restart it with `docker compose -f <that file> restart bookviewer`;
  plain `docker restart` detaches it from the project's networking and breaks the reverse
  proxy.
- **TILE size and max_level must stay in sync** between `server/config.py` (`tile_size`)
  and `server/static/js/config.js` (`TILE`). Changing one without the other breaks tiling.
- **Versions are mtimes**: tile URLs and OCR cache keys embed the page file's mtime.
  Re-saving a page changes the version → fresh tile URLs (immutable headers keep old
  bytes cached harmlessly) and fresh OCR.
- **Static is `no-cache`, tiles are `immutable`** — intentional split for dev.
- **The client relies on the browser HTTP cache** for tiles; there is no client-side
  IndexedDB layer.
- **Catalog listings are TTL-cached (10 min)**; the Reload button passes `force=1` and the
  client compares `signature` to avoid rebuilding when nothing changed.
- **OCR search is inherently partial**: the server returns matches found so far plus
  `pending`; the client polls every 2.5 s until pending hits 0. First search on a big book
  is slow by design.
- **Blocking work must go through `asyncio.to_thread`** in routers (tiles render, OCR).
  The event loop must stay free for tile serving.
- **Path traversal is refused** in `scanner.book_dir`/`page_path`; keep it that way —
  book/page ids come from URLs.
- **Error style**: raise `errors.NotFound`/`errors.BadRequest`; never leak HTTP details
  from lower layers. Client error handling treats any non-2xx as a thrown exception.
- **JS style**: JSDoc header on every module explaining its role; 2-space indent;
  semicolons; relative imports with `.js`. Python: 4-space indent, `from __future__ import
  annotations`, Sphinx-style docstrings (Args/Returns/Raises), `# noqa: BLE001` on
  intentional broad `except Exception`.
- **Debug helpers**: `window.dumpTiles()` and `window.__state` in the client; the D key /
  toolbar toggles the tile-debug overlay and stats bar.
- **Zoom is clamped** to scale ∈ [0.00005, 64].
- The local (gitignored) `.agents.md` contains environment/deployment notes (bun location,
  copyparty deploy of `book.html`) but is **partially stale**: it still describes the
  single-file `book.html` as the active viewer. The server app in `server/` and
  `server/static/` is the current development target.

## Conventions

- Commit style (match recent history): imperative, lowercase, no trailing period; newer
  commits dropped the historical `Book viewer: ` prefix.
- Never push to the remote (`origin` → `github.com/KunoDLK/photo-crop.git`) unless asked.
- `.agents.md` is gitignored — never commit it.
