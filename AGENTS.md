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
  opencv-contrib-python-headless, numpy, pillow, diskcache, rapidocr + onnxruntime.
- **Docker**: `docker build -f server/Dockerfile .` (runs `python -m server.main`;
  installs the OpenCL ICD loader; NVIDIA OpenCL is
  injected at runtime with `--gpus all`, otherwise it transparently falls back to CPU).
- **JS syntax checks**: `node` is NOT installed. Use `bun` (`/home/kuno/.bin/bun`).
  The viewer is plain browser ES modules, so a quick import/syntax smoke test is
  `bun build server/static/js/main.js --outdir /tmp/check --target browser`.
- **No tests, no linter config, no Makefile, no pyproject.toml** exist. There is no test
  suite to run.

## Server architecture (`server/`)

FastAPI app assembled in `server/app.py` `create_app()`: services are constructed once and
exposed on `app.state` (`.settings`, `.tiles`, `.ocr`, `.locations`, `.catalog`, `.rights`,
`.auth`, `.region`, `.policy`); routers fetch them from `request.app.state` — never
construct their own. Static viewer is mounted at `/` with `no-cache` headers (dev-friendly;
tiles are separate and immutable).

### Config — `server/config.py`

Single `Settings` (pydantic-settings) reads env vars (`.env` also supported); the only
module that knows env names. Defaults target `/archive/Library` (books root) and
`/archive/cache`. Key knobs: `ARCHIVE_ROOT`, `CACHE_DIR`, `CACHE_GB` (encoded-tile disk
budget), `PAGE_CACHE_BYTES` (decoded-page RAM budget), `PAGE_IDLE_SECONDS` (default 10),
`TILE_SIZE` (256), `JPEG_QUALITY`/`JPEG_PROGRESSIVE`, `OPENCL`, `OCR_*` (cache dir, max dim
0 = no downscale, lang, conf threshold 25), `RIGHTS_DB_PATH` + `ARCHIVE_USERNAME`/`ARCHIVE_PASSWORD` +
`SESSION_SECRET`/`SESSION_COOKIE_SECURE`/`LOGIN_RATE_LIMIT` + `DEFAULT_REGION`/
`DEV_REGION_HEADER` + `BLUR_STRENGTH`/`BLUR_DARKNESS` (rights/auth/admin; see the Rights
section), `HOST`, `PORT`.

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
  Keys carry a `t/` vs `x<gen>/` prefix for real vs blurred tiles (the generation bumps
  whenever blur rendering changes, so old blur bytes are never served without a manual
  wipe); the router resolves the policy first and never crosses variants, so a real tile
  cached from an owner's visit can never leak to an anonymous viewer.
- `blur.py` — blur rendering for restricted pages: the whole page is blurred
  once, at a capped resolution (`blur_levels_from_coarsest` levels in from the
  coarsest pyramid level), and every blur tile at any zoom level is a crop of
  that single plane — so adjacent tiles are pixel-consistent with no seams,
  and fine blur tiles are cheap upscales. The sigma is scaled so the
  source-space blur stays ≈ 4 × `blur_strength`. No darkening — the client
  overlays its own dark banner for the region text.
- `page_cache.py` — RAM LRU of decoded mipmaps with a **background idle sweeper**:
  pages are dropped `page_idle_seconds` (default 10 s) after last access so RAM falls
  back near zero when idle.
- `locks.py` — `KeyedLock`: per-key `asyncio.Lock`s with refcounting, dedupes concurrent
  identical tile renders. Decoded-page dedupe is separate: per-page `threading.Lock`s in
  the manager, so one page is decoded once under concurrent tile storms.
