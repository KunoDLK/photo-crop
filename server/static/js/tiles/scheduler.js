/**
 * Tile scheduler: decides what to request and enforces the budget + lifecycle.
 *
 * It recomputes the desired tile set for the current view and:
 *   - prefetches/pins each visible image's root tile (instant first paint);
 *   - on zoom-in, keeps a parent tile until all four children are cached;
 *   - on zoom-out, keeps fine tiles until the covering coarse tile arrives, then
 *     unloads all its descendants (any depth);
 *   - limits zoom-in requests so requested + rendered tiles stay within
 *     MAX_DISPLAYED_TILES (coarse zoom-out tiles are fetched freely since each
 *     arrival frees its fine descendants);
 *   - refines each area progressively: only tiles at most one step finer than
 *     the finest cached ancestor are requested, so freshly revealed areas fill
 *     in coarse-to-fine (e.g. L6 -> L5 -> L4 -> ... -> target) instead of
 *     waiting on the full target sweep;
 *   - prioritizes the areas showing the coarsest tile first (worst visible
 *     quality wins), then nearest the cursor/centre.
 */

import { TILE, MAX_DISPLAYED_TILES, PREFETCH_NEIGHBORS, PROGRESSIVE_STEP } from "../config.js";
import * as state from "../state.js";
import * as viewport from "../viewport.js";
import { tileRange, tileCovered } from "../compositor.js";
import { tileKey } from "./tileCache.js";
import { chooseLevels } from "./levelSelect.js";
import { tileUrl } from "../api/tiles.js";

let cache = null;
let queue = null;
let requestRender = null;
let prevK = null; // global coarsening offset from the previous settle (for zoom dir)

/** Provide the dependencies the scheduler drives. Call once at startup. */
export function init(deps) {
  cache = deps.cache;
  queue = deps.queue;
  requestRender = deps.requestRender;
}

/** Forget the previous budget offset (called when the location changes). */
export function resetLevels() {
  prevK = null;
}

function findImage(id) {
  return state.images.find((im) => im.id === id) || null;
}

/** Drop queued (not-yet-sent) requests when navigation supersedes them. */
export function cancelQueued() {
  queue.cancelQueued();
}

/** Visible images ordered nearest-first from the cursor (or viewport centre). */
function visibleImages() {
  const vpw = state.viewport.w, vph = state.viewport.h;
  const ox = state.cursor.x >= 0 ? state.cursor.x : vpw / 2;
  const oy = state.cursor.y >= 0 ? state.cursor.y : vph / 2;
  const out = [];
  for (const im of state.images) {
    if (im.status === "error") continue;
    if (!viewport.isImageVisible(im, vpw, vph)) continue;
    const [cx, cy] = viewport.sceneToDev(im.cellX + im.cell / 2, im.labelY + im.cell / 2);
    const d2 = (cx - ox) * (cx - ox) + (cy - oy) * (cy - oy);
    out.push({ im, d2 });
  }
  out.sort((a, b) => a.d2 - b.d2);
  return out.map((o) => o.im);
}

/** Prefetch and pin the root tile of an image (rule 7). */
export function ensureRootTile(im) {
  const L = im.maxLevel;
  const key = tileKey(im.id, L, 0, 0);
  if (cache.has(key)) return;
  if (queue.has(key)) return;
  queue.request({
    key,
    url: tileUrl(im.bookId, im.pageId, im.version, L, 0, 0),
    priority: 0,
    imId: im.id,
    L,
    tx: 0,
    ty: 0,
  });
}

/**
 * The tiles to request for an image: for every level from one below the root
 * down to the target, request the visible tiles that are the next refinement
 * step for their area (finest cached ancestor at most PROGRESSIVE_STEP levels
 * coarser) and are not yet covered by finer cached tiles. Each request records
 * the coarseness `A` of the tile currently covering its area so the caller can
 * prioritize the areas showing the lowest-quality tile first.
 */
