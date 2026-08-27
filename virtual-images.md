# Virtual images via a pluggable image-source hook

Tech demo for the tiling renderer: procedural images (a Mandelbrot fractal)
rendered tile-by-tile by the server with **no practical zoom limit**, wired in
through a general **image-source hook** rather than hard-coded into the books
or tiles routers.

The hook is the deliverable: an `ImageSource` interface that any image server
can implement to attach books/pages/tiles to a book location. The fractal
generator ships as one clean, isolated reference implementation. Nothing in
the archive, rights, or tile pipeline knows fractals exist.

---

## 1. Architecture: the hook seam

```
              ┌────────────────────────── server/sources/ ──────────────────────────┐
 books router │  base.py        ImageSource (ABC) + TileRequest + SourceRegistry    │
   (listings) │  service.py     SourceTileService: shared disk LRU + locks + encode │
 tiles router │  router.py      GET /pv/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg│
 (provider)   │  fractal/       FractalSource (the demo provider)                   │
              │    mandelbrot.py   pure NumPy renderer (no I/O, no HTTP, no FastAPI) │
              └──────────────────────────────────────────────────────────────────────┘
```

Two rules keep it tidy:

1. **Sources are the only seam.** `app.py` constructs the fractal source and
   hands it to a `SourceRegistry`; the books router and the provider tile
   router only ever talk to the registry. Adding a second image server later
   (an HTTP adapter, a photo CDN, another procedural generator) is: implement
   `ImageSource`, register it, done — zero changes to routers or the client.
2. **Sources know nothing about HTTP, rights, or caching.** They answer
   "which books/pages do I provide" and "render this tile" (a pure function).
   The shared service owns caching, concurrency dedupe, JPEG encoding, and
   cache headers. Domain errors come from `server/errors.py` (`NotFound`,
   `BadRequest`) — sources are not HTTP code.

Precedence: the registry is consulted **before** the archive catalog, so a
source's book ids shadow any real archive directory of the same name. Within
the registry, first registered source wins a disputed id. Both documented in
`base.py`.

---

## 2. `server/sources/base.py` — the contract

```python
@dataclass(frozen=True)
class TileRequest:
    book: str
    page: str
    version: int
    level: int
    tx: int
    ty: int
    tile_size: int


class ImageSource(ABC):
    """Pluggable book/page/tile source.

    Registered in app.py via SourceRegistry. The books router asks the
    registry for listings; the provider tile route asks it for tiles. A
    source owns its book ids: ``pages()`` returning a result for ``book``
    shadows the archive. Sources never see requests, sessions, or caches.
    """

    #: Stable URL/cache namespace slug (must not change between restarts).
    key: ClassVar[str] = "source"
    #: Cache-Control for this source's tiles (procedural bytes are public).
    cache_control: ClassVar[str] = "public, max-age=31536000, immutable"

    def list_books(self) -> list[BookSummary]:
        """Books to appear in the root listing (cover metadata included)."""
        return []

    def pages(self, book_id: str) -> tuple[str, list[PageInfo]] | None:
        """Return ``(signature, pages)`` if this source owns ``book_id``,
        else ``None`` (the catalog/rights path takes over)."""
        return None

    def image_info(self, book_id: str, page_id: str) -> ImageInfo | None:
        """Detailed metadata for one page this source owns, else ``None``."""
        return None

    @abstractmethod
    def render_tile(self, req: TileRequest) -> np.ndarray:
        """Render one ``tile_size`` square tile as a BGR uint8 array.

        Runs on a worker thread (never on the event loop). Pure function of
        the request: the same (book, page, version, level, tx, ty) must
        always produce the same bytes, so tiles cache immutably. Raise
        ``errors.NotFound`` / ``errors.BadRequest`` for unknown ids or
        out-of-range coordinates.
        """
        raise NotImplementedError

    @property
    def signature(self) -> str:
        """Change signature for this source's listings (ids + versions)."""
        return self.key
```

`SourceRegistry` is the only object routers touch:

```python
class SourceRegistry:
    def __init__(self, sources: list[ImageSource]) -> None: ...
    def source_for_book(self, book_id: str) -> ImageSource | None: ...
    def list_books(self) -> list[BookSummary]: ...     # in order, deduped by id
    def pages(self, book_id: str) -> tuple[str, list[PageInfo]] | None: ...
    def image_info(self, book_id: str, page_id: str) -> ImageInfo | None: ...
    @property
    def signature(self) -> str: ...   # combined provider signatures
```

## 3. Listings hook — `server/books/router.py`

Three small, clearly-marked hooks; everything else stays untouched:

- **`list_books_endpoint`**: after the rights-filtered catalog books, append
  `registry.list_books()` (each provider book carries `visibility: "public"`
  and its cover's resolved `access`, since providers own their access story —
  the archive rights policy never sees provider books). Combined signature:
  `_sig([catalog_sig, registry.signature])`.
- **`list_pages_endpoint`** — first line:
  ```python
  result = request.app.state.sources.pages(book_id)
  if result is not None:
      sig, pages = result
      return PagesResponse(book=book_id, pages=pages, signature=sig)
  ```
  This precedes the `_granted`/`_is_public` checks (provider books have no
  rights rows).
- **`image_info_endpoint`**: same short-circuit via
  `registry.image_info(book_id, page_id)`.

OCR/search routes need no change: provider pages simply have no OCR file
(404) and no search hits; the client skips OCR for provider images (§6).

## 4. Provider tiles — `server/sources/service.py` + `router.py`

`SourceTileService` mirrors `TileService`'s seams, minus decode/mipmap:

```python
class SourceTileService:
    def __init__(self, settings, registry):
        self.tiles = encoded_cache.TileCache(settings.cache_dir, settings.cache_bytes)
        self.locks = KeyedLock()
        self.registry = registry

    @staticmethod
    def key(source_key, book, page, version, level, tx, ty):
        return f"p/{source_key}/{book}/{page}/{version}/{level}/{tx}/{ty}"

    async def get_tile(self, book, page, version, level, tx, ty):
        source = self.registry.source_for_book(book)      # None -> errors.NotFound
        key = self.key(source.key, book, page, version, level, tx, ty)
        cached = self.tiles.get(key)
        if cached is not None: return cached, True
        async with self.locks.acquire(key):               # dedupe identical renders
            cached = self.tiles.get(key)
            if cached is not None: return cached, True
            req = TileRequest(book, page, version, level, tx, ty, self.settings.tile_size)
            bgr = await asyncio.to_thread(source.render_tile, req)
            data = encoder.encode_progressive_jpeg(
                bgr, self.settings.jpeg_quality, self.settings.jpeg_progressive)
            self.tiles.put(key, data)
            return data, False
```

- **Same disk LRU** as real tiles (`v/` becomes `p/<source_key>/`), so all
  tiles share one byte budget. `version` namespaces the cache exactly like a
  page mtime: change fractal parameters → bump the version → fresh URLs, old
  bytes LRU-evicted.
- **No decoded-page RAM cache**: rendered tiles encode straight to JPEG and
  are freed; RAM cost is one 256×256 BGR buffer per in-flight tile.
- **`router.py`**:
  ```
  GET /pv/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg
  ```
  No viewer/policy dependency; responds with the source's `cache_control`
  and `X-Tile-Cache`. Negative levels parse fine in the path; the source
  rejects out-of-range with `BadRequest`, unknown books with `NotFound`.
  This generic route is what makes "hook an image server to a book location"
  work for any future source with zero new endpoints.

## 5. The fractal provider — `server/sources/fractal/`

### `mandelbrot.py` — pure renderer (no I/O, no FastAPI, no settings)