- `router.py` — **one route per access variant** so every tile URL is served
  identically to everyone who may cache it: `GET /rt/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg`
  serves the real tile (404 unless access is `full`) and `GET /bx/...` serves
  the blurred tile (404 unless access is exactly `blurred`); `nonexistent` →
  404 both (indistinguishable from a missing page). Cache headers: blur tiles
  are always `public, max-age=31536000, immutable` (region-independent bytes);
  real tiles are `public, immutable` only when the page is **not** region-locked
  (open to every requester, e.g. `public` default with no governing rule) and
  `private, immutable` otherwise — a shared cache (Cloudflare edge, browsers)
  must never hold bytes one requester is entitled to and another is not, and
  the region decision happens on the origin at request time. The client picks
  the variant from each image's resolved `access` (`tileUrl(..., blurred)`).

### OCR — `server/ocr/`

- `engine.py` — the only module that talks to an OCR engine: RapidOCR (PaddleOCR
  PP-OCRv4 models via ONNX Runtime, wheel-bundled, no runtime downloads). Its DBNet
  detector handles freeform layouts far better than Tesseract's page segmentation.
  RapidOCR recognizes whole lines; each line becomes an OCR line and its text is split
  into words with proportionally interpolated boxes so word search highlighting keeps
  working. Filters words below `ocr_conf_threshold`, and rescales every box back into
  source-pixel coordinates when the optional `ocr_max_dim` downscale is enabled.
- `service.py` — OCR is fully off the request path: a single daemon worker thread drains a
  `PriorityQueue`; interactive single-page requests jump ahead of bulk search prefetch
  (priority 0 vs 1). Results cached to disk as JSON keyed by page version
  (`cache_dir/{book}/{page}/{version}.json`), written atomically (tmp + `os.replace`).
  Search matches only against cached pages, submits the rest to the worker, and returns a
  `pending` count so the client polls for progressive results.
- `router.py` — OCR/search routes are `async` and wrap blocking work in
  `asyncio.to_thread` so the event loop (and tile serving) never blocks.

### Rights + auth + admin — `server/rights/`, `server/auth/`, `server/admin/` (RightsUpdate.md)

Access model (fail-closed defaults): unknown book = `private` (invisible to
everyone without a grant), page with no rights row = `blurred` (blurred tile,
never real content), owner always wins, a granted account sees its granted
books in full, region/date rules apply to everything else.

