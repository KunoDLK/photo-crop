"""SQLite-backed, byte-limited tile cache with background eviction.

Replaces the ``diskcache``-backed LRU with an own SQLite store. Tiles are keyed
exactly as before (``t/...`` real, ``x<gen>/...`` blurred, ``p/<source>/...``
provider), but each row records the tile's ``zoom`` level (0 = the whole image
on one tile; larger = deeper/finer), its last access time, and its parsed
numeric coordinates. Eviction is **zoom-first, access-time-second within age
tiers**: the janitor deletes the least-recently-accessed tiles of the deepest
zoom levels before touching shallower ones, so coarse overview tiles are the
last to go and a freshly-written tile can never starve the client's overview
render.

Schema v2 (wafer-scale mosaics): every tile row also carries its ``ns``
(``t`` / ``x<gen>`` / ``p``), ``book``, ``page``, ``version``, ``level``,
``tx``, ``ty`` as indexed columns so coverage queries are range lookups, and a
second table stores **proxy chains** — the per-cell downsampled plane pyramid
the mosaic source renders coarse-band tiles from (see ``docs/wafer-mosaic-scaling.md``).
One chain per cell holds one lossless PNG per proxy-band level plus the pure
manifest geometry of its dependent tile range, so a whole-canvas tile decodes
N tiny planes instead of N full 300 MB sources. Byte accounting spans both
tables through triggers; eviction treats a chain as its eviction unit (whole
chains are deleted, never single planes) and drops redundant chains (every
dependent tile cached) before load-bearing ones, within each age tier.

Deletion never runs on the request path. ``put()`` inserts and, when the cache
is over its byte budget, wakes a per-database background janitor thread that
batches deletions down to a low-water mark. SQLite runs in WAL mode with one
connection per thread, so readers never block writers and the janitor shares
the file with every request thread.

Schema versioning is wipe-based, never migratory. The store reuses the
``cache.db`` filename the old diskcache store used, so on startup a file whose
``user_version`` does not match :data:`SCHEMA_VERSION` is deleted whole (WAL
sidecars included) and recreated from scratch — a leftover diskcache-format
cache or any stale schema is discarded automatically on the first open of a
new build, reclaiming its space (a schema change or a half-written store costs
the cache, never correctness).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

#: Bumped whenever the blur rendering changes: blur-tile keys embed it, so a
#: re-render never serves the old bytes from the disk cache (no manual wipe).
BLUR_GENERATION = 3

#: Schema version of the ``tiles``/``proxies``/``meta`` tables. Anything else
#: on open means the database file is deleted and recreated (module docstring).
SCHEMA_VERSION = 2

#: Database filename inside the cache directory. Reuses the name the old
#: diskcache store used, so a leftover file is caught by the schema check
#: and replaced on first open instead of lingering on disk.
DB_FILENAME = "cache.db"

#: Janitor deletes this many rows per transaction (bounds write-lock hold).
EVICT_BATCH = 200

#: Proxy chains evaluated per janitor batch (each chain delete is one txn).
EVICT_CHAIN_BATCH = 16

#: Seconds a tile's ``access_time`` may age before a cache hit rewrites it
#: (throttles the per-read write so hits stay cheap).
ACCESS_REFRESH_SECONDS = 60.0

#: Janitor re-checks the budget at least this often even without a wake signal.
SWEEP_INTERVAL_SECONDS = 60.0

_SCHEMA_SQL = """
CREATE TABLE meta (
    k TEXT PRIMARY KEY,
    v INTEGER NOT NULL
);
INSERT INTO meta (k, v) VALUES ('bytes', 0);
INSERT INTO meta (k, v) VALUES ('rows', 0);
CREATE TABLE tiles (
    rowid         INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    value         BLOB NOT NULL,
    creation_time REAL NOT NULL,
    access_time   REAL NOT NULL,
    zoom          INTEGER NOT NULL,
    ns            TEXT NOT NULL DEFAULT '',
    book          TEXT NOT NULL DEFAULT '',
    page          TEXT NOT NULL DEFAULT '',
    version       INTEGER NOT NULL DEFAULT 0,
    level         INTEGER NOT NULL DEFAULT 0,
    tx            INTEGER NOT NULL DEFAULT 0,
    ty            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_tiles_evict  ON tiles (zoom DESC, access_time ASC);
CREATE INDEX idx_tiles_family ON tiles (ns, book, page, version, level);
CREATE TRIGGER tiles_bytes_insert AFTER INSERT ON tiles BEGIN
    UPDATE meta SET v = v + length(NEW.value) WHERE k = 'bytes';
    UPDATE meta SET v = v + 1 WHERE k = 'rows';
END;
CREATE TRIGGER tiles_bytes_delete AFTER DELETE ON tiles BEGIN
    UPDATE meta SET v = v - length(OLD.value) WHERE k = 'bytes';
    UPDATE meta SET v = v - 1 WHERE k = 'rows';
END;
CREATE TRIGGER tiles_bytes_update AFTER UPDATE OF value ON tiles BEGIN
    UPDATE meta SET v = v + length(NEW.value) - length(OLD.value) WHERE k = 'bytes';
END;
CREATE TABLE proxies (
    key           TEXT PRIMARY KEY,
    chain_id      TEXT NOT NULL,
    ns            TEXT NOT NULL,
    book          TEXT NOT NULL,
    page          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    level         INTEGER NOT NULL,
    width         INTEGER NOT NULL,
    value         BLOB NOT NULL,
    tx0           INTEGER NOT NULL,
    tx1           INTEGER NOT NULL,
    ty0           INTEGER NOT NULL,
    ty1           INTEGER NOT NULL,
    expected      INTEGER NOT NULL,
    creation_time REAL NOT NULL,
    access_time   REAL NOT NULL
);
CREATE INDEX idx_proxies_chain ON proxies (chain_id);
CREATE INDEX idx_proxies_evict ON proxies (access_time);
CREATE TRIGGER proxies_bytes_insert AFTER INSERT ON proxies BEGIN
    UPDATE meta SET v = v + length(NEW.value) WHERE k = 'bytes';
    UPDATE meta SET v = v + 1 WHERE k = 'rows';
END;
CREATE TRIGGER proxies_bytes_delete AFTER DELETE ON proxies BEGIN
    UPDATE meta SET v = v - length(OLD.value) WHERE k = 'bytes';
    UPDATE meta SET v = v - 1 WHERE k = 'rows';
END;
CREATE TRIGGER proxies_bytes_update AFTER UPDATE OF value ON proxies BEGIN
    UPDATE meta SET v = v + length(NEW.value) - length(OLD.value) WHERE k = 'bytes';
END;
PRAGMA user_version = %d;
""" % SCHEMA_VERSION

#: Per-database-file shared state (schema init + one janitor thread), so the
#: two services that open the same cache directory share a single budget,
#: schema, and evictor instead of fighting over the file.
_registry: dict[str, "_SharedCache"] = {}
_registry_guard = threading.Lock()


def _connect(path: Path) -> sqlite3.Connection:
    """Open a WAL-mode connection with safe write-lock timeouts.

    Args:
        path: Path to the SQLite database file.

    Returns:
        A connection in autocommit mode (each statement commits itself).
    """
    con = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA cache_size=-65536")
    return con


def _init_schema(path: Path) -> None:
    """Ensure ``path`` holds the current tile schema, wiping any other file.

    Runs once per database file under an init lock. A file whose
    ``user_version`` does not match :data:`SCHEMA_VERSION` — a leftover
    diskcache-format cache, a stale schema, or any foreign content — is
    deleted whole (including its ``-wal``/``-shm`` sidecars) and recreated
    from scratch, so a filename reuse never leaves foreign bytes behind.

    Args:
        path: Path to the SQLite database file.
    """
    if not path.exists():
        _create_schema(path)
        return
    con = _connect(path)
    try:
        (version,) = con.execute("PRAGMA user_version").fetchone()
    finally:
        con.close()
    if version != SCHEMA_VERSION:
        _remove_database(path)
        _create_schema(path)


def _create_schema(path: Path) -> None:
    """Create a fresh tile database at ``path`` (asserting file-level pragmas).

    Args:
        path: Path to the SQLite database file.
    """
    con = _connect(path)
    try:
        con.execute("PRAGMA page_size=4096")
        con.execute("PRAGMA auto_vacuum=FULL")
        con.executescript(_SCHEMA_SQL)
    finally:
        con.close()


def _remove_database(path: Path) -> None:
    """Delete a database file and its WAL sidecars (missing files are fine).

    Args:
        path: Path to the SQLite database file.
    """
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{path}{suffix}").unlink()
        except FileNotFoundError:
            pass


def parse_key(key: str) -> tuple[str, str, str, int, int, int, int]:
    """Split a tile key into its numeric-coordinate components.

    Keys are ``{ns}/{book}/{page}/{version}/{level}/{tx}/{ty}`` where ``ns`` is
    ``t`` (real archive tiles), ``x<gen>`` (blurred archive tiles), or ``p``
    (provider tiles, whose keys carry an extra source slug between ``ns`` and
    ``book``). Book/page ids never contain ``/``, so the split is positional.
    A key that does not match either shape yields ``('', '', '', 0, 0, 0, 0)``
    — such rows keep full eviction behaviour but never participate in proxy
    redundancy queries (which only ever match well-formed families).

    Args:
        key: A tile key as produced by :meth:`TileCache.key` or the provider
            tile service.

    Returns:
        A ``(ns, book, page, version, level, tx, ty)`` tuple.
    """
    parts = key.split("/")
    try:
        if len(parts) == 7:
            ns, book, page = parts[0], parts[1], parts[2]
            version, level, tx, ty = (int(p) for p in parts[3:7])
        elif len(parts) == 8 and parts[0] == "p":
            # Provider keys: p/<source>/<book>/<page>/<version>/<level>/<tx>/<ty>.
            ns = "p"
            book, page = parts[2], parts[3]
            version, level, tx, ty = (int(p) for p in parts[4:8])
        else:
            return "", "", "", 0, 0, 0, 0
    except ValueError:
        return "", "", "", 0, 0, 0, 0
    return ns, book, page, version, level, tx, ty


class _SharedCache:
    """Schema init, budget, and the janitor thread for one database file."""

    def __init__(
        self, path: Path, size_limit_bytes: int,
        evict_young_seconds: float = 600.0, evict_old_seconds: float = 604800.0,
    ) -> None:
        self.path = path
        self.size_limit = size_limit_bytes
        self.low_water = max(1, int(size_limit_bytes * 0.95))
        self.evict_young = evict_young_seconds
        self.evict_old = max(evict_old_seconds, evict_young_seconds + 1.0)
        self.refs = 0
        self._init_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def ensure_init(self) -> None:
        """Create the schema exactly once, then start the janitor thread."""
        with self._init_lock:
            _init_schema(self.path)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._janitor_loop, name=f"tile-janitor:{self.path.name}", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        """Ask the janitor to check the budget soon (used after over-budget puts)."""
        self._wake.set()

    def stop(self) -> None:
        """Stop the janitor thread and wait for it to exit."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def cull(self, con: sqlite3.Connection) -> int:
        """Delete over-budget rows until the cache is at/below the low-water mark.

        Candidates are consumed in the design's total order (E2): within each
        age tier — stale (older than ``evict_old``), cold (older than
        ``evict_young``), fresh (everything else) — every tile goes before
        every proxy chain, so a tile that was evicted can be regenerated from
        a surviving chain while a chain that was evicted forces a full-res
        decode to come back. Tiles sort deepest-zoom first, least-recently
        accessed first; chains delete whole (never single planes) with
        redundant chains first (every dependent tile cached — pure redundancy)
        and load-bearing chains last within their tier. Each batch is its own
        commit so concurrent writers slip in between batches.

        Args:
            con: Connection to delete on (the janitor's own, or a caller's).

        Returns:
            The number of rows deleted.
        """
        removed = 0
        # Age tiers over (tiles, chains): each entry is (kind, lo, hi) age bounds.
        stages = (
            ("tiles", self.evict_old, None),
            ("chains", self.evict_old, None),
            ("tiles", self.evict_young, self.evict_old),
            ("chains", self.evict_young, self.evict_old),
            ("tiles", None, self.evict_young),
            ("chains", None, self.evict_young),
        )
        while not self._stop.is_set():
            if self._volume(con) <= self.low_water:
                break
            progressed = False
            for kind, lo, hi in stages:
                while not self._stop.is_set() and self._volume(con) > self.low_water:
                    n = self._cull_batch(con, kind, lo, hi, time.time())
                    if n == 0:
                        break
                    removed += n
                    progressed = True
            if not progressed:
                break
        return removed

    def _cull_batch(
        self, con: sqlite3.Connection, kind: str,
        age_lo: float | None, age_hi: float | None, now: float,
    ) -> int:
        """Delete one batch of the given kind within the age band.

        Args:
            con: Connection to delete on.
            kind: ``"tiles"`` or ``"chains"``.
            age_lo: Exclude rows whose age is below this (None = no floor).
            age_hi: Exclude rows whose age is at or above this (None = no cap).
            now: Current time, shared across a cull pass.

        Returns:
            The number of rows deleted (0 when the band is exhausted).
        """
        if kind == "tiles":
            return self._cull_tile_batch(con, age_lo, age_hi, now)
        return self._cull_chain_batch(con, age_lo, age_hi, now)

    def _age_sql(self, age_lo: float | None, age_hi: float | None, now: float) -> tuple[str, list]:
        """Build the SQL age-band clause for the current tier.

        Args:
            age_lo: Minimum row age (exclusive), or None.
            age_hi: Maximum row age (inclusive), or None.
            now: Current time.

        Returns:
            A ``(clause, params)`` pair appended to a WHERE expression.
        """
        clause = f"({now} - access_time)"
        conds: list[str] = []
        params: list[float] = []
        if age_lo is not None:
            conds.append(f"{clause} > ?")
            params.append(age_lo)
        if age_hi is not None:
            conds.append(f"{clause} <= ?")
            params.append(age_hi)
        return " AND ".join(conds) or "1", params

    def _cull_tile_batch(
        self, con: sqlite3.Connection, age_lo: float | None, age_hi: float | None, now: float,
    ) -> int:
        """Delete up to one batch of over-budget tiles in this age tier.

        Args:
            con: Connection to delete on.
            age_lo: Minimum row age (exclusive), or None.
            age_hi: Maximum row age (inclusive), or None.
            now: Current time.

        Returns:
            The number of rows deleted (0 when the tier is exhausted).
        """
        volume = self._volume(con)
        if volume <= self.low_water:
            return 0
        rows = self._rows(con)
        avg = volume / rows if rows else 0.0
        needed = int((volume - self.low_water) / avg) + 1 if avg > 0 else 1
        limit = max(1, min(EVICT_BATCH, needed))
        clause, params = self._age_sql(age_lo, age_hi, now)
        cur = con.execute(
            "DELETE FROM tiles WHERE rowid IN ("
            f" SELECT rowid FROM tiles WHERE {clause}"
            " ORDER BY zoom DESC, access_time ASC, rowid ASC"
            " LIMIT ?)",
            (*params, limit),
        )
        return cur.rowcount

    def _chain_candidates(
        self, con: sqlite3.Connection, age_lo: float | None, age_hi: float | None, now: float,
        limit: int,
    ) -> list[str]:
        """Chain ids in an age tier, least-recently accessed first.

        A chain's age is its most recently refreshed plane row's age (all rows
        of a chain are written and touched together).

        Args:
            con: Connection to read through.
            age_lo: Minimum chain age (exclusive), or None.
            age_hi: Maximum chain age (inclusive), or None.
            now: Current time.
            limit: Maximum number of candidate chain ids.

        Returns:
            Up to ``limit`` chain ids ordered by access time.
        """
        conds: list[str] = []
        params: list[float] = []
        if age_lo is not None:
            conds.append("(MAX(access_time)) < ?")
            params.append(now - age_lo)
        if age_hi is not None:
            conds.append("(MAX(access_time)) >= ?")
            params.append(now - age_hi)
        having = " AND ".join(conds) if conds else "1"
        rows = con.execute(
            "SELECT chain_id FROM proxies GROUP BY chain_id"
            f" HAVING {having} ORDER BY MAX(access_time) ASC, chain_id ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [r[0] for r in rows]

    def _chain_redundant(self, con: sqlite3.Connection, chain_id: str) -> bool:
        """True when every plane of the chain has all its dependent tiles cached.

        The dependent tile set of a plane is the pure manifest geometry stored
        on its row (G4): the level-``L`` grid tiles whose rectangle intersects
        the cell. When every such tile row exists the chain is pure redundancy
        (E5) — dropping it costs nothing until a tile is evicted; if any
        dependent tile is missing the chain is load-bearing and its loss would
        force a full-res decode, so it is evicted last within its tier.

        Args:
            con: Connection to read through.
            chain_id: The chain to test.

        Returns:
            Whether every dependent tile of every plane is cached.
        """
        planes = con.execute(
            "SELECT ns, book, page, version, level, tx0, tx1, ty0, ty1, expected"
            " FROM proxies WHERE chain_id = ?",
            (chain_id,),
        ).fetchall()
        if not planes:
            return True  # nothing stored: nothing to protect (handled as absent)
        for ns, book, page, version, level, tx0, tx1, ty0, ty1, expected in planes:
            (count,) = con.execute(
                "SELECT COUNT(*) FROM tiles WHERE ns = ? AND book = ? AND page = ?"
                " AND version = ? AND level = ? AND tx BETWEEN ? AND ? AND ty BETWEEN ? AND ?",
                (ns, book, page, version, level, tx0, tx1, ty0, ty1),
            ).fetchone()
            if count != expected:
                return False
        return True

    def _cull_chain_batch(
        self, con: sqlite3.Connection, age_lo: float | None, age_hi: float | None, now: float,
    ) -> int:
        """Delete whole chains in this age tier, redundant ones first.

        Args:
            con: Connection to delete on.
            age_lo: Minimum chain age (exclusive), or None.
            age_hi: Maximum chain age (inclusive), or None.
            now: Current time.

        Returns:
            The number of proxy rows deleted (0 when the tier is exhausted).
        """
        candidates = self._chain_candidates(con, age_lo, age_hi, now, EVICT_CHAIN_BATCH)
        if not candidates:
            return 0
        # E4: redundant chains (all dependents cached) are pure redundancy and
        # go first; load-bearing chains survive until the tier is exhausted.
        redundant = [c for c in candidates if self._chain_redundant(con, c)]
        load_bearing = [c for c in candidates if c not in redundant]
        removed = 0
        for chain_id in (*redundant, *load_bearing):
            if self._volume(con) <= self.low_water:
                break
            cur = con.execute("DELETE FROM proxies WHERE chain_id = ?", (chain_id,))
            if cur.rowcount == 0:
                continue
            removed += cur.rowcount
            if removed >= EVICT_BATCH:
                break
        return removed

    @staticmethod
    def _volume(con: sqlite3.Connection) -> int:
        """Total live tile+proxy bytes, from the maintained counter (self-healing).

        Args:
            con: Connection to read through.

        Returns:
            ``SUM(length(value))`` over both tables.
        """
        row = con.execute("SELECT v FROM meta WHERE k = 'bytes'").fetchone()
        if row is not None:
            return int(row[0])
        total = int(con.execute(
            "SELECT COALESCE(SUM(length(value)), 0) FROM tiles"
        ).fetchone()[0]) + int(con.execute(
            "SELECT COALESCE(SUM(length(value)), 0) FROM proxies"
        ).fetchone()[0])
        con.execute(
            "INSERT INTO meta (k, v) VALUES ('bytes', ?)"
            " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (total,),
        )
        return total

    @staticmethod
    def _rows(con: sqlite3.Connection) -> int:
        """Live row count (tiles + proxy planes), from the maintained counter.

        Args:
            con: Connection to read through.

        Returns:
            The number of rows in both tables.
        """
        row = con.execute("SELECT v FROM meta WHERE k = 'rows'").fetchone()
        if row is not None:
            return int(row[0])
        total = int(con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]) + int(
            con.execute("SELECT COUNT(*) FROM proxies").fetchone()[0]
        )
        con.execute(
            "INSERT INTO meta (k, v) VALUES ('rows', ?)"
            " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (total,),
        )
        return total

    def _janitor_loop(self) -> None:
        """Background thread body: wake on demand, sweep periodically."""
        con = _connect(self.path)
        try:
            while not self._stop.is_set():
                if self._wake.wait(SWEEP_INTERVAL_SECONDS):
                    self._wake.clear()
                self.cull(con)
        finally:
            con.close()


class TileCache:
    """SQLite tile + proxy-chain store with a shared background janitor.

    Identical public surface to the cache it replaces (:meth:`key`/:meth:`get`/
    :meth:`put`/:meth:`contains`) plus a mandatory ``zoom`` on :meth:`put` and
    the proxy-chain API (:meth:`put_chain`/:meth:`get_chain`/:meth:`touch_chain`/
    :meth:`delete_chain`). All instances pointing at the same database file
    share one schema and one janitor thread.
    """

    def __init__(
        self, cache_dir: Path, size_limit_bytes: int,
        evict_young_seconds: float = 600.0, evict_old_seconds: float = 604800.0,
    ) -> None:
        """Open (creating or wiping as needed) the tile database.

        Args:
            cache_dir: Directory for the tile database (``cache.db`` inside).
            size_limit_bytes: Byte budget for stored tile + proxy data;
                eviction holds the cache at 95% of this once the janitor
                catches up.
            evict_young_seconds: Age at which a row leaves the fresh tier
                (default 600 s).
            evict_old_seconds: Age at which a row enters the stale tier
                (default 7 days).
        """
        path = cache_dir / DB_FILENAME
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = str(path.resolve())
        with _registry_guard:
            shared = _registry.get(key)
            if shared is None:
                shared = _SharedCache(
                    path, size_limit_bytes,
                    evict_young_seconds=evict_young_seconds,
                    evict_old_seconds=evict_old_seconds,
                )
                _registry[key] = shared
            shared.refs += 1
        self._path = path
        self._shared = shared
        self._local = threading.local()
        self._shared.ensure_init()

    @staticmethod
    def key(
        book: str, page: str, version: int, level: int, tx: int, ty: int,
        blur: bool = False,
    ) -> str:
        """Build a stable cache key for a tile.

        The ``version`` (the page file's mtime, or a provider's content
        version) namespaces the cache, so changed content produces new keys
        and stale tiles are never served. The ``blur`` flag selects the
        ``t/`` (real) or ``x<gen>/`` (blurred) prefix and provider tiles use
        ``p/<source>/`` keys, keeping every variant strictly separated.

        Args:
            book: Book directory name (or provider book id).
            page: Page id (filename).
            version: Page file mtime (content version).
            level: Pyramid level (negative levels valid for providers).
            tx: Tile column.
            ty: Tile row.
            blur: True for the blurred variant of the tile.

        Returns:
            A string key for the cache.
        """
        prefix = f"x{BLUR_GENERATION}" if blur else "t"
        return f"{prefix}/{book}/{page}/{version}/{level}/{tx}/{ty}"

    @property
    def db_path(self) -> Path:
        """Database file path (useful for inspection)."""
        return self._path

    @property
    def size_bytes(self) -> int:
        """Live tile + proxy data bytes (the number the budget is enforced against)."""
        return self._shared._volume(self._conn())  # noqa: SLF001 — shared by design

    @property
    def row_count(self) -> int:
        """Number of cached tiles (proxy planes are counted separately)."""
        return int(self._conn().execute("SELECT COUNT(*) FROM tiles").fetchone()[0])

    @property
    def chain_count(self) -> int:
        """Number of cached proxy chains."""
        return int(
            self._conn().execute("SELECT COUNT(DISTINCT chain_id) FROM proxies").fetchone()[0]
        )

    def get(self, key: str) -> bytes | None:
        """Return cached tile bytes, or ``None`` on a miss.

        A hit refreshes the row's ``access_time`` (throttled, so repeated
        reads stay cheap) — the tile's recency within its zoom level.

        Args:
            key: Cache key produced by :meth:`key`.

        Returns:
            The stored JPEG bytes, or ``None``.
        """
        con = self._conn()
        row = con.execute(
            "SELECT value, access_time FROM tiles WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value, accessed = row
        now = time.time()
        if now - accessed > ACCESS_REFRESH_SECONDS:
            con.execute("UPDATE tiles SET access_time = ? WHERE key = ?", (now, key))
        return value

    def put(self, key: str, data: bytes, zoom: int) -> None:
        """Store encoded tile bytes, waking the janitor when over budget.

        Inserting over the budget never deletes inline: the janitor thread
        reclaims in the background, so tile rendering is not slowed by cache
        maintenance. Re-putting an existing key replaces it in place (byte
        accounting netted by trigger). The row's numeric coordinates are
        parsed from the key so coverage/redundancy queries stay range lookups.

        Args:
            key: Cache key produced by :meth:`key`.
            data: Encoded JPEG bytes.
            zoom: Tile depth from the whole-image tile (0 = one tile per
                image, 1 = 2x2 grid, ...). Eviction deletes the deepest zoom
                levels first; pass ``max_level - level`` for archive pages or
                ``-level`` for providers whose level 0 is the whole image.
        """
        now = time.time()
        ns, book, page, version, level, tx, ty = parse_key(key)
        con = self._conn()
        con.execute(
            "INSERT INTO tiles (key, value, creation_time, access_time, zoom,"
            " ns, book, page, version, level, tx, ty)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET"
            " value = excluded.value,"
            " creation_time = excluded.creation_time,"
            " access_time = excluded.access_time,"
            " zoom = excluded.zoom,"
            " ns = excluded.ns, book = excluded.book, page = excluded.page,"
            " version = excluded.version, level = excluded.level,"
            " tx = excluded.tx, ty = excluded.ty",
            (key, sqlite3.Binary(data), now, now, zoom,
             ns, book, page, version, level, tx, ty),
        )
        if self._shared._volume(con) > self._shared.size_limit:  # noqa: SLF001
            self._shared.wake()

    def contains(self, key: str) -> bool:
        """Return True if the tile is cached.

        Args:
            key: Cache key produced by :meth:`key`.
        """
        row = self._conn().execute("SELECT 1 FROM tiles WHERE key = ?", (key,)).fetchone()
        return row is not None

    def put_chain(
        self, chain_id: str, ns: str, book: str, page: str, version: int,
        planes: list[tuple[int, int, bytes, int, int, int, int, int]],
    ) -> None:
        """Store (or atomically replace) one cell's whole proxy chain.

        One transaction deletes any previous rows of the chain and inserts
        every plane, so a rebuild is all-or-nothing (B6): a partial chain
        found at read time is treated as absent. Each plane row carries the
        pure manifest geometry of its dependent tile range (G4) so eviction
        can test redundancy without geometry or key parsing.

        Args:
            chain_id: ``proxy/<ns>/<book>/<page>/<version>/<cell>``.
            ns: Variant namespace, ``p`` for provider tiles.
            book: Book id of the page the planes feed.
            page: Page id of the page the planes feed.
            version: Page content version.
            planes: One ``(level, width, png_bytes, tx0, tx1, ty0, ty1,
                expected)`` tuple per proxy-band level.
        """
        now = time.time()
        con = self._conn()
        con.execute("BEGIN")
        try:
            con.execute("DELETE FROM proxies WHERE chain_id = ?", (chain_id,))
            con.executemany(
                "INSERT INTO proxies (key, chain_id, ns, book, page, version,"
                " level, width, value, tx0, tx1, ty0, ty1, expected,"
                " creation_time, access_time)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (f"{chain_id}/{level}", chain_id, ns, book, page, version,
                     level, width, sqlite3.Binary(png),
                     tx0, tx1, ty0, ty1, expected, now, now)
                    for level, width, png, tx0, tx1, ty0, ty1, expected in planes
                ],
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        if self._shared._volume(con) > self._shared.size_limit:  # noqa: SLF001
            self._shared.wake()

    def get_chain(self, chain_id: str) -> dict[int, tuple[int, bytes]] | None:
        """Return the chain's planes as ``{level: (width, png_bytes)}``.

        Reads never refresh the chain's ``access_time`` (E6): only chain
        builds and tile generations do, so an idle chain ages through the
        eviction tiers exactly when the cache needs room.

        Args:
            chain_id: The chain to read.

        Returns:
            A level-keyed map of plane rows, or ``None`` when the chain has no
            rows at all. A partial chain (missing band levels) is the caller's
            signal to rebuild.
        """
        rows = self._conn().execute(
            "SELECT level, width, value FROM proxies WHERE chain_id = ?", (chain_id,)
        ).fetchall()
        if not rows:
            return None
        return {level: (width, value) for level, width, value in rows}

    def touch_chain(self, chain_id: str) -> None:
        """Refresh a chain's access time after a tile generation used its planes.

        Throttled like tile hits so a burst of tile generations does not write
        once per tile; a chain that stops being generated ages into the stale
        tier on schedule (E6b).

        Args:
            chain_id: The chain that served a generated tile.
        """
        con = self._conn()
        row = con.execute(
            "SELECT MAX(access_time) FROM proxies WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return
        now = time.time()
        if now - row[0] > ACCESS_REFRESH_SECONDS:
            con.execute(
                "UPDATE proxies SET access_time = ? WHERE chain_id = ?", (now, chain_id)
            )

    def delete_chain(self, chain_id: str) -> int:
        """Delete every plane of a chain (the chain is the eviction unit).

        Args:
            chain_id: The chain to delete.

        Returns:
            The number of proxy rows deleted.
        """
        cur = self._conn().execute("DELETE FROM proxies WHERE chain_id = ?", (chain_id,))
        return cur.rowcount

    def cull(self) -> int:
        """Run an eviction pass now (janitor also does this in the background).

        Exposed for tests and diagnostics.

        Returns:
            The number of rows deleted.
        """
        return self._shared.cull(self._conn())

    def close(self) -> None:
        """Release this instance; stop the janitor when the last one closes."""
        with _registry_guard:
            shared = _registry.get(str(self._path.resolve()))
            if shared is None:
                return
            shared.refs -= 1
            if shared.refs > 0:
                return
            _registry.pop(str(self._path.resolve()), None)
        shared.stop()

    def _conn(self) -> sqlite3.Connection:
        """A connection bound to the calling thread (created on first use)."""
        con = getattr(self._local, "con", None)
        if con is None:
            con = _connect(self._path)
            self._local.con = con
        return con
