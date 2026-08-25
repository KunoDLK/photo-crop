/**
 * OCR text overlay with box selection.
 *
 * OCR text is rendered as transparent DOM spans positioned over the canvas. A
 * single CSS transform on an inner scene element maps scene coordinates to
 * device coordinates, so pan/zoom only updates one style property rather than
 * every span. Text selection is gated behind Ctrl: while held, the overlay
 * captures pointers and the canvas yields them. Dragging draws a marquee box
 * and releases select every OCR line whose box intersects it, copying their
 * text to the clipboard and highlighting the lines.
 */

import * as state from "../state.js";
import * as viewport from "../viewport.js";
import * as render from "../render.js";
import { fetchPageOcr } from "../api/ocr.js";
import { OCR_LOAD_MIN_PX, OCR_MIN_FONT } from "../config.js";

let viewEl = null;
let overlayEl = null;
let sceneEl = null;
let marqueeEl = null;
const loaded = new Map(); // image id -> OCR page data
const pending = new Set(); // image ids being fetched
let spans = []; // { im, line, el }
let dirtySpans = false;
let lastTransform = "";
let lastScale = -1;
let lastDebug = false;
let marquee = null; // { x0, y0, x1, y1 } in scene coords
let selectedLines = []; // OCR line objects of the focused image

/** Wire the overlay to the canvas and its DOM containers. Call once. */
export function init(deps) {
  viewEl = deps.viewEl;
  overlayEl = document.getElementById("ocr-overlay");
  sceneEl = document.getElementById("ocr-scene");
  marqueeEl = document.createElement("div");
  marqueeEl.id = "ocr-marquee";
  marqueeEl.hidden = true;
  overlayEl.appendChild(marqueeEl);
  installCtrlHandling();
  installMarquee();

  state.on("images-changed", () => {
    dirtySpans = true;
  });
  state.on("images-removed", (removed) => {
    for (const im of removed) loaded.delete(im.id);
    dirtySpans = true;
  });
  state.on("focus-changed", () => {
    selectedLines = [];
    dirtySpans = true;
  });
}

function installCtrlHandling() {
  window.addEventListener("keydown", (e) => {
    if (e.key === "Control" && !e.repeat) setSelectMode(true);
  });
  window.addEventListener("keyup", (e) => {
    if (e.key === "Control") setSelectMode(false);
  });
  window.addEventListener("blur", () => setSelectMode(false));
}

/** Drag-to-box selection while Ctrl is held (native text selection is disabled). */
function installMarquee() {
  overlayEl.addEventListener("pointerdown", (e) => {
    if (!state.textSelect) return;
    e.preventDefault();
    try { overlayEl.setPointerCapture(e.pointerId); } catch (err) { /* noop */ }
    const [sx, sy] = overlayPoint(e);
    marquee = { x0: sx, y0: sy, x1: sx, y1: sy };
    selectedLines = [];
    applySelectionHighlight();
    drawMarquee();
  });
  overlayEl.addEventListener("pointermove", (e) => {
    if (!marquee) return;
    const [sx, sy] = overlayPoint(e);
    marquee.x1 = sx;
    marquee.y1 = sy;
    drawMarquee();
  });
  overlayEl.addEventListener("pointerup", endMarquee);
  overlayEl.addEventListener("pointercancel", endMarquee);
}

/** Toggle the pointer-events split between the overlay and the canvas. */
function setSelectMode(on) {
  if (state.textSelect === on) return;
  if (!on) endMarquee(); // finalize an in-progress box before yielding pointers
  state.setTextSelect(on);
  if (overlayEl) {
    overlayEl.style.pointerEvents = on ? "auto" : "none";
    overlayEl.style.cursor = on ? "crosshair" : "";
  }
  if (viewEl) viewEl.style.pointerEvents = on ? "none" : "";
  state.emit("text-select", on);
}

/** The pointer position as scene coordinates relative to the overlay. */
function overlayPoint(e) {
  const rect = overlayEl.getBoundingClientRect();
  return viewport.devToScene(e.clientX - rect.left, e.clientY - rect.top);
}

/** Position the marquee element (device coords, so its border stays crisp). */
function drawMarquee() {
  if (!marquee || !marqueeEl) return;
  const [dx0, dy0] = viewport.sceneToDev(Math.min(marquee.x0, marquee.x1), Math.min(marquee.y0, marquee.y1));
  const [dx1, dy1] = viewport.sceneToDev(Math.max(marquee.x0, marquee.x1), Math.max(marquee.y0, marquee.y1));
  marqueeEl.hidden = false;
  marqueeEl.style.left = dx0 + "px";
  marqueeEl.style.top = dy0 + "px";
  marqueeEl.style.width = (dx1 - dx0) + "px";
  marqueeEl.style.height = (dy1 - dy0) + "px";
}

/** Finish a marquee drag: select the lines intersecting the box, then copy. */
function endMarquee() {
  if (!marquee) return;
  const m = marquee;
  marquee = null;
  if (marqueeEl) marqueeEl.hidden = true;
  // A click (no real drag) just clears the previous selection.
  if (Math.abs(m.x1 - m.x0) < 1 && Math.abs(m.y1 - m.y0) < 1) return;
  selectLinesInRect({
    x0: Math.min(m.x0, m.x1), y0: Math.min(m.y0, m.y1),
    x1: Math.max(m.x0, m.x1), y1: Math.max(m.y0, m.y1),
  });
}

