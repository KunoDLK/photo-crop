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
  constructor(onTile) {
    this.onTile = onTile; // (req, bitmap | null) callback
    this.inflight = new Set(); // keys currently being fetched
    this.queued = []; // { key, url, priority, imId, L, tx, ty }
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
      fetchTile(req.url)
        .then((bitmap) => {
          this.inflight.delete(req.key);
          this.onTile(req, bitmap);
          this._pump();
        })
        .catch(() => {
          this.inflight.delete(req.key);
          this.onTile(req, null);
          this._pump();
        });
    }
  }
}