- `rights/store.py` — SQLite (`cache_dir/rights.db`, full schema created on
  first boot): editors (death year is the access key), rights_holders, books
  (visibility, editor, holder, publication year), page_rights (the allow-rule
  whitelist: exact page → book default → blurred, a per-page `copyright_kind` of
  `editor`/`holder`/`ad`, plus a per-page `default_access` of `block` (fail
  closed) or `public` — the owner's own images, open everywhere with no
  governing rule), page_editors (many-to-many page → editors; UK/EU terms run
  from the LAST editor's death), users (pbkdf2), user_grants.
  One connection + a lock; all CRUD lives here.
- `rights/geo.py` — `RegionDetector`: policy zone from the `CF-IPCountry` header
  (`US`→us, `GB`/`IE`→uk, EU→eu, else `unknown`), `DEFAULT_REGION` fallback for
  dev, per-IP TTL cache (bypassed by the dev-only `X-Test-Region` header when
  `DEV_REGION_HEADER=true`). Unknown regions fail closed.
- `rights/rules.py` — `pd_year(editors, kind, zone, publication_year)`, per
  copyright kind: `editor` → uk/eu = LAST editor's death year + 70, us =
  publication year ≤ 1929 → +95; `holder` (rights holder/publisher) → uk/eu =
  publication + 70, us = publication + 95; `ad` (advertisement, not covered by
  the notice) → publication + 28 in every zone; unknown → never.
- `rights/policy.py` — `Policy.resolve(viewer, book, page, zone, today)` →
  `full` / `blurred` (+`until` "1 Jan YYYY") / `nonexistent`; recomputed from
  today's date every request, so pages flip to full automatically on their PD
  date. `resolve_pages` does one DB pass per book. Resolution: owner → grant →
  book visibility → governing rule (the page's own copyright kind + editors,
  else the book's default editor; region/date rules) → the page's
  `default_access` (`public` = open everywhere, `block` = blurred). Each result
  carries `region_locked`: True unless the page is `full` for an anonymous
  viewer in every zone — the only case where real tiles may be cached publicly.
- `auth/service.py` — owner login from env (`ARCHIVE_USERNAME`/`ARCHIVE_PASSWORD`,
  `hmac.compare_digest`); account login from pbkdf2 hashes in the DB (stdlib
  only). Stateless HMAC-signed session cookie (`bv_session`, httpOnly, Secure,
  SameSite=Lax, 30-day); signing secret auto-persisted to `cache_dir/secret` so
  sessions survive restarts. Per-IP failure rate limit (`login_rate_limit`,
  5-min window). Exposes the `Viewer` type (owner/account/anonymous, with
  `grants`), the `current_viewer` FastAPI dependency, and stateless per-session
  CSRF tokens (`csrf_token`/`verify_csrf`) for the admin forms.
- `auth/router.py` — `POST /api/login`, `POST /api/logout`, `GET /api/me`
  (`{authenticated, username, is_owner, grants}`). pbkdf2 runs in the thread pool.
- `admin/` — server-rendered HTML CRUD at `/admin` (no build step, no required
  JS): books (flip public/private, editor, holder, year), editors, rights
  holders, per-book page-rights screens (default editor / bulk / per-page
  override: multi-editor sets via Ctrl-click selects, the per-page
  `copyright_kind` editor/holder/ad, `default_access` block↔public flags, and a
  bulk "set for all pages" action), accounts (create, reset password, grants, delete). Owner session only
  (Cloudflare Access guards the network layer); every POST carries the session
  CSRF token; anonymous GETs render the login form, mutations 401.
- `scripts/import_rights.py` — CSV bulk seed (`python -m server.scripts.import_rights
  --db … --editors e.csv --books b.csv --pages p.csv`; upserts, names matched
  case-insensitively and created when missing).
- Config: `rights_db_path` (default `cache_dir/rights.db`), `archive_username`,
  `archive_password` (empty disables owner login), `session_secret` (empty =
  auto-persist), `session_cookie_secure` (set `false` for plain-http local dev),
  `login_rate_limit`, `default_region`, `dev_region_header`, `blur_strength`,
  Requirements gained `python-multipart` (admin forms).

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
- **Access gating**: every content route resolves the policy for the requester's
  session + region first. `GET /og/...` serves the real preview only for `full`
  access; `blurred` (and `nonexistent`) pages get **no preview image at all** —
  the OG meta carries no `og:image`/`twitter:image`, so region-locked shares
  render as a text-only card and the real image can never leak. SEO fragments:
  public books render normally, blurred pages keep structure but **no OCR
  text**, private locations render an empty fragment with
  `X-Robots-Tag: noindex, nofollow`. The sitemap lists only public books and
  their pages.
- `GET /og/{book}/{page}/{mtime}.jpg` renders the 1200×630 preview **from the
  existing tile pipeline**: picks the finest level whose image still covers the
  target, fetches that level's full grid via `TileService.get_tile` (reusing the
  encoded disk cache, decoded-page cache, and mipmaps), stitches, area-downscales,
  centers on white, and progressive-JPEG-encodes. Content-addressed by mtime →
  `immutable`; small in-process cache (≤32, keyed by status too).

### HTTP API

| Endpoint | Notes |
|---|---|
| `GET /api/books[?force=1]` | visible books (private hidden without a grant) + `signature` + per-book `visibility` + cover `access` |
| `GET /api/books/{book}/pages[?force=1]` | pages sorted by (group, order) + `signature`; each page carries its resolved `access` |
| `GET /api/books/{book}/pages/{page}/info` | dims, `max_level`, file size, sha256, resolved `access` |
| `GET /api/locations?book=&page=` | create/fetch short id → `{id}` |
| `GET /api/locations/{id}` | resolve short id → `{book, page}` |
| `GET /rt/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg` | immutable real progressive JPEG — `full` access only |
| `GET /bx/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg` | immutable blurred progressive JPEG — `blurred` access only |
| `GET /og/{book}/{page}/{version}.jpg` | 1200×630 real social preview (full access only; region-locked pages send no image) |
| `GET /api/books/{book}/pages/{page}/ocr` | word/line boxes in source px — `full` access only, else 404 |
| `GET /api/search?book=&q=&regex=` | matches (filtered to fully-visible pages) + `pending` count |
| `GET /api/qr?url=` | PNG QR code (H error correction) with the brand "K" logo centred over it, for the Share panel |
| `POST /api/login` | owner/account login → session cookie (401 bad creds, 429 rate-limited) |
| `POST /api/logout` | clears the session cookie |
| `GET /api/me` | `{authenticated, username, is_owner, grants}` for the current session |
| `GET /admin…` / `POST /admin…` | owner-only server-rendered CRUD (CSRF-protected forms) |

Response schemas in `server/models.py` are the server↔client contract: listings include
each image's dimensions and `max_level` so the client computes tile geometry without
ever decoding an image.

## Client architecture (`server/static/js/`)

Plain ES modules, **no bundler** — imports are relative paths with explicit `.js`
extensions, loaded via `<script type="module" src="js/main.js">`. Edits to module imports
must keep the `.js` extension or the browser breaks.

`main.js` is the only module that imports everything and wires the object graph
(TileCache → TileQueue → scheduler → render → interaction → keys → ui → OCR overlay →
search → nameFilter → help → share → login → access), then restores the location from
the launch path. No domain logic there.

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
- `share.js` — Share button opens a centred panel with the current URL's QR code
  (`/api/qr`) and copies the URL to the clipboard; a green tick marks a completed
  clipboard write. Dismissed by the Okay button or Enter/Escape (keys.js routes
  those while the panel is open).
- `access.js` — access UX: boot-time `/api/me` fetch → `state.viewer`; renders
  per-image DOM elements inside the OCR scene (Private badges, and the
  "Unavailable in your region until …" text, shown only for blurred pages
  zoomed in near page size — the dark tint itself is canvas-painted by
  `render.js`, so hundreds of unavailable pages cost nothing at overview zoom);
  top banner shows the sign-in status or a region-unavailable count (it
  auto-slides away after a few seconds and returns on any content change).
  Listings carry per-page `access` and per-book `visibility`, so the client
  never resolves policy itself.
- `login.js` — toolbar lock button opens a modal: username/password form for
  anonymous viewers, "Signed in as … / Log out" for authenticated ones. Login
  updates `state.viewer` (event `auth-changed`) and reloads the current
  location so private books appear. Enter submits, Escape dismisses (keys.js).
- `fullscreen.js` — two paths to reclaimed screen space. Desktop/iPad: the
  Fullscreen API via a toolbar button (hidden where unsupported, e.g. iPhone
  Safari; Shift+F also toggles it). iPhone Safari/Chrome: the browser bars can
  only collapse via a real downward scroll of the document, so a
  `visualViewport` watcher shows a fixed "swipe down" hint while the bars are
  open in landscape and temporarily unlocks the document (a hidden spacer adds
  scroll room; the viewer is fully `position: fixed`, so the scroll is
  invisible) — the swipe is then a genuine document scroll that collapses the
  bars, and the watcher locks the page again and flips the app into
  "immersive" mode (`html.immersive`): on coarse-pointer devices the
  toolbar/status bar tuck away after a pan/zoom burst and a touch near the top
  edge brings them back. On touch devices /
  narrow windows the canvas is full-bleed (`#left` fixed, inset 0) and the
  chrome floats on top of it: back/title/☰ become translucent safe-area-aware
  pills, and the banner + status line become floating pills — content renders
  right around the dynamic island. `viewport-fit=cover` +
  `env(safe-area-inset-*)` paddings keep content clear of the notch/home
  indicator when the chrome is gone.

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
