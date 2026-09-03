/**
 * Colour modes.
 *
 * Four canvas/mode palettes (grey, black, K&S, light) selectable from a
 * radio group in the ☰ menu (each option previews its colours with a swatch
 * tile) and cycled with the B key. The choice sets ``data-mode`` on the
 * document root (driving the ``--canvas-bg``/``--cross`` CSS variables the
 * renderer and cross overlay read), persists in localStorage (``bv.mode``),
 * and is applied before first paint so there is no flash of the wrong
 * background.
 *
 * The radios deliberately are not ``<button>`` elements, so ui.js's
 * "close the ☰ menu after a button press" rule never fires — picking a mode
 * keeps the menu open for further tweaks.
 */

import * as state from "./state.js";
import * as render from "./render.js";

const STORAGE_KEY = "bv.mode";

/** Mode order (menu order, B-key cycle order). The kuno id is kept stable so
 * a persisted ``bv.mode`` still resolves; only its label/palette changed. */
const MODES = [
  { id: "grey", label: "Grey", bg: "#808080", cross: "#404040" },
  { id: "black", label: "Black", bg: "#000000", cross: "#404040" },
  { id: "kuno", label: "K&S", bg: "#191b24", cross: "#76983e", crossAlt: "#F6B6B8" },
  { id: "light", label: "Light", bg: "#f2f2f2", cross: "#404040" },
];

let current = MODES[0].id;

let radios = [];

/** The active mode id. */
export function getCurrent() {
  return current;
}

/** Switch to the next mode in the list (B key). */
export function cycle() {
  const i = MODES.findIndex((m) => m.id === current);
  apply(MODES[(i + 1) % MODES.length].id);
}

/** Apply a mode: set the CSS attribute, persist, repaint. */
export function apply(id) {
  if (!MODES.some((m) => m.id === id)) return;
  current = id;
  document.documentElement.dataset.mode = id;
  try { localStorage.setItem(STORAGE_KEY, id); } catch (e) { /* storage unavailable */ }
  syncUI();
  state.emit("mode-changed", id);
  render.requestRender();
}

/**
 * Restore the persisted mode and wire the radio group. Call once at startup,
 * before the renderer paints (so the correct background is set from frame one).
 */
export function init() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && MODES.some((m) => m.id === saved)) current = saved;
  } catch (e) { /* storage unavailable */ }
  document.documentElement.dataset.mode = current;
  wireRadios();
}

// -------------------------------------------------------------------- radios

/** Paint a swatch element from its mode palette (bg fill + cross in `color`). */
function paintSwatch(el, mode) {
  el.style.background = mode.bg;
  el.style.color = mode.cross; // the <i> plus uses currentColor
}

/** Reflect the current mode in the radio group (checked state). */
function syncUI() {
  for (const input of radios) {
    input.checked = input.value === current;
  }
}

/** Build one radio per mode into #mode-radios (swatches from the palette). */
function wireRadios() {
  const group = document.getElementById("mode-radios");
  if (!group) return;

  radios = MODES.map((m) => {
    const label = document.createElement("label");
    label.className = "mode-radio";

    const input = document.createElement("input");
    input.type = "radio";
    input.name = "bv-mode"; // one shared name: native arrow-key navigation
    input.value = m.id;
    input.addEventListener("change", () => apply(m.id));

    const sw = document.createElement("span");
    sw.className = "mode-swatch";
    sw.appendChild(document.createElement("i"));
    paintSwatch(sw, m);

    const text = document.createElement("span");
    text.className = "mode-radio-name";
    text.textContent = m.label;

    label.append(input, sw, text);
    group.appendChild(label);
    return input;
  });

  syncUI();
}