```python
def render_tile(img: FractalParams, level: int, tx: int, ty: int,
                tile_size: int) -> np.ndarray:
    """Escape-time Mandelbrot for one tile -> BGR uint8 (tile_size x tile_size)."""
    # The region is rectangular (3.5 x 2.5): each axis gets its own step.
    span_x = (img.re_max - img.re_min) / (2 ** -level)
    span_y = (img.im_max - img.im_min) / (2 ** -level)
    dc_x, dc_y = span_x / tile_size, span_y / tile_size
    cx0 = img.re_min + tx * span_x
    cy0 = img.im_min + ty * span_y
    xs = np.linspace(cx0, cx0 + (tile_size - 1) * dc_x, tile_size)
    ys = np.linspace(cy0 + (tile_size - 1) * dc_y, cy0, tile_size)  # top -> bottom
    # Pixel (row, col) samples (x=xs[col], y=ys[row]): x left-to-right,
    # y top-to-bottom (half-open tiles: no overlap or gap between neighbours).
    c = xs[None, :] + 1j * ys[:, None]
    z = np.zeros_like(c)
    max_iter = min(img.max_iter_base + img.max_iter_per_level * -level,
                   img.max_iter_cap)
    m = np.full(c.shape, max_iter, dtype=np.float64)   # first-divergence index
    done = np.zeros(c.shape, dtype=bool)
    for i in range(max_iter):
        z[~done] = z[~done] ** 2 + c[~done]
        div = ~done & (z.real ** 2 + z.imag ** 2 > 4.0)
        if div.any():
            m[div] = i + 1 - np.log2(np.log2(np.abs(z[div])))   # smooth coloring
            done |= div
        if done.all():
            break
    return palette[(np.clip(m / max_iter, 0, 1) * 255).astype(np.uint8)]  # -> BGR
```

- Tiles are a **pure function of their complex rect** — independent at any
  depth, no mipmaps, no shared state (this is what makes "any arbitrary tile,
  no limit" possible). The masked loop stops iterating diverged pixels (the
  full-array alternative would overflow them to NaN).
- Iteration budget grows with depth (`300 + 20·depth`, capped 1500; the cap
  bounds the worst case: a fully interior tile runs every pixel to the cap).
  Typical cold tile: tens of ms; deep boundary tiles ~0.5 s (one-time, then
  disk-cached forever); every repeat visit is a cache hit.
- Levels are independent uniform grids of the same region: adjacent tiles of
  one level share edges exactly (half-open sampling), and consecutive levels
  are offset by half a sample — the standard Deep Zoom behavior, invisible
  when a finer level replaces a coarser one during zoom.

### `__init__.py` — `FractalSource(ImageSource)`

```python
class FractalSource(ImageSource):
    key = "fractal"
    BOOK_ID = "fractals"

    def list_books(self):
        return [BookSummary(id=self.BOOK_ID, name="Fractals",
                cover=CoverInfo(page_id="mandelbrot", width=256, height=256,
                                max_level=0, mtime=0,
                                access=AccessInfo(status="full", zone="",
                                                  region_locked=False)),
                visibility="public")]

    def pages(self, book_id):
        if book_id != self.BOOK_ID: return None
        return (self.signature, [PageInfo(page_id="mandelbrot", name="Mandelbrot",
                group=1, order="1", width=256, height=256, max_level=0,
                mtime=0, access=..., source=self.key)])

    def image_info(self, book_id, page_id): ...   # dims, file_size=0, hash, access

    def render_tile(self, req):
        if req.book != self.BOOK_ID: raise NotFound(...)
        if not (self.min_level <= req.level <= 0): raise BadRequest(...)
        grid = 2 ** -req.level
        if not (0 <= req.tx < grid and 0 <= req.ty < grid): raise BadRequest(...)
        return mandelbrot.render_tile(self.params, req.level, req.tx, req.ty,
                                      req.tile_size)
```

`FractalParams` (region `[-2.5, 1] × [-1.25, 1.25]`, iteration budget,
`version`) lives in this module too; bumping `version` is the cache
invalidation switch.

## 6. Level-space design (unchanged from the agreed design)

The "reverse levels" convention that makes the pyramid bottomless:

| zoom | your Lk | our level | grid | tile covers |
|---|---|---|---|---|
| whole image | L0 | `0` | 1×1 | full span |
| 2× | L1 | `-1` | 2×2 | span/2 |
| k | Lk | `-k` | 2^k × 2^k | span/2^k |

