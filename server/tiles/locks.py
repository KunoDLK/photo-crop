"""Per-key async locks to dedupe concurrent tile requests.

When several clients (or one fast client) request the same tile simultaneously,
only one should do the decode/resample work; the rest await the same result.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from types import TracebackType


class _LockGuard:
    """Async context manager that releases a keyed lock on exit."""

    def __init__(self, owner: "KeyedLock", key: str) -> None:
        self._owner = owner
        self._key = key

    async def __aenter__(self) -> None:
        await self._owner._lock(self._key).acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        self._owner._release(self._key)


class KeyedLock:
    """A pool of :class:`asyncio.Lock` objects keyed by arbitrary strings."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._refcount: dict[str, int] = defaultdict(int)

    def acquire(self, key: str) -> _LockGuard:
        """Return an async context manager that locks ``key``.

        Args:
            key: Dedupe key (e.g. the tile cache key).

        Returns:
            A context manager usable with ``async with``.
        """
        return _LockGuard(self, key)

    def _lock(self, key: str) -> asyncio.Lock:
        """Get (or create) the lock for ``key`` and bump its refcount."""
        self._refcount[key] += 1
        return self._locks[key]

    def _release(self, key: str) -> None:
        """Decrement the refcount and drop the lock once unused."""
        self._refcount[key] -= 1
        if self._refcount[key] <= 0:
            self._refcount.pop(key, None)
            self._locks.pop(key, None)
