# Wafer-scale mosaic: proxy pyramid + tiered cache eviction (design)

> Status: **implemented** on `mosaic-wip` (schema v2 in `server/tiles/sqlite_cache.py`,
> proxy engine in `server/sources/mosaic.py`, tiered eviction, config knobs
> `MOSAIC_PROXY_ENABLED`/`MOSAIC_PROXY_PREWARM`/`EVICT_*`, and the offline prewarmer
> `dev/wafer_mosaic.py --prewarm`, which also builds the 31x31 = 961-cell test
> fixture). P1 (SQLite store foundation) arrived via `origin/main` (PR #6). This
> document remains the reference for the rules the code implements.

Scope: make the mosaic source serve hundreds of very large source images
(100+ MP wafer scans) on one virtual canvas without ever holding more than a
few of them in RAM, and make first views of the whole canvas (and any
multi-cell zoom) fast instead of minutes. Backend only — the client, the HTTP
API, tile geometry, and the archive pipeline are unchanged.

---

## 1. High-level plan

The current mosaic render decodes every overlapping source **cell in full
resolution** (~300 MB each) for every requested tile, so a whole-canvas tile
over N cells costs N full decodes and gigabytes of RAM churn. The plan replaces
that with a **persistent per-cell proxy pyramid**, plus an eviction policy that
can actually keep hundreds of images' worth of cache healthy.

```
               ┌─────────────────────────── tiles table (as today, +numeric coords)
               │
cell JPEG ──►  full-res decode ──► RAM PageCache (deep band only, idle 10 s)
   │               │  (once per cell per version, per-cell lock)
   │               ▼
   │   successive INTER_AREA halving ──► proxy planes (only widths < TILE)
   │               │                            │
   │               ▼                            ▼
   │   (deep band: footprint ≥ TILE)   proxies table (chain per cell,
   │   render tiles by crop+resize     row per level, disk, atomic)
   ▼
render_tile: coarse-band tiles always draw from proxy planes;
             deep-band tiles always from the full-res decode
```

**Phases**

- **P1 — Merge the tile-store foundation.** Bring `Cache_Improvements`
  (SQLite tile store, background janitor, zoom-aware eviction) onto this line
  of work. `mosaic-wip` still runs the diskcache `cache.py`; every later step
  is a change to `sqlite_cache.py`.
- **P2 — Schema v2.** Add numeric tile coordinates, the `proxies` table, and
  chain/coverage columns (see §5). One wipe-based schema bump (existing
  policy, never migrate).
- **P3 — Proxy engine in the mosaic source.** Chain detection, one-decode
  all-at-once chain build, proxy-band render path (§3, §4). Client-visible
  behaviour identical, just faster.
- **P4 — Tiered eviction.** Age tiers (7 d / 10 min) layered over the existing
  zoom-first ordering; chain-aware proxy eviction (§6).
- **P5 — Prewarm (optional).** Ingest-time or startup background worker that
  builds chains so the first human viewer never pays the cold cost (§9).

---

## 2. Terms and constants

| Term | Meaning |
|---|---|
| cell | One source image placed on the mosaic canvas, width `W` px (square in practice; rules use `W`) |
| canvas | The virtual mosaic, `CW × CH` px, from the manifest |
| `TILE` | Output tile edge, 256 (must match client) |
| `M` | `max_level(canvas)` — whole canvas fits one tile |
| level `L` | 1 output px = `2**L` canvas px |
| footprint | `W / 2**L` — how wide one cell renders at level `L` |
| proxy band | Levels where a cell's footprint is `< TILE` (see G2) |
| deep band | Levels where a cell's footprint is `>= TILE` |
| plane | A cell downsampled by exactly `2**L` (one per proxy-band level) |
| chain | All of one cell's planes, one per proxy-band level |

Defaults: `EVICT_YOUNG_SECONDS = 600`, `EVICT_OLD_SECONDS = 7 * 86400`,
`ACCESS_REFRESH_SECONDS = 60` (existing), `EVICT_BATCH = 200` (existing),
low-water 95% of the byte budget (existing), janitor sweep 60 s (existing).

---

## 3. Geometry rules

- **G1 — Band membership.** For cell `c` at level `L`, the cell renders from a
  proxy plane iff `W_c < TILE * 2**L` (equivalently footprint `< 256` px).
  Otherwise it renders from the full-res decode. This is a pure function of
  the manifest — never a function of what happens to be cached.
- **G2 — The band is a suffix of levels.** Since `W_c / 2**M <= TILE`, there is
  a threshold `L*_c = min { L : W_c < TILE * 2**L }`; the proxy band is exactly
  `L*_c .. M`. (For a canvas that is a single cell sized exactly `TILE * 2**M`,
  the band is empty — that canvas behaves like an ordinary archive page.)
- **G3 — One plane per level.** The plane for level `L` is the whole cell
  downsampled by `2**L` (dimensions `ceil(W_c / 2**L)` on a side). Different
  levels use different planes; no plane serves two levels.
- **G4 — Coverage range.** The dependent tile set of plane `(c, L)` is the set
  of level-`L` grid tiles whose canvas rect intersects `c`'s rect. Tile
  columns/rows at `L`: `cols = ceil(CW / (TILE * 2**L))`, same for rows with
  `CH`. The tile-coordinate bounds of cell `c` at level `L`:
  `tx ∈ [floor(c.x / (TILE*2**L)), floor((c.x + W_c - 1) / (TILE*2**L))]`,
  analogously for `ty`. The *expected* count (see E5) is the size of that
  product range.
- **G5 — Square cells.** Wafers are square; rules use one width. For
  rectangular cells, band membership and chain widths use
  `max(cell_w, cell_h)` and planes keep both axes.

---

## 4. Proxy chain rules (build + render)

- **B1 — Chain build trigger.** A tile request whose render needs plane
  `(c, L)` finds it missing → the cell's chain is built first, then the tile
  renders. Chain build never runs on the request path without holding the
  cell's decode lock (B4).
- **B2 — All levels at once.** A chain build decodes the full-res cell JPEG
  **exactly once**, then produces every plane in the chain by successive
  `INTER_AREA` halving of that one decode: `W/2, W/4, ...` down to
  `ceil(W / 2**M)`. Only planes with width `< TILE` are stored; larger
  intermediates are computed and discarded. Total extra work beyond the decode
  is ≈ ⅓ of one full-res pass. Never build planes one level at a time — a
  later level would need another full decode, and the RAM LRU drops the decode
  after 10 s idle.
- **B3 — Plane encoding.** Planes are stored lossless (PNG) so the halving
  chain never compounds JPEG artefacts, and because a plane is itself decoded
  to build nothing further (tiles crop it directly).
- **B4 — Locking and dedupe.** Chain build for cell `c` holds the existing
  per-cell `threading.Lock` (`_decode_locks` in the mosaic source). Concurrent
  misses wait, then re-check the store before building. Exactly one chain
  build per `(cell, version)` ever (until evicted).
- **B5 — RAM reuse.** The full-res decode performed by a chain build is also
  inserted into the shared decoded-image `PageCache` (same key semantics as
  today's `_cell_image`), so the deep band and a subsequent chain rebuild both
  reuse it until the idle sweeper drops it.
- **B6 — No partial chains.** A chain is stored atomically: all planes of a
  cell inserted in one transaction (delete-old + insert-new), or none. A
  partial chain found at read time is treated as absent and rebuilt.
- **R1 — Render path is deterministic by level.** For each overlapping cell at
  level `L`: if `L` is in the cell's proxy band, draw from plane `(c, L)`;
  else draw from the full-res bitmap exactly as `_draw_cell` does today. The
  choice never depends on cache state, so a given tile key always maps to the
  same generation source and the same bytes.
- **R2 — No cross-path caching.** A proxy-band tile is never rendered from the
  full-res decode *and stored under its normal key*. If the plane is missing,
  B1 (chain build) runs first. The only acceptable full-res fallback is an
  uncached, non-stored render (availability hatch) — it must not write bytes
  under the proxy-band key.
- **R3 — Plane crop math.** The plane's pixel grid is origin-aligned to the
  cell's source (`plane px i` covers source `[i*2**L, (i+1)*2**L)`), so a
  plane blit mirrors `_draw_cell` with `scale = 2**L`; a cell origin not
  divisible by `2**L` leaves a ≤ 1 plane-px boundary, absorbed by the same
  clamp + final `INTER_AREA` resize step the full-res path already uses.
- **R4 — Coarse tile cost.** A whole-canvas tile at level `M` reads each
  cell's coarsest plane only: N tiny PNG decodes + blits (well under a second
  for 400 cells) instead of N full 300 MB JPEG decodes.

---

## 5. Storage rules (SQLite schema v2)

Same database file as the tiles (`cache.db`), one janitor, one byte budget.

- **S1 — `tiles` gains numeric coordinates.** Every tile row additionally
  stores `ns` (`t` / `x<gen>` / `p`), `book`, `page`, `version`, `level`,
  `tx`, `ty` as indexed columns (populated on `put`), so coverage queries are
  range lookups, not key-string `LIKE`. `key`, `value`, `creation_time`,
  `access_time`, `zoom` stay as today.
- **S2 — New `proxies` table**, one row per plane:

  ```sql
  CREATE TABLE proxies (
    key            TEXT PRIMARY KEY,   -- proxy/<ns>/<book>/<page>/<version>/<cell>/<L>
    chain_id       TEXT NOT NULL,      -- proxy/<ns>/<book>/<page>/<version>/<cell>
    version        INTEGER NOT NULL,
    level          INTEGER NOT NULL,
    width          INTEGER NOT NULL,   -- plane edge px
    value          BLOB NOT NULL,      -- lossless PNG
    tx0, tx1, ty0, ty1 INTEGER NOT NULL,  -- G4 dependent-tile range at `level`
    expected       INTEGER NOT NULL,   -- |tx0..tx1| x |ty0..ty1|
    creation_time  REAL NOT NULL,
    access_time    REAL NOT NULL
  );
  CREATE INDEX idx_proxies_chain ON proxies (chain_id);
  CREATE INDEX idx_proxies_evict  ON proxies (access_time);
  ```

  `tx0..ty1` and `expected` are pure manifest geometry, computed at chain
  build time (O(1) per plane), never by scanning tiles.
- **S3 — Byte accounting spans both tables.** Triggers on `tiles` *and*
  `proxies` maintain the single `bytes`/`rows` meta counters; one
  `size_limit`, one low-water mark, one janitor.
- **S4 — Chain is the eviction unit.** Eviction deletes whole chains (all rows
  of a `chain_id`) in one transaction, never single planes — partial chains
  would break B6 and reintroduce re-decodes. Plane rows exist for cheap
  per-level reads; lifecycle is per-chain.
- **S5 — Schema bumps wipe (unchanged policy).** Schema v2 replaces the file
  on first open of a new build (old tiles + proxies gone; regenerated lazily).

---

## 6. Cache eviction rules

The janitor keeps its mechanics: background thread, wake on over-budget
`put`, delete in batches of `EVICT_BATCH`, stop at the low-water mark, sweep
at least every 60 s, never delete on the request path. What changes is the
candidate ordering — age tiers added *above* the existing zoom-first order.

- **E1 — Tiers.** For a row, `age = now - access_time`.
  - Tier A (stale): `age > EVICT_OLD_SECONDS` (7 d)
  - Tier B (cold): `age > EVICT_YOUNG_SECONDS` (10 min)
  - Tier C (fresh): everything else
- **E2 — Total order.** Candidates are consumed in this order until the cache
  is at/below the low-water mark:
  1. Tier A tiles — `zoom DESC, access_time ASC` (deepest zoom first)
  2. Tier A proxy chains — redundant first (E5), then `access_time ASC`
  3. Tier B tiles — `zoom DESC, access_time ASC`
  4. Tier B proxy chains — redundant first, then `access_time ASC`
  5. Tier C tiles — `zoom DESC, access_time ASC`
  6. Tier C proxy chains — redundant first (last resort; only if still over)
- **E3 — Tiles before chains, within a tier.** A tile that was evicted can be
  regenerated cheaply from a surviving chain (plane decode); a chain that was
  evicted forces a full-res decode to come back. So within each tier, tiles
  always go first.
- **E4 — Redundant chains first.** A *redundant* chain (E5) is pure
  redundancy — dropping it costs nothing now. A *load-bearing* chain has
  uncached dependents and dropping it turns their next request into a full-res
  decode, so it is evicted last within its tier.
- **E5 — Redundancy test (DB-only, no geometry).** Chain `C` is redundant iff
  for every plane row of `C`:
  `SELECT COUNT(*) FROM tiles
    WHERE ns = ? AND book = ? AND page = ? AND version = ? AND level = ? AND
          tx BETWEEN tx0 AND tx1 AND ty BETWEEN ty0 AND ty1` equals `expected`.
  (Row keys are unique, so count == expected ⟺ every dependent tile exists.)
- **E6 — Proxy recency.** A chain's `access_time` is refreshed:
  (a) at chain build, and (b) whenever a tile is *generated* using one of its
  planes (the store write path, not cache hits). Once a chain's dependent
  tiles are all cached, nothing refreshes it → it ages through the tiers and
  becomes evictable exactly when the cache needs room, matching the "drop
  proxies nobody reads any more" requirement.
- **E7 — Young-tile floor.** Tiles younger than `EVICT_YOUNG_SECONDS` are only
  evicted after every older tile and chain in the cache (E2 order), so an
  active zoom session's just-written deep tiles survive budget overruns
  instead of evicting themselves (the current zoom-first churn).
- **E8 — Zoom semantics unchanged.** `zoom` keeps today's meaning: `0` = whole
  image on one tile (evicted last), larger = deeper/finer. Mosaic tile rows
  keep `tile_zoom = max_level - level`; archive pages and providers keep their
  existing conventions. Proxies carry no zoom — tier + redundancy order them.

---

## 7. Versioning and determinism

- **V1 — Version scoping unchanged.** Tile and proxy keys embed the manifest
  version (newest source mtime). Re-ingesting a cell bumps the version →
  fresh key families; old tiles *and* old chains are orphaned and reclaimed
  purely by the age tiers (A) — recency eviction is what finally removes
  superseded shallow tiles that zoom-first alone would keep forever.
- **V2 — Deterministic derivation.** Plane bytes are a pure function of the
  source file at its mtime and the halving recipe (B2), so chain rebuilds
  after eviction reproduce identical bytes. Combined with R1/R2, every cache
  key maps to exactly one byte stream over its whole life.
- **V3 — Signature/refresh.** `refresh()` and `signature` behaviour is
  unchanged (force reload re-reads the manifest). A new manifest version
  invalidates nothing explicitly — new keys, old bytes age out.

---

## 8. RAM and concurrency rules

- **M1 — Resident set stays tiny for the coarse band.** Proxy-band tiles never
  decode full-res cells, so browsing the overview or any multi-cell zoom
  touches only small planes; the 6 GiB decoded-page LRU is not thrashed by
  overview renders and does not evict co-resident archive pages.
- **M2 — Full-res decode happens only in the deep band** (footprint ≥ TILE) —
  one cell at a time — and reuses today's `PageCache` LRU + 10 s idle sweeper.
  Working set while deep-zooming one wafer: that cell (~300 MB) plus planes.
- **M3 — One chain build at a time per cell** (B4); chain builds of different
  cells may run concurrently but each holds its own decode lock — no global
  build storm, no decode stampede.
- **M4 — Chain builds go through `asyncio.to_thread`** exactly like tile
  renders today; the event loop never blocks.

---

## 9. Prewarm and operations (phase P5, optional)

- **O1 — Offline build tool** (extend the dev ingest tooling or a small
  script): for each cell of the current manifest, decode once and write its
  chain into `cache.db` through the same store API. Safe because chains are a
  pure function of (source file, version) (V2).
- **O2 — Background warm-up at boot** (server option): a low-priority worker
  builds chains for manifest cells not yet in the store, bounded to one or two
  in flight so startup and first requests are unaffected.
- **O3 — Cold-first-view budget.** Without prewarm, the first whole-canvas
  request pays N × (one decode + chain) — minutes for hundreds of cells,
  exactly once per version, then effectively free. Prewarm moves that cost to
  ingest/deploy where it belongs.
- **O4 — Warm front door** (status + progress): a client learns a page's chain
  readiness from its listing, polls `GET /api/mosaic/{book}/proxy`, and holds
  off tile requests for a not-ready page (no cold-render storm) while showing
  `warming NN%`; the first poll starts the O2-style sweep, and provider tile
  responses echo the state as `X-Mosaic-*` headers. See
  `docs/mosaic-proxy-progress.md`.

---

## 10. Config knobs (additions to `Settings`)

- `evict_young_seconds: float = 600`
- `evict_old_seconds: float = 604800`
- `mosaic_proxy_enabled: bool = True` (off = today's behaviour)
- `mosaic_proxy_prewarm: bool = False` (O2)
- (Band width is fixed at `< TILE` by G1 — no knob, keeps geometry sound.)

---

## 11. Out of scope / explicitly unchanged

- Tile geometry formulas, `max_level`, archive tile cache headers. (The O4
  front door adds the mosaic status endpoint and `X-Mosaic-*` provider-tile
  headers and small client changes — see `docs/mosaic-proxy-progress.md`.)
- Blur/rights pipeline (unrelated key spaces).
- Archive page pipeline (deep band of a mosaic page behaves like it already).
- Schema migrations (wipe-based by design); v2 bump costs one cache rebuild.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Proxy-band tile needs a chain that was evicted → one full decode reappears | Rare by E6 (chains age only when idle); bounded by B2 (rebuilds the whole chain, not one plane) |
| Coverage query cost in the janitor | Only run for chains actually in eviction tiers; per-plane range + `expected` with the numeric index (S1/S2); coarse band has few tiles |
| Determinism drift between proxy and full-res bytes | R1/R2: level-based rule, no cross-path storage |
| Schema v2 wipes the cache on upgrade | Accepted (existing policy); lazy rebuild + optional prewarm (O1/O2) |
| Non-power-of-two cells / odd offsets | G5 + R3: per-plane integer crop with the existing resize step |