export function nextStepTiles(im) {
  const L = im.targetLevel == null ? im.maxLevel : im.targetLevel;
  if (L >= im.maxLevel || imageComplete(im)) return [];
  const vpw = state.viewport.w, vph = state.viewport.h;
  const sc = state.view.scale;
  const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
  const dw = im.drawW * sc, dh = im.drawH * sc;
  const ox = state.cursor.x >= 0 ? state.cursor.x : vpw / 2;
  const oy = state.cursor.y >= 0 ? state.cursor.y : vph / 2;

  const list = [];
  for (let M = im.maxLevel - 1; M >= L; M -= PROGRESSIVE_STEP) {
    const r = tileRange(im, M, sc, dx, dy, dw, dh, vpw, vph);
    if (r.tx0 > r.tx1 || r.ty0 > r.ty1) continue;
    const twsc = TILE * Math.pow(2, M) * im.fitFactor * sc;
    for (let ty = r.ty0; ty <= r.ty1; ty++) {
      for (let tx = r.tx0; tx <= r.tx1; tx++) {
        const key = tileKey(im.id, M, tx, ty);
        if (cache.has(key)) continue;
        if (queue.has(key)) continue;
        if (tileCovered(im, M, tx, ty, L, sc, dx, dy, dw, dh)) continue;
        const A = finestCachedAncestor(im, M, tx, ty);
        if (A > M + PROGRESSIVE_STEP) continue; // deeper step; wait for the parent
        const cx = dx + (tx + 0.5) * twsc;
        const cy = dy + (ty + 0.5) * twsc;
        const d2 = (cx - ox) * (cx - ox) + (cy - oy) * (cy - oy);
        list.push({ key, url: tileUrl(im.bookId, im.pageId, im.version, M, tx, ty), priority: d2, A, imId: im.id, L: M, tx, ty });
      }
    }
  }
  return list;
}

/**
 * Coarseness of the finest cached tile covering (tx, ty) at `level`: walk up
 * the ancestors until one is cached. The root is always cached for a ready
 * image, so the walk terminates; a still-loading image falls back to the root.
 */
function finestCachedAncestor(im, level, tx, ty) {
  let cx = tx, cy = ty;
  for (let lv = level + 1; lv <= im.maxLevel; lv++) {
    cx = Math.floor(cx / 2);
    cy = Math.floor(cy / 2);
    if (cache.has(tileKey(im.id, lv, cx, cy))) return lv;
  }
  return im.maxLevel;
}

/** Count cached tiles the compositor would draw right now (all visible levels). */
export function onScreenCachedCount() {
  const vpw = state.viewport.w, vph = state.viewport.h;
  const sc = state.view.scale;
  let n = 0;
  for (const im of state.images) {
    if (im.status === "error") continue;
    if (!viewport.isImageVisible(im, vpw, vph)) continue;
    const L = im.targetLevel == null ? im.maxLevel : im.targetLevel;
    const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
    const dw = im.drawW * sc, dh = im.drawH * sc;
    for (let lv = im.maxLevel; lv >= L; lv--) {
      const r = tileRange(im, lv, sc, dx, dy, dw, dh, vpw, vph);
      if (r.tx0 > r.tx1 || r.ty0 > r.ty1) continue;
      for (let ty = r.ty0; ty <= r.ty1; ty++) {
        for (let tx = r.tx0; tx <= r.tx1; tx++) {
          if (tileCovered(im, lv, tx, ty, L, sc, dx, dy, dw, dh)) continue;
          if (cache.has(tileKey(im.id, lv, tx, ty))) n++;
        }
      }
    }
  }
  return n;
}

/** Tiles currently committed (rendered on screen + requested). */
export function committedCount() {
  return onScreenCachedCount() + queue.inflightCount + queue.queuedCount;
}

