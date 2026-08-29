/**
 * OCR text overlay with box selection.
 *
 * OCR text is rendered as transparent DOM spans positioned over the canvas. A
 * single CSS transform on an inner scene element maps scene coordinates to
 * device coordinates, so pan/zoom only updates one style property rather than
 * every span. Text selection is gated behind Ctrl: while held, the overlay
 * captures pointers and the canvas yields them. Dragging draws a marquee box
 * and releases select every OCR line of the image the drag started on whose
 * box intersects it, copying their text to the clipboard and highlighting the
 * lines.
 */

import * as state from "../state.js";
import * as viewport from "../viewport.js";
import * as render from "../render.js";
import { fetchPageOcr } from "../api/ocr.js";
import { OCR_MIN_FONT } from "../config.js";

let viewEl = null;
let overlayEl = null;
let sceneEl = null;
let marqueeEl = null;
const loaded = new Map(); // image id -> OCR page data
const pending = new Set(); // image ids being fetched
const suppressed = new Set(); // ids a drag unloaded; stale in-flight results drop
let spans = []; // { im, line, el }
let dirtySpans = false;
let lastTransform = "";
let lastScale = -1;
let lastDebug = false;
let marquee = null; // { x0, y0, x1, y1 } in scene coords
let marqueeImage = null; // the image the drag started on (selection source)
let selectedLines = []; // OCR line objects of the selection source image

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
  state.on("focus-changed", (im) => {
    selectedLines = [];
    // OCR is a one-page overlay: text follows the focused image, so switching
    // pages (arrows through search results, clicking another match) unloads
    // every other page's text instead of accumulating overlays.
    for (const other of state.images) {
      if (other === im) continue;
      loaded.delete(other.id);
    }
    dirtySpans = true;
  });
  // Keep OCR lifetime in lockstep with the tile cache: when an image's tiles
  // are pruned (it scrolled off-screen), its text unloads too.
  state.on("tiles-pruned", (ids) => {
    for (const id of ids) loaded.delete(id);
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
    // The selection reads from the image the drag started on (falling back to
    // the focused one). Selecting text on a page makes it the current one, and
    // loading its OCR unloads every other page's text; in-flight results for
    // those pages are suppressed so they can't resurrect.
    marqueeImage = imageAtPoint(sx, sy);
    if (marqueeImage && marqueeImage.kind === "page") {
      state.setFocusedImage(marqueeImage);
      // Drop every other page's text, loaded or merely in flight: suppress all
      // other current images so stale arrivals can't resurrect them.
      for (const other of state.images) {
        if (other.id === marqueeImage.id) continue;
        loaded.delete(other.id);
        suppressed.add(other.id);
      }
      pending.delete(marqueeImage.id); // a stale fetch must not block the new load
      requestOcr(marqueeImage);
    }
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
  const src = marqueeImage || state.focusedImage;
  marquee = null;
  marqueeImage = null;
  if (marqueeEl) marqueeEl.hidden = true;
  // A click (no real drag) just clears the previous selection.
  if (Math.abs(m.x1 - m.x0) < 1 && Math.abs(m.y1 - m.y0) < 1) return;
  selectLinesInRect({
    x0: Math.min(m.x0, m.x1), y0: Math.min(m.y0, m.y1),
    x1: Math.max(m.x0, m.x1), y1: Math.max(m.y0, m.y1),
  }, src);
}

/**
 * Select every line of ``im`` whose box intersects the rect, then copy. The
 * drag may have started over a different page than the focused one, so the
 * source image is passed in rather than read from focus.
 */
function selectLinesInRect(r, im) {
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

/** The image whose cell contains the scene point, or null. */
function imageAtPoint(sx, sy) {
  for (let i = state.images.length - 1; i >= 0; i--) {
    const im = state.images[i];
    if (sx >= im.cellX && sx <= im.cellX + im.cell &&
        sy >= im.labelY && sy <= im.cellY + im.cell) return im;
  }
  return null;
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
 * Fetch OCR lazily, and only when the text is actually needed: while Ctrl is
 * held (the user is about to select), in debug mode (lines are visible), or
 * for the focused page during a search. Only the focused image's text is ever
 * loaded, so the overlay stays a one-page overlay. Pushing to the screen
 * happens on arrival: the fetch stores the data and marks the spans dirty, so
 * the next frame rebuilds them.
 */
function ensureLoaded() {
  if (state.searchActive) {
    // Search mode: only the focused page's text loads. Matched text is already
    // highlighted by the canvas dim layer (search.drawHighlights); the DOM
    // overlay exists for reading/selecting one page, and loading it for every
    // matched page at once is what stalls frames.
    const im = state.focusedImage;
    if (im && im.status === "ready") requestOcr(im);
    return;
  }
  const im = state.focusedImage;
  if (!im || im.status !== "ready") return;
  if (state.tileDebug || state.textSelect) requestOcr(im);
}

/** Fetch one page's OCR once (deduped); push to screen when it arrives. */
function requestOcr(im) {
  if (im.source !== "archive") return; // provider images have no OCR
  if (loaded.has(im.id) || pending.has(im.id)) return;
  suppressed.delete(im.id); // an explicit fetch is wanted: lift any stale suppression
  pending.add(im.id);
  fetchPageOcr(im.bookId, im.pageId)
    .then((data) => {
      pending.delete(im.id);
      // Drop results for pages a drag unloaded mid-flight, and keep data only
      // while the page is still current and on screen: a fetch racing a
      // scroll-away must not resurrect text whose tiles were already pruned.
      if (suppressed.delete(im.id)) return;
      if (state.focusedImage === im
          && viewport.isImageVisible(im, state.viewport.w, state.viewport.h)) {
        loaded.set(im.id, data);
        dirtySpans = true;
        render.requestRender();
      }
    })
    .catch(() => {
      pending.delete(im.id);
    });
}

/**
 * Rebuild the spans for the single image with loaded OCR — the focused page
 * (focus changes unload every other page's text, so at most one image's
 * overlay exists at a time).
 */
function rebuildSpans() {
  if (!sceneEl) return;
  sceneEl.textContent = "";
  spans = [];
  for (const im of state.images) {
    const data = loaded.get(im.id);
    if (!data) continue;
    for (const line of data.lines || []) {
      const el = document.createElement("span");
      el.className = state.tileDebug ? "ocr-line ocr-debug" : "ocr-line";
      el.textContent = line.text;
      el.style.left = (im.drawX + line.x * im.fitFactor) + "px";
      el.style.top = (im.drawY + line.y * im.fitFactor) + "px";
      el.style.width = (line.w * im.fitFactor) + "px";
      el.style.fontSize = (line.h * im.fitFactor) + "px";
      spans.push({ im, line, el });
      sceneEl.appendChild(el);
      fitSpanToBox(el, line.w * im.fitFactor, line.h * im.fitFactor);
    }
  }
  dirtySpans = false;
  applySelectionHighlight();
  updateVisibility();
}

/**
 * Make a span's text fit its OCR bounding box without distorting it. The OCR
 * box matches the printed text's extent, but a browser font is usually wider
 * than the scan's condensed typeface — a title can naturally render 40% wider
 * than its box. The old scaleX squash aligned glyphs to the printed line but
 * visibly crushed the letters; instead the font size is scaled down uniformly
 * (aspect preserved) until the text fits, and the shorter text is centred
 * vertically in the box. Text narrower than the box is left untouched — only
 * overflow is ever reduced. The width is measured with canvas measureText:
 * DOM layout APIs (scrollWidth, Range rects) are clamped to the span's own
 * width by engines that clip via ``overflow: hidden``, but a canvas context
 * measures glyph advances in isolation and cannot be clipped. The 0.5% shrink
 * keeps glyph bearings from poking past the box edge after hinting/rounding.
 */
const _measureCtx = document.createElement("canvas").getContext("2d");

function fitSpanToBox(el, boxW, boxH) {
  if (!(boxW > 0) || !(boxH > 0)) return;
  _measureCtx.font = getComputedStyle(el).font;
  const naturalW = _measureCtx.measureText(el.textContent).width;
  if (!(naturalW > 0) || naturalW <= boxW) return; // already fits: never stretch
  const ratio = (boxW / naturalW) * 0.995;
  const fontSize = parseFloat(el.style.fontSize);
  el.style.fontSize = (fontSize * ratio) + "px";
  // The span's height is its font size (line-height: 1), so centre it.
  el.style.top = (parseFloat(el.style.top) + (boxH - fontSize * ratio) / 2) + "px";
}

/** Hide spans too small to select (avoids tiny/overlapping hit targets). */
function updateVisibility() {
  const sc = state.view.scale;
  for (const s of spans) {
    const onScreen = s.line.h * s.im.fitFactor * sc;
    s.el.style.display = onScreen < OCR_MIN_FONT ? "none" : "";
  }
}
