/**
 * Pointer / wheel / pinch input handling.
 *
 * Translates gestures into view transform changes, tracks the cursor (used for
 * tile priority), and raises the interacting/settle lifecycle. It only mutates
 * state and requests renders — actual tile work happens in the scheduler.
 */

import * as state from "./state.js";
import { clamp } from "./util.js";
import { SETTLE_MS } from "./config.js";
import * as render from "./render.js";

let scheduler = null;
let nav = null;
let settleTimer = null;
let panning = null;
const activePointers = new Map();
let pinch = null;
let downInfo = null; // { x, y, t } for click detection
let lastClick = { t: 0 };

/** Provide the scheduler/nav used on settle and activation. Call once. */
export function init(deps) {
  scheduler = deps.scheduler;
  nav = deps.nav;
}

/** Mark interaction active and (re)arm the settle timer. */
export function markInteracting() {
  state.setInteracting(true);
  if (settleTimer) clearTimeout(settleTimer);
  if (scheduler) scheduler.cancelQueued();
  settleTimer = setTimeout(() => {
    settleTimer = null;
    state.setInteracting(false);
    if (scheduler) scheduler.reconcile();
    render.requestRender();
  }, SETTLE_MS);
  if (scheduler) scheduler.reconcile();
}

/** Attach all input listeners to the canvas. Call once at startup. */
export function installInteraction(viewEl) {
  viewEl.addEventListener("wheel", onWheel, { passive: false });
  viewEl.addEventListener("pointerdown", onPointerDown);
  viewEl.addEventListener("pointermove", onPointerMove);
  viewEl.addEventListener("pointerup", onPointerUp);
  viewEl.addEventListener("pointercancel", onPointerUp);
  viewEl.addEventListener("contextmenu", onContextMenu);
  viewEl.addEventListener("pointerleave", () => {
    state.cursor.x = -1;
    state.cursor.y = -1;
  });
}

function onWheel(e) {
  e.preventDefault();
  const rect = e.currentTarget.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const f = Math.exp(-e.deltaY * 0.0012);
  const ns = clamp(state.view.scale * f, 0.00005, 64);
  const wx = (mx - state.view.vx) / state.view.scale;
  const wy = (my - state.view.vy) / state.view.scale;
  state.view.vx = mx - wx * ns;
  state.view.vy = my - wy * ns;
  state.view.scale = ns;
  markInteracting();
  render.requestRender();
}

function onPointerDown(e) {
  if (e.button !== 0) return;
  try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
  const rect = e.currentTarget.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  state.cursor.x = mx;
  state.cursor.y = my;
  downInfo = { x: mx, y: my, t: performance.now(), pointerType: e.pointerType };

  if (e.pointerType === "touch") {
    activePointers.set(e.pointerId, { x: mx, y: my });
    if (activePointers.size === 2) { beginPinch(); panning = null; return; }
    if (activePointers.size > 2) return;
  }
  panning = { wx: (mx - state.view.vx) / state.view.scale, wy: (my - state.view.vy) / state.view.scale };
}

function onPointerMove(e) {
  const rect = e.currentTarget.getBoundingClientRect();
  state.cursor.x = e.clientX - rect.left;
  state.cursor.y = e.clientY - rect.top;

  if (e.pointerType === "touch" && activePointers.has(e.pointerId)) {
    activePointers.set(e.pointerId, { x: state.cursor.x, y: state.cursor.y });
    if (pinch && activePointers.size >= 2) { updatePinch(); return; }
  }
  if (!panning) return;
  state.view.vx = state.cursor.x - panning.wx * state.view.scale;
  state.view.vy = state.cursor.y - panning.wy * state.view.scale;
  markInteracting();
  render.requestRender();
}

function onPointerUp(e) {
  if (e.pointerType === "touch" && activePointers.has(e.pointerId)) {
    if (pinch) {
      activePointers.delete(e.pointerId);
      pinch = null;
      if (activePointers.size === 1) {
        const only = activePointers.values().next().value;
        panning = { wx: (only.x - state.view.vx) / state.view.scale, wy: (only.y - state.view.vy) / state.view.scale };
      }
      return;
    }
    activePointers.delete(e.pointerId);
  }

  // Touch taps tolerate more movement (finger jitter) and a slightly longer
  // press than mouse clicks.
  const slop = downInfo && downInfo.pointerType === "touch" ? 12 : 5;
  const wasClick = downInfo
    && Math.abs(state.cursor.x - downInfo.x) < slop
    && Math.abs(state.cursor.y - downInfo.y) < slop
    && performance.now() - downInfo.t < 400;

  if (wasClick) {
    handleClick(downInfo.x, downInfo.y);
  }
  downInfo = null;
  panning = null;
}

function handleClick(mx, my) {
  // Book badge (top-right "Open →") takes priority.
  for (let i = state.images.length - 1; i >= 0; i--) {
    const im = state.images[i];
    if (im.kind !== "book" || !im.badgeRect) continue;
    const b = im.badgeRect;
    if (mx >= b.x0 && mx <= b.x1 && my >= b.y0 && my <= b.y1) {
      nav.handleCellActivate(im);
      return;
    }
  }

  const now = performance.now();
  const isDouble = now - lastClick.t < 350;
  lastClick.t = now;
  if (!isDouble) return;

  const im = imageAtPoint(mx, my);
  if (im) nav.handleCellActivate(im);
}

function imageAtPoint(mx, my) {
  const sx = (mx - state.view.vx) / state.view.scale;
  const sy = (my - state.view.vy) / state.view.scale;
  for (let i = state.images.length - 1; i >= 0; i--) {
    const im = state.images[i];
    if (sx >= im.cellX && sx <= im.cellX + im.cell &&
        sy >= im.labelY && sy <= im.cellY + im.cell) return im;
  }
  return null;
}

function onContextMenu(e) {
  e.preventDefault();
  const rect = e.currentTarget.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const im = imageAtPoint(mx, my);
  if (im) nav.handleFitImage(im);
}

function beginPinch() {
  const [a, b] = [...activePointers.values()];
  const dist = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  pinch = {
    startDist: dist,
    startScale: state.view.scale,
    startVx: state.view.vx,
    startVy: state.view.vy,
    midX: (a.x + b.x) / 2,
    midY: (a.y + b.y) / 2,
  };
}

function updatePinch() {
  const [a, b] = [...activePointers.values()];
  if (!a || !b) return;
  const dist = Math.hypot(b.x - a.x, b.y - a.y) || 1;
  const midX = (a.x + b.x) / 2, midY = (a.y + b.y) / 2;
  const ns = clamp(pinch.startScale * (dist / pinch.startDist), 0.00005, 64);
  const wx = (pinch.midX - pinch.startVx) / pinch.startScale;
  const wy = (pinch.midY - pinch.startVy) / pinch.startScale;
  state.view.scale = ns;
  state.view.vx = midX - wx * ns;
  state.view.vy = midY - wy * ns;
  markInteracting();
  render.requestRender();
}