/** Recompute the desired tile set and drive the queue. */
export function reconcile() {
  if (!queue) return;
  // Rebuild the request set from scratch: drop anything queued for a previous
  // view so stale off-screen tiles are never fetched. In-flight requests are
  // left to finish (their results are still useful or harmless).
  queue.cancelQueued();
  const visible = visibleImages();

  // Prune tiles of images that scrolled off-screen: keep only their pinned root
  // (the always-viewable coarse tile) so fine detail is freed and reloaded when
  // the image returns.
  const visibleIds = new Set(visible.map((im) => im.id));
  for (const im of state.images) {
    if (!visibleIds.has(im.id)) cache.pruneImage(im.id);
  }

  // Rule 7: every visible image always has its root tile requested.
  for (const im of visible) ensureRootTile(im);

  // Global quality budget: one comparable level across all visible images, with
  // the same coarsening offset K applied to every image (closest under MAX).
  const K = chooseLevels(visible);
  const zoomIn = prevK != null && K < prevK;
  prevK = K;

  // Once an image has fully loaded its target level, prune any tiles not at that
  // level — finer leftovers from a deeper zoom, and coarser underlays the target
  // now fully covers — keeping only the target level plus the pinned root.
  for (const im of visible) {
    if (imageComplete(im)) cache.pruneToLevel(im, im.targetLevel);
  }

  // Build one priority-ordered list across all visible images: areas showing
  // the coarsest tile first (worst visible quality), then nearest-first.
  const desired = [];
  for (const im of visible) {
    for (const req of nextStepTiles(im)) desired.push(req);
  }
  desired.sort((a, b) => (b.A - a.A) || (a.priority - b.priority));

  // Lazy: only count on-screen cached tiles when zoom-in actually needs the
  // budget check (during pan/zoom-out it is unnecessary and would just cost CPU).
  let rendered = -1;
  for (const req of desired) {
    if (queue.has(req.key)) continue;
    // Zoom-in is budget-limited; zoom-out/pan request freely (each coarse tile
    // frees its fine descendants when it arrives).
    if (zoomIn) {
      if (rendered < 0) rendered = onScreenCachedCount();
      if (rendered + queue.inflightCount + queue.queuedCount >= MAX_DISPLAYED_TILES) break;
    }
    queue.request(req);
  }

  prefetchNeighbors(visible);
}

/** Warm root tiles of images adjacent to the view while idle. */
function prefetchNeighbors(visible) {
  if (!visible.length) return;
  if (queue.inflightCount || queue.queuedCount) return;
  const idxs = visible.map((im) => state.images.indexOf(im)).filter((i) => i >= 0);
  const lo = Math.max(0, Math.min(...idxs) - PREFETCH_NEIGHBORS);
  const hi = Math.min(state.images.length - 1, Math.max(...idxs) + PREFETCH_NEIGHBORS);
  for (let i = lo; i <= hi; i++) {
    const im = state.images[i];
    if (im.status === "error") continue;
    ensureRootTile(im);
  }
}

/** Handle a tile that just arrived (or failed) from the fetch queue. */
export function handleTile(req, bitmap) {
  const im = findImage(req.imId);
  if (!im) {
    if (bitmap && bitmap.close) bitmap.close();
    return;
  }
  if (!bitmap) {
    if (req.L === im.maxLevel && req.tx === 0 && req.ty === 0) im.status = "error";
    requestRender();
    return;
  }

  const key = tileKey(im.id, req.L, req.tx, req.ty);
  cache.set(key, bitmap);
  if (req.L === im.maxLevel && req.tx === 0 && req.ty === 0) {
    cache.pin(key);
    im.status = "ready";
  }

  requestRender();
  reconcile();
}

/** True when every visible tile of the image's target level is cached. */
function imageComplete(im) {
  const L = im.targetLevel == null ? im.maxLevel : im.targetLevel;
  const sc = state.view.scale;
  const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
  const dw = im.drawW * sc, dh = im.drawH * sc;
  const r = tileRange(im, L, sc, dx, dy, dw, dh);
  for (let ty = r.ty0; ty <= r.ty1; ty++) {
    for (let tx = r.tx0; tx <= r.tx1; tx++) {
      if (!cache.has(tileKey(im.id, L, tx, ty))) return false;
    }
  }
  return true;
}
