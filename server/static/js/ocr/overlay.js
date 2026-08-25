/**
 * Invisible selectable-text overlay.
 *
 * OCR text is rendered as transparent DOM spans positioned over the canvas so
 * the user can Ctrl+drag to select and copy it. A single CSS transform on an
 * inner scene element maps scene coordinates to device coordinates, so pan/zoom
 * only updates one style property rather than every span. Selection is gated
 * behind Ctrl: while held, the overlay captures pointers and the canvas yields
 * them so the drag selects text instead of panning.
 */

import * as state from "../state.js";
import * as viewport from "../viewport.js";
import * as render from "../render.js";
import { fetchPageOcr } from "../api/ocr.js";
import { OCR_LOAD_MIN_PX, OCR_MIN_FONT } from "../config.js";

let viewEl = null;
let overlayEl = null;
let sceneEl = null;
const loaded = new Map(); // image id -> OCR page data
const pending = new Set(); // image ids being fetched
let spans = []; // { im, line, el }
let dirtySpans = false;
let lastTransform = "";
let lastScale = -1;
let lastDebug = false;

/** Wire the overlay to the canvas and its DOM containers. Call once. */
export function init(deps) {
  viewEl = deps.viewEl;
  overlayEl = document.getElementById("ocr-overlay");
  sceneEl = document.getElementById("ocr-scene");
  installCtrlHandling();

  state.on("images-changed", () => {
    dirtySpans = true;
  });
  state.on("images-removed", (removed) => {
    for (const im of removed) loaded.delete(im.id);
    dirtySpans = true;
  });
  state.on("focus-changed", () => {
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

/** Toggle the pointer-events split between the overlay and the canvas. */
function setSelectMode(on) {
  if (state.textSelect === on) return;
  state.setTextSelect(on);
  if (overlayEl) overlayEl.style.pointerEvents = on ? "auto" : "none";
  if (viewEl) viewEl.style.pointerEvents = on ? "none" : "";
  state.emit("text-select", on);
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
