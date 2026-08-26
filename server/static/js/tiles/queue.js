/**
 * Prioritized tile fetch queue.
 *
 * Manages in-flight HTTP requests and a priority queue ordered nearest-first from
 * the cursor/centre. It only performs network I/O — it does not decide what
 * should be requested (that is the scheduler) nor where to draw it.
 */

import { MAX_INFLIGHT } from "../config.js";
import { fetchTile } from "../api/tiles.js";

export class TileQueue {
  constructor(onTile, onDropped = () => {}) {
    this.onTile = onTile; // (req, bitmap | null) callback
    this.onDropped = onDropped; // called after a stale response is discarded
    this.inflight = new Set(); // keys currently being fetched
    this.queued = []; // { key, url, priority, imId, L, tx, ty }
    this.stats = { total: 0, hits: 0 }; // cache-hit tally for the debug bar
    this.epoch = 0; // bumped on identity change; stale responses are discarded
  }

  /**
   * Invalidate every request issued before now (in-flight and queued).
   *
   * Called when the viewer identity changes (login/logout): each image's
   * access variant flips, so a blur/real tile fetched under the old session
   * must never land in the decoded cache under the new one. Queued requests
   * are dropped outright; in-flight ones are allowed to finish on the wire but
   * their bitmaps are closed and discarded when they arrive (the scheduler is
   * notified so it re-requests the tiles under the current variant).
   */
  invalidate() {
    this.epoch++;
    this.queued.length = 0;
  }

  /** Reset the cache-hit tally (called when a fetch burst begins). */
  resetStats() {
    this.stats.total = 0;
    this.stats.hits = 0;
  }

  /** True if a key is already queued or in flight. */
  has(key) {
    return this.inflight.has(key) || this.queued.some((q) => q.key === key);
  }

  /** Request a tile; dedupes inflight/queued keys. */
  request(req) {
    if (this.inflight.has(req.key)) return;
    if (this.queued.some((q) => q.key === req.key)) return;
    this.queued.push(req);
    this._pump();
  }

  /** Drop queued (not yet sent) requests. */
  cancelQueued() {
    this.queued.length = 0;
  }

  /** Number of in-flight requests. */
  get inflightCount() {
    return this.inflight.size;
  }

  /** Number of queued (not yet sent) requests. */
  get queuedCount() {
    return this.queued.length;
  }

  /** Drain queued requests up to MAX_INFLIGHT, highest priority first. */
  _pump() {
    while (this.inflight.size < MAX_INFLIGHT && this.queued.length) {
      // Highest priority = lowest priority value; nearest-first order.
      let best = 0;
      for (let i = 1; i < this.queued.length; i++) {
        if (this.queued[i].priority < this.queued[best].priority) best = i;
      }
      const req = this.queued.splice(best, 1)[0];
      this.inflight.add(req.key);
      const epoch = this.epoch;
      const stale = () => epoch !== this.epoch;
      fetchTile(req.url)
        .then(({ bitmap, hit }) => {
          this.inflight.delete(req.key);
          if (stale()) {
            // Identity changed while this tile was on the wire; its variant is
            // wrong for the new session, so never cache it. Wake the scheduler
            // so the tile is re-requested under the current variant.
            if (bitmap && bitmap.close) bitmap.close();
            this.onDropped();
            this._pump();
            return;
          }
          this.stats.total++;
          if (hit) this.stats.hits++;
          this.onTile(req, bitmap);
          this._pump();
        })
        .catch(() => {
          this.inflight.delete(req.key);
          if (stale()) {
            this.onDropped();
          } else {
            this.stats.total++;
            this.onTile(req, null);
          }
          this._pump();
        });
    }
  }
}
