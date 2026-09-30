/**
 * View transform math.
 *
 * Owns the mapping between scene coordinates and device coordinates, plus fit
 * helpers. Pure functions over the shared view/scene state; no drawing.
 */

import { clamp } from "./util.js";
import { MAX_SCALE } from "./config.js";
import { view, scene, viewport as vp } from "./state.js";

/** Map scene coordinates to device (CSS px) coordinates. */
export function sceneToDev(x, y) {
  return [view.vx + x * view.scale, view.vy + y * view.scale];
}

/** Map device (CSS px) coordinates to scene coordinates. */
export function devToScene(mx, my) {
  return [(mx - view.vx) / view.scale, (my - view.vy) / view.scale];
}

/** The viewport rect in scene coordinates. */
export function visibleSceneRect(vpw, vph) {
  const [x0, y0] = devToScene(0, 0);
  const [x1, y1] = devToScene(vpw, vph);
  return { x0, y0, x1, y1 };
}

/**
 * The box a fit targets. Callers pass the canvas size, but on iPhone the canvas
 * is deliberately taller than the screen content area (it paints behind the
 * dynamic island and the translucent URL bar, see render.js): a fit then uses
 * the visible slice plus its offset, so pages stay fully on screen while the
 * canvas keeps painting edge to edge behind the bars. A caller that explicitly
 * passes its own size gets exactly that size back.
 */
export function fitBox(vpw = vp.w, vph = vp.h) {
  if (vph === vp.h && vp.visibleH && vp.visibleH < vp.h) {
    return { w: vpw, h: vp.visibleH, top: vp.visibleTop || 0 };
  }
  return { w: vpw, h: vph, top: 0 };
}

/**
 * The visible slice of the canvas in device (CSS px) coordinates: the part of
 * the full-screen canvas that no browser bar covers (see render.js). Drawing
 * and tile culling use the whole canvas; user-facing geometry — fits, the
 * "which page is at the centre" checks, the off-screen arrows — uses this.
 */
export function visibleRect() {
  const box = fitBox();
  return { x0: 0, y0: box.top, x1: box.w, y1: box.top + box.h };
}

/** Centre of the visible slice, in device (CSS px) coordinates. */
export function visibleCenter() {
  const box = fitBox();
  return { x: box.w / 2, y: box.top + box.h / 2 };
}

/** Fit the whole scene into the viewport (used for root and book fit). */
export function fitView(vpw, vph) {
  const box = fitBox(vpw, vph);
  if (!scene.w || !scene.h || !box.w || !box.h) {
    view.scale = 1;
    view.vx = 0;
    view.vy = 0;
    return;
  }
  view.fitScale = Math.min(box.w / scene.w, box.h / scene.h) * 0.96;
  view.scale = view.fitScale;
  view.vx = (box.w - scene.w * view.scale) / 2;
  view.vy = box.top + (box.h - scene.h * view.scale) / 2;
}

/** Fit a single image's draw rect into the viewport, centered. */
export function fitViewToImage(im, vpw, vph) {
  const box = fitBox(vpw, vph);
  if (!box.w || !box.h) return;
  const cx = im.drawX + im.drawW / 2;
  const cy = im.drawY + im.drawH / 2;
  const s = Math.min(box.w / im.drawW, box.h / im.drawH) * 0.96;
  view.scale = clamp(s, 0.00005, MAX_SCALE);
  view.vx = box.w / 2 - cx * view.scale;
  view.vy = box.top + box.h / 2 - cy * view.scale;
}

/** True if an image's cell intersects the viewport. */
export function isImageVisible(im, vpw, vph) {
  const { x0, y0, x1, y1 } = visibleSceneRect(vpw, vph);
  const cx0 = im.cellX, cy0 = im.labelY;
  const cx1 = im.cellX + im.cell, cy1 = im.cellY + im.cell;
  return !(cx1 < x0 || cx0 > x1 || cy1 < y0 || cy0 > y1);
}