/** Select every line of the focused image whose box intersects the rect. */
function selectLinesInRect(r) {
  const im = state.focusedImage;
  selectedLines = [];
  if (!im) return;
  const data = loaded.get(im.id);
  if (!data) return;
  for (const line of data.lines || []) {
    const lx0 = im.drawX + line.x * im.fitFactor;
    const ly0 = im.drawY + line.y * im.fitFactor;
    const lx1 = lx0 + line.w * im.fitFactor;
    const ly1 = ly0 + line.h * im.fitFactor;
    if (lx1 < r.x0 || lx0 > r.x1 || ly1 < r.y0 || ly0 > r.y1) continue;
    selectedLines.push(line);
  }
  applySelectionHighlight();
  copySelection(selectedLines);
}

/** Copy the selected lines' text (joined by newlines) to the clipboard. */
function copySelection(lines) {
  if (!lines.length) return;
  const text = lines.map((l) => l.text).join("\n");
  const done = () => state.setStatus(`Copied ${lines.length} OCR line(s)`);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

/** Clipboard API fallback for non-secure contexts. */
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch (err) { /* noop */ }
  document.body.removeChild(ta);
  done();
}

/** Toggle the highlight class on spans for the currently selected lines. */
function applySelectionHighlight() {
  const selected = new Set(selectedLines);
  for (const s of spans) s.el.classList.toggle("selected", selected.has(s.line));
}

/** Called from the render loop each frame: sync transform + lazy-load OCR. */
export function update() {
  if (!sceneEl) return;

  const { scale, vx, vy } = state.view;
  const t = `translate(${vx}px, ${vy}px) scale(${scale})`;
  if (t !== lastTransform) {
    lastTransform = t;
    sceneEl.style.transform = t;
  }

  ensureLoaded();

  if (state.tileDebug !== lastDebug) {
    lastDebug = state.tileDebug;
    dirtySpans = true;
  }
  if (dirtySpans) rebuildSpans();
  if (scale !== lastScale) {
    lastScale = scale;
    updateVisibility();
  }
}

/**
 * Fetch OCR lazily, and only where it matters: the selected image (once it is
 * large enough to read), or visible matched pages during a search. Pushing to
 * the screen happens on arrival: the fetch stores the data and marks the spans
 * dirty, so the next frame rebuilds them for the focused image.
 */
function ensureLoaded() {
  if (state.searchActive) {
    // Search mode: warm OCR for visible matched pages so their text overlay is
    // ready as soon as one of them is focused (a single-result search is
    // auto-selected immediately). Bounded by what is actually on screen.
    const vpw = state.viewport.w, vph = state.viewport.h;
    for (const im of state.images) {
      if (!im.searchHits) continue;
      if (im.status !== "ready") continue;
      if (!viewport.isImageVisible(im, vpw, vph)) continue;
      requestOcr(im);
    }
    return;
  }
  const im = state.focusedImage;
  if (!im || im.status !== "ready") return;
  if (im.drawW * state.view.scale < OCR_LOAD_MIN_PX) return;
  requestOcr(im);
}

/** Fetch one page's OCR once (deduped); push to screen when it arrives. */
function requestOcr(im) {
  if (loaded.has(im.id) || pending.has(im.id)) return;
  pending.add(im.id);
  fetchPageOcr(im.bookId, im.pageId)
    .then((data) => {
      pending.delete(im.id);
      loaded.set(im.id, data);
      // Ensure a frame renders the new spans even if tiles are quiet (e.g. the
      // page was already cached): otherwise the text waits for the next input.
      if (state.focusedImage && state.focusedImage.id === im.id) {
        dirtySpans = true;
        render.requestRender();
      }
    })
    .catch(() => {
      pending.delete(im.id);
    });
}

/** Rebuild the spans for the focused image only (bounds the DOM to one page). */
function rebuildSpans() {
  if (!sceneEl) return;
  sceneEl.textContent = "";
  spans = [];
  const im = state.focusedImage;
  if (im) {
    const data = loaded.get(im.id);
    if (data) {
      for (const line of data.lines || []) {
        const el = document.createElement("span");
        el.className = state.tileDebug ? "ocr-line ocr-debug" : "ocr-line";
        el.textContent = line.text;
        el.style.left = (im.drawX + line.x * im.fitFactor) + "px";
        el.style.top = (im.drawY + line.y * im.fitFactor) + "px";
        el.style.fontSize = (line.h * im.fitFactor) + "px";
        spans.push({ im, line, el });
        sceneEl.appendChild(el);
      }
    }
  }
  dirtySpans = false;
  applySelectionHighlight();
  updateVisibility();
}

/** Hide spans too small to select (avoids tiny/overlapping hit targets). */
function updateVisibility() {
  const sc = state.view.scale;
  for (const s of spans) {
    const onScreen = s.line.h * s.im.fitFactor * sc;
    s.el.style.display = onScreen < OCR_MIN_FONT ? "none" : "";
  }
}
