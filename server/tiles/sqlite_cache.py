"""SQLite-backed, byte-limited tile cache with background eviction.

Replaces the ``diskcache``-backed LRU with an own SQLite store. Tiles are keyed
exactly as before (``t/...`` real, ``x<gen>/...`` blurred, ``p/<source>/...``
provider), but each row records the tile's ``zoom`` level (0 = the whole image
on one tile; larger = deeper/finer) and its last access time. Eviction is
**zoom-first, access-time-second**: the janitor deletes the least-recently-
accessed tiles of the deepest zoom levels before touching shallower ones, so
coarse overview tiles are the last to go and a freshly-written tile can never
starve the client's overview render.

Deletion never runs on the request path. ``put()`` inserts and, when the cache
is over its byte budget, wakes a per-database background janitor thread that
batches deletions down to a low-water mark. SQLite runs in WAL mode with one
connection per thread, so readers never block writers and the janitor shares
the file with every request thread.

Schema versioning is wipe-based, never migratory: opening a database whose
``user_version`` does not match :data:`SCHEMA_VERSION` drops both tables and
recreates them (a schema change or a half-written store costs the cache, never
correctness). Old diskcache ``cache.db`` files are simply not read.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

#: Bumped whenever the blur rendering changes: blur-tile keys embed it, so a
#: re-render never serves the old bytes from the disk cache (no manual wipe).
BLUR_GENERATION = 3

#: Schema version of the ``tiles``/``meta`` tables. Anything else on open
#: means the database is wiped and recreated (see module docstring).
SCHEMA_VERSION = 1

#: Janitor deletes this many rows per transaction (bounds write-lock hold).
EVICT_BATCH = 200

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
    zoom          INTEGER NOT NULL
);
CREATE INDEX idx_tiles_evict ON tiles (zoom DESC, access_time ASC);
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
    """Create or wipe-and-recreate the tile tables to match the schema version.

    Runs once per database file under an init lock. A missing or stale
    ``user_version`` drops both tables and rebuilds them from scratch; the
    file-level pragmas (``page_size``, ``auto_vacuum``) are asserted first,
    harmlessly ignored when the file already carries them.

    Args:
        path: Path to the SQLite database file.
    """
    con = _connect(path)
    try:
        con.execute("PRAGMA page_size=4096")
        con.execute("PRAGMA auto_vacuum=FULL")
        (version,) = con.execute("PRAGMA user_version").fetchone()
        if version != SCHEMA_VERSION:
            con.execute("DROP TABLE IF EXISTS tiles")
            con.execute("DROP TABLE IF EXISTS meta")
            con.executescript(_SCHEMA_SQL)
    finally:
        con.close()


class _SharedCache:
    """Schema init, budget, and the janitor thread for one database file."""

    def __init__(self, path: Path, size_limit_bytes: int) -> None:
        self.path = path
        self.size_limit = size_limit_bytes
        self.low_water = max(1, int(size_limit_bytes * 0.95))
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
        """Delete over-budget tiles until the cache is at or under the low-water mark.

        Rows are removed deepest-zoom first, least-recently-accessed first
        within a zoom level; zoom-0 (whole-image) tiles sort last and are only
        deleted once every deeper tile is gone. Each batch's size is estimated
        from the average row size so the loop stops at the low-water mark
        instead of overshooting to an empty cache, and each batch is its own
        commit so concurrent writers slip in between batches.

        Args:
            con: Connection to delete on (the janitor's own, or a caller's).

        Returns:
            The number of rows deleted.
        """
        removed = 0
        while not self._stop.is_set():
            volume = self._volume(con)
            if volume <= self.low_water:
                break
            rows = self._rows(con)
            avg = volume / rows if rows else 0.0
            # How many rows must go to reach the low-water mark (capped at one
            # batch); the loop re-estimates after each commit.
            needed = int((volume - self.low_water) / avg) + 1 if avg > 0 else 1
            limit = max(1, min(EVICT_BATCH, needed))
            cur = con.execute(
                "DELETE FROM tiles WHERE rowid IN ("
                " SELECT rowid FROM tiles"
                " ORDER BY zoom DESC, access_time ASC, rowid ASC"
                " LIMIT ?)",
                (limit,),
            )
            if cur.rowcount == 0:
                break
            removed += cur.rowcount
        return removed

    @staticmethod
    def _volume(con: sqlite3.Connection) -> int:
        """Total live tile bytes, from the maintained counter (self-healing).

        Args:
            con: Connection to read through.

        Returns:
            ``SUM(length(value))`` over all rows.
        """
        row = con.execute("SELECT v FROM meta WHERE k = 'bytes'").fetchone()
        if row is not None:
            return int(row[0])
        total = int(con.execute(
            "SELECT COALESCE(SUM(length(value)), 0) FROM tiles"
        ).fetchone()[0])
        con.execute(
            "INSERT INTO meta (k, v) VALUES ('bytes', ?)"
            " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (total,),
        )
        return total

    @staticmethod
    def _rows(con: sqlite3.Connection) -> int:
        """Live tile count, from the maintained counter (self-healing).

        Args:
            con: Connection to read through.

        Returns:
            The number of rows in ``tiles``.
        """
        row = con.execute("SELECT v FROM meta WHERE k = 'rows'").fetchone()
        if row is not None:
            return int(row[0])
        total = int(con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0])
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
    """SQLite tile store with a shared background janitor.

    Identical public surface to the diskcache-backed cache it replaces
    (:meth:`key`/:meth:`get`/:meth:`put`/:meth:`contains`), plus a mandatory
    ``zoom`` on :meth:`put` so every row carries its eviction priority. All
    instances pointing at the same database file share one schema and one
    janitor thread.
    """

    def __init__(self, cache_dir: Path, size_limit_bytes: int) -> None:
        """Open (creating or wiping as needed) the tile database.

        Args:
            cache_dir: Directory for the tile database (``tiles.db`` inside).
            size_limit_bytes: Byte budget for stored tile data; eviction holds
                the cache at 95% of this once the janitor catches up.
        """
        path = cache_dir / "tiles.db"
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = str(path.resolve())
        with _registry_guard:
            shared = _registry.get(key)
            if shared is None:
                shared = _SharedCache(path, size_limit_bytes)
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
        """Live tile data bytes (the number the budget is enforced against)."""
        return self._shared._volume(self._conn())  # noqa: SLF001 — shared by design

    @property
    def row_count(self) -> int:
        """Number of cached tiles."""
        return int(self._conn().execute("SELECT COUNT(*) FROM tiles").fetchone()[0])

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
        accounting netted by trigger).

        Args:
            key: Cache key produced by :meth:`key`.
            data: Encoded JPEG bytes.
            zoom: Tile depth from the whole-image tile (0 = one tile per
                image, 1 = 2x2 grid, ...). Eviction deletes the deepest zoom
                levels first; pass ``max_level - level`` for archive pages or
                ``-level`` for providers whose level 0 is the whole image.
        """
        now = time.time()
        con = self._conn()
        con.execute(
            "INSERT INTO tiles (key, value, creation_time, access_time, zoom)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET"
            " value = excluded.value,"
            " creation_time = excluded.creation_time,"
            " access_time = excluded.access_time,"
            " zoom = excluded.zoom",
            (key, sqlite3.Binary(data), now, now, zoom),
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
