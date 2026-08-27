/**
 * View transform math.
 *
 * Owns the mapping between scene coordinates and device coordinates, plus fit
 * helpers. Pure functions over the shared view/scene state; no drawing.
 */

import { clamp } from "./util.js";
import { MAX_SCALE } from "./config.js";
import { view, scene } from "./state.js";

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

/** Fit the whole scene into the viewport (used for root and book fit). */
export function fitView(vpw, vph) {
  if (!scene.w || !scene.h || !vpw || !vph) {
    view.scale = 1;
    view.vx = 0;
    view.vy = 0;
    return;
  }
  view.fitScale = Math.min(vpw / scene.w, vph / scene.h) * 0.96;
  view.scale = view.fitScale;
  view.vx = (vpw - scene.w * view.scale) / 2;
  view.vy = (vph - scene.h * view.scale) / 2;
}

/** Fit a single image's draw rect into the viewport, centered. */
export function fitViewToImage(im, vpw, vph) {
  if (!vpw || !vph) return;
  const cx = im.drawX + im.drawW / 2;
  const cy = im.drawY + im.drawH / 2;
  const s = Math.min(vpw / im.drawW, vph / im.drawH) * 0.96;
  view.scale = clamp(s, 0.00005, MAX_SCALE);
  view.vx = vpw / 2 - cx * view.scale;
  view.vy = vph / 2 - cy * view.scale;
}

/** True if an image's cell intersects the viewport. */
export function isImageVisible(im, vpw, vph) {
  const { x0, y0, x1, y1 } = visibleSceneRect(vpw, vph);
  const cx0 = im.cellX, cy0 = im.labelY;
  const cx1 = im.cellX + im.cell, cy1 = im.cellY + im.cell;
  return !(cx1 < x0 || cx0 > x1 || cy1 < y0 || cy0 > y1);
}