The minus sign keeps every invariant (`higher = coarser`, root at `maxLevel`,
children at `level−1`), so the scheduler/compositor/caches work unchanged.
The client sees a provider image as `iw = ih = 256`, `maxLevel = 0`; the grid
formula `ceil(256 / (256·2^level))` yields 1 tile at 0 and 2^k at `-k`.

**Limits (effectively none):** `MAX_SCALE = 2^52`, `VIRTUAL_MIN_LEVEL = -52`
— the JS float64/safe-integer walls (tile indices must stay < 2^53). That is
a 2^52 × 2^52 grid ≈ 10^31 tiles; nothing in the pipeline uses fixed-width
int storage (JS doubles + string keys, Python ints), so the caps are the
platform's, not the tiling design's. Panning becomes coarse past ~2^40 but
rendering never errors. Real images are unaffected — their `baseLevel` still
clamps at `maxLevel`.

## 7. Client changes

- **`server/models.py`**: `source: str = "archive"` on `CoverInfo` and
  `PageInfo` (server↔client contract: `"archive"` = the normal path,
  anything else = provider tiles + no OCR). This is the generic contract for
  any future image server hook.
- **`nav.js` / `layout.js`**: carry `im.source` from `b.cover.source` /
  `p.source`.
- **`api/tiles.js`**: `tileUrl(book, page, version, level, tx, ty, blurred,
  source)` — when `source !== "archive"`, build
  `/pv/{book}/{page}/{version}/{level}/{tx}/{ty}.jpg` (book stays in the URL:
  it is the namespace key the server registry resolves). Scheduler passes
  `im.source` at both call sites.
- **`levelSelect.js`** — the one real geometry change:
  ```js
  export function baseLevel(im) {
    const dpr = window.devicePixelRatio || 1;
    const eff = im.fitFactor * state.view.scale;
    const L = Math.floor(-Math.log2(Math.max(eff * dpr, 1e-9)));
    if (im.source !== "archive") {
      return Math.max(VIRTUAL_MIN_LEVEL, Math.min(im.maxLevel, L));
    }
    return Math.max(0, Math.min(im.maxLevel, L));
  }
  ```
  Everything downstream already iterates correctly into negative levels
  (`nextStepTiles` from `maxLevel - 1` down, `imageComplete`, `tileCovered`,
  `pruneToLevel`, `onScreenCachedCount`).
- **`config.js`**: `VIRTUAL_MIN_LEVEL = -52`, `MAX_SCALE = 2 ** 52`; replace
  the `64` upper clamp with `MAX_SCALE` in `interaction.js` (wheel ~line 67,
  pinch ~line 197) and `viewport.js` (`fitViewToImage` ~line 48).
- **Negative-level fixes**: `render.js` debug loop
  `lv >= (im.source !== "archive" ? VIRTUAL_MIN_LEVEL : 0)` and a
  non-negative `LEVEL_COLORS` modulo; `ocr/overlay.js` skips provider images
  in `requestOcr`.

## 8. Wiring — `server/app.py`

One tidy block, next to the other services:

```python
sources = SourceRegistry([FractalSource(settings)])
app.state.sources = sources
app.state.source_tiles = SourceTileService(settings, sources)
...
app.include_router(sources_router.router)   # the /pv/ route
```

## 9. Performance

| lever | mechanism |
|---|---|
| render cost | vectorized NumPy escape-time, masked early-exit, depth-scaled iteration budget |
| concurrency | `asyncio.to_thread` + `KeyedLock` dedupe (mirrors `TileService`) |
| repeat visits | shared disk LRU (`p/fractal/...` keys) + `public, immutable` URLs → browser + edge caches |
| zoom-out cost | existing coverage culling unloads fine descendants when a coarse tile lands |
| RAM | zero long-lived buffers; tiles encode → free |
| quality | `PROGRESSIVE_STEP=1` coarse→fine, budget-limited by `MAX_DISPLAYED_TILES` |

## 10. Verification / acceptance

