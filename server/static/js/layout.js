/**
 * Grid layout for the current location.
 *
 * Places each image (book cover or page) into the square-cell grid, groups by
 * ``group``, computes per-image draw rects, and derives scene bounds. Input
 * dimensions come from the API (no decoding needed).
 */

import { CELL, CELL_GAP, GROUP_GAP, LABEL_H } from "./config.js";
import * as state from "./state.js";

let nextId = 1;

/** Stable identity for reuse across reloads (preserves cached tiles). */
function stableKey(it) {
  return it.kind === "book" ? "b:" + it.bookId : "p:" + it.bookId + ":" + it.pageId;
}

/**
 * Rebuild the layout from listing records.
 *
 * Reuses existing image objects (by stable key) so cached tiles survive reloads;
 * drops images no longer present. Sets `state.images`, `state.scene`, and emits
 * "images-changed" (plus "images-removed" with the dropped images).
 *
 * @param {Array} items  Normalized records: { kind, bookId, pageId, name,
 *                        group, order, iw, ih, maxLevel }
 */
export function buildLayout(items) {
  const existingByKey = new Map();
  for (const im of state.images) existingByKey.set(im.stableKey, im);

  const groups = new Map();
  for (const it of items) {
    if (!groups.has(it.group)) groups.set(it.group, []);
    groups.get(it.group).push(it);
  }
  const gkeys = [...groups.keys()].sort((a, b) => a - b);

  let y = 0;
  let maxRight = 0;
  const next = [];
  const kept = new Set();

  for (const gk of gkeys) {
    const list = groups.get(gk).sort(compareOrder);
    const cols = Math.max(1, Math.ceil(Math.sqrt(list.length)));
    const rows = Math.ceil(list.length / cols);
    for (let i = 0; i < list.length; i++) {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const ox = col * (CELL + CELL_GAP);
      const oy = y + row * (CELL + LABEL_H + CELL_GAP);
      const it = list[i];
      const key = stableKey(it);
      let im = existingByKey.get(key);
      // Content changed (same name, new mtime): recreate so cached tiles are
      // dropped and re-fetched under the new version. The old object falls out
      // of `kept` and is cleaned up by the removed-images handling.
      if (im && im.version !== it.version) {
        existingByKey.delete(key);
        im = null;
      }
      if (im) {
        im.kind = it.kind;
        im.bookId = it.bookId;
        im.pageId = it.pageId;
        im.name = it.name;
        im.group = it.group;
        im.order = it.order;
        im.iw = it.iw;
        im.ih = it.ih;
        im.maxLevel = it.maxLevel;
        im.version = it.version;
        im.cellX = ox;
        im.cellY = oy + LABEL_H;
        im.cell = CELL;
        im.labelY = oy;
      } else {
        im = {
          id: nextId++,
          stableKey: key,
          kind: it.kind,
          bookId: it.bookId,
          pageId: it.pageId,
          name: it.name,
          group: it.group,
          order: it.order,
          iw: it.iw,
          ih: it.ih,
          maxLevel: it.maxLevel,
          version: it.version,
          fitFactor: 1,
          cellX: ox,
          cellY: oy + LABEL_H,
          cell: CELL,
          labelY: oy,
          drawX: ox,
          drawY: oy + LABEL_H,
          drawW: CELL,
          drawH: CELL,
          status: "idle",
          targetLevel: null,
          badgeRect: null,
        };
        existingByKey.set(key, im);
      }
      fitImage(im);
      next.push(im);
      kept.add(im.id);
      maxRight = Math.max(maxRight, ox + CELL);
    }
    y += rows * (CELL + LABEL_H + CELL_GAP) + GROUP_GAP;
  }

  const removed = state.images.filter((im) => !kept.has(im.id));
  state.setImages(next);
  state.scene.w = maxRight;
  state.scene.h = Math.max(0, y - GROUP_GAP);
  state.setHasImages(next.length > 0);
  state.emit("images-changed");
  if (removed.length) state.emit("images-removed", removed);
  return next;
}

/**
 * Compare page orders like the server's lexicographic convention. Orders are
 * zero-padded and may carry a letter suffix ("064", "064A"), so "064A" sorts
 * right after "064"; comparing the leading digits first also keeps unpadded
 * synthetic orders (book indices "1".."12") in numeric sequence.
 */
function compareOrder(a, b) {
  const an = parseInt(a.order, 10) || 0;
  const bn = parseInt(b.order, 10) || 0;
  if (an !== bn) return an - bn;
  return a.order < b.order ? -1 : a.order > b.order ? 1 : 0;
}

/** Fit a decoded image inside its square cell, preserving aspect. */
function fitImage(im) {
  const s = Math.min(im.cell / im.iw, im.cell / im.ih);
  im.fitFactor = s;
  im.drawW = im.iw * s;
  im.drawH = im.ih * s;
  im.drawX = im.cellX + (im.cell - im.drawW) / 2;
  im.drawY = im.cellY + (im.cell - im.drawH) / 2;
}
