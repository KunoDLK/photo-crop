"""OCR service: background OCR with a disk JSON cache, plus text search.

OCR is expensive, so it is off the request path entirely: a single low-priority
background worker drains a priority queue of pages, decoding + recognizing each
one and caching the result to disk keyed by page version (file mtime). Image
serving is unaffected — it never shares the OCR worker thread, and interactive
(single-page) OCR requests are prioritised ahead of bulk search prefetch.

Search is therefore non-blocking: it matches against whatever is already cached
and submits any missing pages to the worker, reporting how many are still
pending so the client can poll and show progressive results.
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import re
import threading
from pathlib import Path

from ..books.scanner import page_path
from ..config import Settings
from ..models import OCRPage, OCRWord, SearchHit
from ..tiles import decoder
from . import engine

#: Interactive (single-page) OCR jumps ahead of bulk search prefetch.
_PRIO_INTERACTIVE = 0
_PRIO_BULK = 1


class OCRService:
    """Per-page OCR driven by a background worker, with a disk cache."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache_dir = settings.ocr_cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._counter = itertools.count()
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._pending: set[str] = set()
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True, name="ocr-worker")
        self._worker.start()

    # ------------------------------------------------------------- caching

    def _page_version(self, book: str, page: str) -> int:
        return page_path(self.settings.archive_root, book, page).stat().st_mtime_ns

    def _cache_path(self, book: str, page: str, version: int) -> Path:
        return self.cache_dir / book / page / f"{version}.json"

    @staticmethod
    def _key(book: str, page: str, version: int) -> str:
        return f"{book}/{page}/{version}"

    @staticmethod
    def _load(path: Path) -> dict | None:
        try:
            if path.is_file():
                return json.loads(path.read_text())
        except Exception:  # noqa: BLE001 — a corrupt cache file is a miss
            pass
        return None

    @staticmethod
    def _store(path: Path, data: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)  # atomic: readers never see a partial file
        except Exception:  # noqa: BLE001 — cache write failures are non-fatal
            pass

    # ------------------------------------------------------------- worker

    def _run(self) -> None:
        """Drain the priority queue, OCR-ing one page at a time."""
        while True:
            priority, _, book, page, version, key = self._queue.get()
            try:
                self._ocr_and_store(book, page, version)
            except Exception:  # noqa: BLE001 — a failed page is simply not cached
                pass
            finally:
                with self._lock:
                    self._pending.discard(key)
                    ev = self._events.pop(key, None)
                if ev is not None:
                    ev.set()
                self._queue.task_done()

    def _enqueue(self, priority: int, book: str, page: str, version: int, key: str) -> None:
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
            self._queue.put((priority, next(self._counter), book, page, version, key))

    def _event(self, key: str) -> threading.Event:
        with self._lock:
            ev = self._events.get(key)
            if ev is None:
                ev = threading.Event()
                self._events[key] = ev
            return ev

    def _ocr_and_store(self, book: str, page: str, version: int) -> OCRPage:
        """Decode + OCR one page and write its cache file."""
        path = page_path(self.settings.archive_root, book, page)
        src = decoder.decode(path)
        result = engine.ocr_image(
            src, self.settings.ocr_lang, self.settings.ocr_max_dim,
            self.settings.ocr_conf_threshold,
        )
        data = {
            "page_id": page,
            "width": src.shape[1],
            "height": src.shape[0],
            "version": version,
            "lines": result["lines"],
            "words": result["words"],
        }
        self._store(self._cache_path(book, page, version), data)
        return OCRPage(**data)

    # ------------------------------------------------------------- API

    def get_page_ocr(self, book: str, page: str) -> OCRPage:
        """Return a page's OCR, blocking until the background worker finishes it.

        Interactive single-page requests are prioritised ahead of bulk prefetch,
        so this returns as fast as possible even mid-search. Callers should run
        it via ``asyncio.to_thread`` so the event loop stays free.
        """
        version = self._page_version(book, page)
        path = self._cache_path(book, page, version)
        cached = self._load(path)
        if cached is not None:
            return OCRPage(**cached)

        key = self._key(book, page, version)
        self._enqueue(_PRIO_INTERACTIVE, book, page, version, key)
        self._event(key).wait()

        cached = self._load(path)
        if cached is not None:
            return OCRPage(**cached)
        return self._ocr_and_store(book, page, version)  # worker failed: do it inline

    def search(self, book: str, pages: list, query: str, regex: bool) -> tuple[list[SearchHit], int]:
        """Match cached OCR text, submitting missing pages to the worker.

        A page matches when the query appears in its text (line text joined).
        Multi-word literal queries highlight each of their terms; regex queries
        highlight every word the regex matches, falling back to whole matching
        lines when the regex spans words.

        Args:
            book: Book directory name.
            pages: The book's page records (from the catalog).
            query: Search text or regex pattern.
            regex: Treat ``query`` as a regular expression.

        Returns:
            ``(matches, pending)`` — matching pages found so far, and the count
            of pages still queued/uncached that may match later.
        """
        q = query.strip()
        if regex:
            rx = re.compile(q, re.IGNORECASE)
            page_match = lambda text: rx.search(text) is not None
            word_match = page_match
        else:
            ql = q.lower()
            terms = ql.split()
            page_match = lambda text: ql in text.lower()
            word_match = lambda text: any(t in text.lower() for t in terms)

        matches: list[SearchHit] = []
        pending = 0
        for p in pages:
            version = self._page_version(book, p.page_id)
            path = self._cache_path(book, p.page_id, version)
            cached = self._load(path)
            if cached is None:
                self._enqueue(_PRIO_BULK, book, p.page_id, version, self._key(book, p.page_id, version))
                pending += 1
                continue
            ocr = OCRPage(**cached)
            full_text = " ".join(line.text for line in ocr.lines)
            if not page_match(full_text):
                continue
            hits = [w for w in ocr.words if word_match(w.text)]
            if not hits:
                hits = [
                    OCRWord(x=ln.x, y=ln.y, w=ln.w, h=ln.h, text=ln.text, conf=0.0)
                    for ln in ocr.lines
                    if page_match(ln.text)
                ]
            if hits:
                matches.append(SearchHit(page_id=p.page_id, hits=hits))
        return matches, pending