Server (run `python -m server.main`; an archive root must exist):

```bash
# provider book is listed (public) and its page listing is served
curl -s http://localhost:8000/api/books | python3 -m json.tool
curl -s http://localhost:8000/api/books/fractals/pages | python3 -m json.tool
# root tile = recognizable Mandelbrot; second call is a cache hit
curl -s -o /tmp/t0.jpg http://localhost:8000/pv/fractals/mandelbrot/0/0/0/0.jpg
curl -si http://localhost:8000/pv/fractals/mandelbrot/0/0/0/0.jpg | grep -i x-tile-cache
# arbitrary deep tile (level -12, grid 4096 x 4096)
curl -s -o /tmp/t1.jpg http://localhost:8000/pv/fractals/mandelbrot/0/-12/2048/1024.jpg
# errors: unknown source book -> 404; out-of-grid / too deep -> 400
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/pv/nope/mandelbrot/0/0/0/0.jpg
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/pv/fractals/mandelbrot/0/-12/5000/0.jpg
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/pv/fractals/mandelbrot/0/-60/0/0.jpg
# archive pages still work unchanged
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/api/books | python3 -c "import sys,json; print(json.load(sys.stdin)['books'][0]['id'])"
```

Stitch check: fetch the four level -1 tiles, upscale each 2×, assemble, and
compare edges with the level 0 root (small Pillow script) — proves the level
mapping, not just "looks like a fractal".

Client: `bun build server/static/js/main.js --outdir /tmp/check --target browser`
for syntax; manual pass — enter Fractals, wheel-zoom 20+ levels with
progressive refinement and the D-overlay, no seams, pan stays fluid; zoom
back out frees fine tiles (`window.dumpTiles()`); reload preserves view and
cached tiles; share link opens the fractal; login/logout doesn't affect it;
debug overlay colors negative levels.

## 11. File-by-file change list

Server:
- `server/sources/__init__.py` (new)
- `server/sources/base.py` (new) — `TileRequest`, `ImageSource`, `SourceRegistry`
- `server/sources/service.py` (new) — `SourceTileService` (shared LRU/locks/encode)
- `server/sources/router.py` (new) — `GET /pv/...`
- `server/sources/fractal/__init__.py` (new) — `FractalSource` + params
- `server/sources/fractal/mandelbrot.py` (new) — pure NumPy renderer
- `server/books/router.py` — three source hooks (books / pages / info)
- `server/models.py` — `source: str = "archive"` on `CoverInfo` + `PageInfo`
- `server/app.py` — construct registry + service, include `/pv/` router

Client:
- `server/static/js/config.js` — `VIRTUAL_MIN_LEVEL`, `MAX_SCALE`
- `server/static/js/api/tiles.js` — provider URL branch
- `server/static/js/nav.js` + `layout.js` — `im.source`
- `server/static/js/tiles/levelSelect.js` — `baseLevel` clamp for providers
- `server/static/js/tiles/scheduler.js` — pass `im.source` to `tileUrl`
- `server/static/js/interaction.js` + `viewport.js` — `MAX_SCALE` clamp
- `server/static/js/render.js` — debug loop bound + non-negative color index
- `server/static/js/ocr/overlay.js` — skip provider images

## 12. Non-goals / follow-ups

- **Not** a client-side fractal renderer (WebGL) — the point is proving the
  *server* tile pipeline pulls arbitrary tiles on demand through the hook.
- **Not** synthetic images on disk — gigabytes of finite files, defeats the
  demo and the hook.
- The fractal ships as the **reference `ImageSource`**. A second
  implementation (an HTTP adapter that proxies a real image server, or a
  local photo library mount) would follow the exact same interface — that is
  the "hook in an image server to a book location" story, and no router or
  client code would change (the client already branches on `source`).
- Social previews (`pages.py`/`social.py`) for provider share links render as
  a text-only/empty card today; add a provider case later if it matters.
- Optional accelerations: interior-region time-budget fallback, pre-warm
  levels 0..-3 on first visit, Julia set as a second registry entry.
