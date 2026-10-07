# Mosaic proxy generation status + client warm-front-door (design)

> Implements: when a mosaic's proxy chains are not yet fully generated, the
> server reports that state together with generation progress on every channel
> that touches the page, and the client holds off sending tile requests while
> showing the image's warming progress as a percentage.

Status: implemented (branch `mosaic-wip`).

Implementation touches: `server/tiles/sqlite_cache.py` (`count_family_chains`),
`server/models.py` (`ProxyStatus`), `server/sources/mosaic.py` (`proxy_state`,
`ensure_warming`, active-build tracking, listing payloads),
`server/sources/router.py` (`GET /api/mosaic/{book}/proxy` + `X-Mosaic-*` tile
headers), and the client modules `proxy.js`, `api/proxy.js`,
`tiles/scheduler.js` (`proxyBlocked`), `render.js`, `layout.js`, `nav.js`,
`config.js`, `main.js`.

---

## 1. Problem

A wafer-scale mosaic (hundreds of cells, coarse-band overview) only renders
its overview tiles from per-cell proxy chains. When those chains are missing —
cold cache, no boot prewarm, or chains evicted — the first viewer that opens
the book pays the whole build cost. Worse, the client's scheduler fires the
root request **and** a wave of refinement requests at once, so several
concurrent cold renders race the same missing chains (serialized only by
per-cell decode locks). The user sees a frozen placeholder with no indication
of how long the build will take or how far along it is.

Today this is the documented cold-first-view budget (rule O3 in
`wafer-mosaic-scaling.md`): N × (one decode + chain), paid once per version.

## 2. Goals

1. The server tells a client, wherever it is looking, whether a mosaic page's
   proxy chains are fully built and, if not, how far the build has progressed.
2. The client, upon learning proxies are missing, holds off further tile
   requests for that image (no thundering herd against a cold store) and
   shows a live `warming NN%` readout on the image.
3. When generation completes, the client resumes tile requests automatically
   and the image fills in normally.
4. Zero behavior change for archive pages, fractal sources, proxy-disabled
   mosaics, and old clients — everything falls back to today's on-demand cold
   renders.

## 3. Server changes

### 3.1 `server/tiles/sqlite_cache.py`

`TileCache.count_family_chains(ns, book, page, version) -> int`:

```
SELECT COUNT(DISTINCT chain_id) FROM proxies
WHERE chain_id LIKE 'proxy/<ns>/<book>/<page>/<version>/%' ESCAPE '\'
```

- Chain ids have the fixed shape `proxy/<ns>/<book>/<page>/<version>/<cell>`
  (written by `put_chain`, deleted whole by `delete_chain`), so every distinct
  id in a family is one complete, usable chain. The count is the readiness
  numerator.
- A constant-prefix `LIKE` runs as a range scan over the existing
  `idx_proxies_chain (chain_id)` index — **no new table/index, no schema
  bump, no cache wipe**.

### 3.2 `server/models.py`

`ProxyStatus` pydantic model:

- `book`, `page`, `version` — which page family the state is for
  (version = manifest version, mirroring page `mtime`).
- `enabled: bool` — proxies are on for this source/book.
- `ready: bool` — every cell that needs a chain has one.
- `ready_cells`, `total_cells` — chains in the store vs. cells whose proxy
  band is non-empty (the same rule `prewarm()` skips cells on).
- `percent: int` — `round(100 * ready_cells / total_cells)` (100 when
  `total_cells == 0`).
- `generating: bool` — a chain build is in progress right now.

Optional `proxy: ProxyStatus | None = None` on `CoverInfo`, `PageInfo`, and
`ImageInfo`. Absent (default) for archive books and non-mosaic providers, so
the client contract stays backward compatible.

### 3.3 `server/sources/mosaic.py` — `MosaicSource`

- `proxy_state(fresh=False)` — assembles the `ProxyStatus`. `ready_cells`
  comes from `store.count_family_chains(...)` for the page's family; cached
  ~1 s and invalidated on manifest version change so per-tile response headers
  stay cheap (listings and the poll endpoint pass `fresh=True`).
- `ensure_warming()` — when chains are missing and no sweep thread is running,
  spawns one daemon thread running the existing `prewarm()` sweep; guarded so
  concurrent callers never double-start.
- Chain-build accounting: the build section of `_ensure_chain` (under the cell
  lock) increments/decrements `_active_builds`, so `generating` is true both
  while a sweep runs and while any render-driven cold build is running.
- `proxy=` attached to `list_books()`/`pages()`/`image_info()` records when
  proxies are enabled.

No change to the render path: a tile request whose chains are missing still
builds them on demand under the cell locks, so requests already in flight
during a warm complete correctly.

### 3.4 `server/sources/router.py`

- `GET /api/mosaic/{book}/proxy[?page=]` → `ProxyStatus`. 404 for a book whose
  source has no `proxy_state`. `page` defaults to the book's cover page (a
  mosaic book may serve several). When the page is not ready the endpoint calls
  `ensure_warming(page)` before returning, so the first poll both starts
  generation and reports it.
