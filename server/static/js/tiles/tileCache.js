/**
 * Decoded-tile cache.
 *
 * Holds decoded ImageBitmaps keyed by `id:level:tx:ty`, byte-budgeted with LRU
 * eviction. The root tile (coarsest level covering the whole image) is pinned so
 * every image renders instantly on first appearance, per the tile rules.
 *
 * A per-image index (`byImage`) lets cleanup operate on the tiles that actually
 * exist for one image, instead of scanning every possible tile position (which
 * grows as 4^depth and is mostly empty).
 */

import { TILE, CACHE_MAX, CACHE_MAX_BYTES } from "../config.js";

const TILE_BYTES = TILE * TILE * 4;

export const tileKey = (id, level, tx, ty) => `${id}:${level}:${tx}:${ty}`;

/** Extract the image id from a tile key ("id:level:tx:ty" -> id). */
function keyId(key) {
  return Number(key.slice(0, key.indexOf(":")));
}

export class TileCache {
  constructor() {
    this.map = new Map();     // key -> ImageBitmap
    this.byImage = new Map(); // image id -> Set of keys (for fast per-image cleanup)
    this.last = new Map();    // key -> last-access clock (for LRU eviction)
    this.clock = 0;
    this.bytes = 0;
    this.pinned = new Set();  // root-tile keys, never evicted
  }

  /** Return a cached tile, refreshing its LRU position, or null. */
  get(key) {
    const img = this.map.get(key);
    if (img) this.last.set(key, ++this.clock);
    return img || null;
  }

  /** Store a decoded tile, evicting LRU (non-pinned) entries to fit the budget. */
  set(key, img) {
    const prev = this.map.get(key);
    if (prev === img) {
      this.last.set(key, ++this.clock);
      return;
    }
    if (prev && prev.close) prev.close();
    this.map.set(key, img);
    this.last.set(key, ++this.clock);
    let s = this.byImage.get(keyId(key));
    if (!s) { s = new Set(); this.byImage.set(keyId(key), s); }
    s.add(key);
    if (!prev) this.bytes += TILE_BYTES;
    this._evict();
  }

  /** True if the tile is present. */
  has(key) {
    return this.map.has(key);
  }

  /** Pin a root tile so it survives eviction for the image's lifetime. */
  pin(key) {
    this.pinned.add(key);
  }

  /** Unpin a tile (it becomes evictable again). */
  unpin(key) {
    this.pinned.delete(key);
  }

  /** Drop all tiles for an image (e.g. it left the listing). */
  dropImage(id) {
    const s = this.byImage.get(id);
    if (!s) return;
    for (const key of [...s]) this._remove(key);
  }

  /**
   * Remove every tile of an image except its pinned root (used when the image
   * scrolls off-screen: only the coarse, always-viewable root stays cached).
   */
  pruneImage(id) {
    const s = this.byImage.get(id);
    if (!s) return;
    for (const key of [...s]) {
      if (this.pinned.has(key)) continue;
      this._remove(key);
    }
  }

  /**
   * Prune an image down to its target level: keep the pinned root plus any tile
   * at exactly `level`, and drop everything else (finer tiles left over from a
   * deeper zoom, or coarser underlays that the target level now fully covers).
   */
  pruneToLevel(im, level) {
    const s = this.byImage.get(im.id);
    if (!s) return;
    for (const key of [...s]) {
      if (this.pinned.has(key)) continue;
      const lv = Number(key.split(":")[1]);
      if (lv !== level) this._remove(key);
    }
  }

  _remove(key) {
    const img = this.map.get(key);
    if (!img) return;
    if (img.close) img.close();
    this.map.delete(key);
    this.last.delete(key);
    this.pinned.delete(key);
    const s = this.byImage.get(keyId(key));
    if (s) { s.delete(key); if (s.size === 0) this.byImage.delete(keyId(key)); }
    this.bytes -= TILE_BYTES;
  }

  _evict() {
    while (this.map.size > CACHE_MAX || this.bytes > CACHE_MAX_BYTES) {
      let victim = null, oldest = Infinity;
      for (const [key, ts] of this.last) {
        if (this.pinned.has(key)) continue;
        if (ts < oldest) { oldest = ts; victim = key; }
      }
      if (victim === null) break;
      this._remove(victim);
    }
  }
}