- `/pv/...` tile responses for a proxy-enabled mosaic carry, on every
  response, `X-Mosaic-Ready: 1|0` and `X-Mosaic-Progress: <0-100>` (from the
  same ~1 s-cached state, so no per-request store query).

No 503 / empty-body refusal on tile requests: a warm sweep and an on-demand
render both build missing chains under the same per-cell locks, so already
in-flight requests still succeed. Load reduction comes from client gating.

## 4. Client changes

### 4.0 Multi-page mosaics

A manifest may declare a `pages` list: each page is its own canvas and is
served as a page of the one book (`pages()`, `image_info()`, `render_tile` and
the proxy/`X-Mosaic-*` state are all keyed by page id). Warming and proxy
progress are per page, so the client's poller keys on `(book, page)`. The
legacy single-canvas manifest stays one page with id `mosaic`, and each page's
version is recomputed from the manifest + cell source mtimes on load, so a
re-saved cell or an edited manifest is picked up on the next Reload.

### 4.1 Listings → layout

- `nav.js` passes `proxy: b.cover.proxy` / `p.proxy` into layout items.
- `layout.js` stores `im.proxy` on reused and new image objects.

### 4.2 Tile-request gating (`tiles/scheduler.js`)

`proxyBlocked(im)` = `im.proxy && im.proxy.enabled && !im.proxy.ready`. While
blocked, `ensureRootTile`, `nextStepTiles`, and `prefetchNeighbors` request
nothing for the image (including deep-zoom tiles — deliberately all-or-nothing
per image). Archive pages, fractal sources, ready mosaics, and servers without
the feature behave exactly as before.

### 4.3 `static/js/proxy.js` (+ `api/proxy.js`)

One poll for the current warming set (single `setInterval`,
`MOSAIC_PROXY_POLL_MS`, default 1500 ms):

- On `images-changed` / `images-removed` / `location-changed`, mark
  content-less warming images `status = "warming"` and (re)start the poll.
- Poll `GET /api/mosaic/<book>/proxy?page=<page>` for each warming
  `(book, page)`; write the fresh state into every matching `im.proxy`; request
  a render when the percentage moves.
- On `ready`: drop the poller, reset `status` from `warming` to `idle`, and
  call `scheduler.reconcile()` so the previously gated requests start.

Wired in `main.js`: `proxy.init({ reconcile: scheduler.reconcile,
requestRender: render.requestRender })`.

### 4.4 Progress display (`render.js`)

`drawPlaceholder()` renders `warming NN%` (from `im.proxy.percent`) instead of
`loading…` for `im.status === "warming"`, in the same dashed placeholder frame.

## 5. Config / knobs

None required. Poll cadence is `MOSAIC_PROXY_POLL_MS` in `config.js`;
`ensure_warming()` reuses the `mosaic_proxy_prewarm` sweep path as its thread
target and honors `mosaic_proxy_enabled`.

## 6. Behavior matrix

| Scenario | Server | Client |
|---|---|---|
| Archive page / fractal / proxy-disabled mosaic | no `proxy` in listings, no headers | normal tile fetching |
| Mosaic, all chains cached | `proxy.ready: true`, percent 100 | normal fetching; poller never starts |
| Mosaic, chains missing, nothing building | first poll starts a sweep; `generating: true` | `warming NN%`, no tile requests |
| Mosaic, build running | `generating: true`, percent climbs | polls update the label; still gated |
| Build completes | `ready: true` | poller drops, status → `idle`, `reconcile()` fires |
| Old client / no polling | tiles cold-build chains on demand | unchanged |

## 7. Verification (done)

1. `bun build server/static/js/main.js --outdir /tmp/check --target browser`
   bundles cleanly; `python -m compileall` on the changed server modules.
2. Source-level test: `proxy_state` reports not-ready/0 %, `ensure_warming`
   builds all chains, then ready/100 %; disabled sources report `ready=True`
   and no listing payload.
3. Router-level test: listing cover/page carry the state; the first
   `/api/mosaic/{book}/proxy` poll reports `generating: true`; a cold tile
   response carries `X-Mosaic-Ready: 0`; after warming the same tile response
   carries `X-Mosaic-Ready: 1`; a non-mosaic book 404s; fractal tiles get no
   `X-Mosaic-*` headers.
4. Live dev server (isolated cache, mosaic demo): progress polls climbed
   0 → 100 % over 11 cells, then a `/pv/...` tile returned `200` with
   `X-Mosaic-Ready: 1`, `X-Mosaic-Progress: 100`, `X-Tile-Cache: hit`.
5. Client (bun) test: a blocked image requests nothing (`ensureRootTile` /
   `nextStepTiles` stay empty), a warming image shows `warming` and its percent
   advances from polls, readiness resets it to `idle` and reconciles, and a
   ready-only layout never polls.

## 8. Out of scope

- Pushing progress (SSE/WebSocket) — polling is sufficient and fits the
  existing request model.
- Refusing/short-circuiting tile requests server-side during a warm.
- Deep-zoom-only unblocking while a mosaic is warming.
- Anything for archive pages or non-mosaic providers.
