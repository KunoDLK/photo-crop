/**
 * Cross/arrow lattice overlay.
 *
 * Draws a multi-level lattice of crosses into the main canvas, aligned to the
 * layout's grid. Level 0 puts a cross at every point where four image cells
 * meet (the junctions in the gaps between rows/columns, plus the outer frame);
 * zooming in subdivides by inserting midpoints (roughly a 3x3 glyph grid stays
 * on screen), and zooming out holds one cross per image corner until the
 * spacing would drop below ~4x the glyph size, then decimates to every
 * 2nd/4th/... junction line so the glyphs never merge. When the image content
 * leaves the viewport the crosses morph into arrows pointing back at its
 * nearest edge/corner (easing ramps ported from tmp/cross_arrow_test.html).
 *
 * Rendering batches every visible glyph into one path per active level at
 * device resolution (1px strokes, fixed on-screen glyph size), so the overlay
 * stays cheap at any zoom. Glyphs are drawn under the images, so the pattern
 * only shows in the gaps and in empty space, never over page content.
 */

import * as state from "./state.js";
import * as render from "./render.js";
import { getCss, clamp } from "./util.js";
import {
  CELL, CELL_GAP, LABEL_H,
  GLYPH_PX, DENSITY_DIV, MIN_PATTERN_SPACING, MAX_PATTERN_LEVEL,
  PATTERN_FADE_S, MORPH_TAU_S, DIR_TAU_S, MIN_PATTERN_LEVEL,
} from "./config.js";

// ---------------------------------------------------------------- geometry

/** Glyph frame: arm length in the morph geometry's local units. */
const GLYPH_ARM = 15;

/** Rotate (x, y) by a degrees, CSS semantics (y down, positive = clockwise). */
function rot(x, y, a) {
  const r = a * Math.PI / 180;
  const c = Math.cos(r), s = Math.sin(r);
  return [x * c - y * s, x * s + y * c];
}

/** Shortest signed angular delta from `from` to `to` in degrees. */
function shortestDelta(from, to) {
  return ((to - from + 540) % 360) - 180;
}

/** Normalise an angle to [0, 360). */
function normalize(deg) {
  return ((deg % 360) + 360) % 360;
}

/**
 * The four spindle segments of one glyph for morph progress p (0 = cross,
 * 1 = arrow), including the assembly's -GLYPH_ARM*p drift which centres the
 * finished arrow on the pivot. Segments are [x1, y1, x2, y2] pairs.
 */
function glyphGeometry(p) {
  const drift = -GLYPH_ARM * p;
  const segs = [];
  // Top + bottom slide back toward the tail (+15p), then the drift.
  segs.push([0, GLYPH_ARM * p + drift, 0, GLYPH_ARM * p - GLYPH_ARM + drift]);
  segs.push([0, GLYPH_ARM * p + drift, 0, GLYPH_ARM * p + GLYPH_ARM + drift]);
  // Wings: left anticlockwise, right clockwise, half LENGTH (scaleX).
  const len = GLYPH_ARM * (1 - 0.5 * p);
  {
    const [tx, ty] = rot(-len, 0, -45 * p);
    segs.push([0, drift, tx, ty + drift]);
  }
  {
    const [tx, ty] = rot(len, 0, 45 * p);
    segs.push([0, drift, tx, ty + drift]);
  }
  return segs;
}

// ------------------------------------------------------------------ lattice

// Base line sets (sorted scene coordinates) for the current layout: X = cell
// column boundaries, Y = row junctions + outer frame. Subdivision levels
// insert midpoints, so the finer lattice is always a subset of the base and
// crossfading levels never blink at shared points.
let xBase = [];
let yBase = [];
let basePitch = 0;   // min base pitch (scene units), drives the level index
let pitchX = 0;      // per-axis base pitches: the checkerboard cell size the
let pitchY = 0;      // cross colours alternate over (see draw())
let contentRect = null; // bbox of the image draw rects (arrow target)
let hasLattice = false;

// Zoom-out decimation uses perfectly uniform lattices taken from the image
// grid's own spec (the cell pitch), anchored at the first junction near the
// top-left and extending infinitely past the content — so the grid stays
// regular even across group-boundary gaps, where the base lattice itself is
// irregular.
const X_ANCHOR = CELL + CELL_GAP / 2;          // first column-gap centre
const X_PITCH = CELL + CELL_GAP;               // one cell column
const Y_ANCHOR = LABEL_H + CELL + CELL_GAP / 2; // first row-gap centre
const Y_PITCH = CELL + LABEL_H + CELL_GAP;      // one cell row (label included)

/**
 * Lattice line set from a bag of edge coordinates: the frame edges plus a
 * junction centred in every gap between consecutive edges whose width is not
 * in ``skip`` (cell/label spans). For X the edges are cell sides (gaps =
 * CELL_GAP junctions); for Y they are label tops / cell tops / cell bottoms
 * (LABEL_H and CELL spans are skipped, so junctions land in the 120px row
 * gaps and the wider group-boundary gaps).
 */
function junctionLines(edges, skip) {
  const sorted = [...edges].sort((a, b) => a - b);
  const out = [sorted[0]];
  for (let i = 1; i < sorted.length; i++) {
    const d = sorted[i] - sorted[i - 1];
    if (!skip.has(d)) out.push((sorted[i - 1] + sorted[i]) / 2);
  }
  out.push(sorted[sorted.length - 1]);
  return out;
}

/** Rebuild the base lattice from the current layout (images-changed). */
function rebuildLattice() {
  const xEdges = new Set();
  const yEdges = new Set();
  for (const im of state.images) {
    xEdges.add(im.cellX);
    xEdges.add(im.cellX + im.cell);
    yEdges.add(im.cellY - LABEL_H);   // label top (row start / outer edge)
    yEdges.add(im.cellY);             // cell top
    yEdges.add(im.cellY + im.cell);   // cell bottom
  }
  xBase = junctionLines(xEdges, new Set([CELL]));
  yBase = junctionLines(yEdges, new Set([LABEL_H, CELL]));
  hasLattice = state.images.length > 0 && xBase.length >= 2 && yBase.length >= 2;
  if (hasLattice) {
    pitchX = xBase[1] - xBase[0];
    pitchY = yBase[1] - yBase[0];
    basePitch = Math.min(pitchX, pitchY);
  } else {
    pitchX = 0;
    pitchY = 0;
    basePitch = 0;
  }
  rebuildContentRect();
  // A layout change invalidates the settled level and any arrow state.
  level = 0;
  fadeFrom = -1;
  fadeTo = -1;
  arrowOn = false;
  autoHolding = false;
  wasOffScreen = false;
  redrawVersion++;
}

/** Bbox of all image draw rects — the "content" the arrows point back at. */
function rebuildContentRect() {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const im of state.images) {
    x0 = Math.min(x0, im.drawX);
    y0 = Math.min(y0, im.drawY);
    x1 = Math.max(x1, im.drawX + im.drawW);
    y1 = Math.max(y1, im.drawY + im.drawH);
  }
  contentRect = state.images.length ? { x0, y0, x1, y1 } : null;
}

/**
 * Visible lattice lines at subdivision level lv (<= 0) within [a, b].
 *
 * Each base interval is split into 2^-lv equal parts; the pattern extends
 * past the ends at the edge intervals' pitch. The visible window prunes the
 * result to a handful of lines no matter how deep the level. The finer
 * lattice is always a subset of the base, so crossfades never blink.
 */
function lineRange(base, lv, a, b) {
  const out = [];
  const n = base.length;
  if (!n || b < a) return out;

  const sub = Math.pow(2, -lv);
  // Extension below base[0] at the first interval's pitch (ascending order).
  if (n >= 2 && a < base[0]) {
    const d = base[1] - base[0];
    const lo = Math.max(1, Math.ceil((base[0] - b) * sub / d));
    const hi = Math.floor((base[0] - a) * sub / d);
    for (let m = hi; m >= lo; m--) out.push(base[0] - m * d / sub);
  }
  for (let i = 0; i < n; i++) {
    const p = base[i];
    if (p >= a && p <= b) out.push(p);
    if (i < n - 1) {
      const d = base[i + 1] - p;
      const lo = Math.max(1, Math.ceil((a - p) * sub / d));
      const hi = Math.min(sub - 1, Math.floor((b - p) * sub / d));
      for (let m = lo; m <= hi; m++) out.push(p + m * d / sub);
    }
  }
  // Extension above base[n-1] at the last interval's pitch.
  if (n >= 2 && b > base[n - 1]) {
    const d = base[n - 1] - base[n - 2];
    const lo = Math.max(1, Math.ceil((a - base[n - 1]) * sub / d));
    const hi = Math.floor((b - base[n - 1]) * sub / d);
    for (let m = lo; m <= hi; m++) out.push(base[n - 1] + m * d / sub);
  }
  return out;
}

/**
 * Zoom-out decimation (lv > 0): a perfectly uniform lattice taken from the
 * image grid's own spec — anchor at the first junction, pitch = the cell
 * pitch doubled per level — extending infinitely past the content. Using the
 * cell pitch (not the irregular base intervals) keeps the grid regular even
 * across group-boundary gaps, so no lines bunch up or double-remove at the
 * edges of the image groups.
 */
function uniformRange(anchor, pitch, lv, a, b) {
  const out = [];
  const p = pitch * (1 << lv);
  const m0 = Math.ceil((a - anchor) / p);
  const m1 = Math.floor((b - anchor) / p);
  for (let m = m0; m <= m1; m++) out.push(anchor + m * p);
  return out;
}

// --------------------------------------------------------------- animation

// Eased knobs (ported from the test): morph 0..1, direction in degrees.
let arrowOn = false;
let morphValue = 0;
let dirValue = 0;
let targetRotation = 0;
let autoHolding = false;  // auto mode is the one holding the arrow toggle
let wasOffScreen = false; // previous tick's off-screen state

// Binary level switch + timed fade between lattice levels.
let level = 0;                // settled (fully visible) level
let fadeFrom = -1, fadeTo = -1;
let fadeT = 0;

// Repaint bookkeeping: bump to force a redraw (layout/mode changed).
let redrawVersion = 0;
let lastDrawn = null;

// Colours, cached and refreshed on mode change (canvas bg is render.js's).
// `crossColorAlt` is the second lattice colour (K&S alternates the two);
// every other mode resolves it to the same value via the CSS default.
let crossColor = "#404040";
let crossColorAlt = "#404040";

function refreshColors() {
  crossColor = getCss("--cross");
  crossColorAlt = getCss("--cross-alt") || crossColor;
  redrawVersion++;
  render.requestRender();
}

/** True while the content rect is entirely outside the viewport. */
function isContentOffScreen() {
  if (!contentRect) return true;
  const s = state.view.scale;
  const x0 = -state.view.vx / s, y0 = -state.view.vy / s;
  const x1 = (state.viewport.w - state.view.vx) / s;
  const y1 = (state.viewport.h - state.view.vy) / s;
  return x1 < contentRect.x0 || x0 > contentRect.x1 ||
    y1 < contentRect.y0 || y0 > contentRect.y1;
}

/** Dial angle (0 = up, clockwise) from the viewport centre to the nearest
 * point on the content rect; null when the centre is inside it. */
function contentDirection() {
  if (!contentRect) return null;
  const s = state.view.scale;
  const cx = (state.viewport.w / 2 - state.view.vx) / s;
  const cy = (state.viewport.h / 2 - state.view.vy) / s;
  const nx = clamp(cx, contentRect.x0, contentRect.x1);
  const ny = clamp(cy, contentRect.y0, contentRect.y1);
  const dx = nx - cx, dy = ny - cy;
  if (dx === 0 && dy === 0) return null;
  return Math.atan2(dy, dx) * 180 / Math.PI + 90;
}

let lastT = performance.now();
function tick(now) {
  const dt = Math.min(50, now - lastT) / 1000;
  lastT = now;

  if (hasLattice) {
    // Ease the knobs toward their targets (exponential ramps).
    const morphTarget = arrowOn ? 1 : 0;
    morphValue += (morphTarget - morphValue) * (1 - Math.exp(-dt / MORPH_TAU_S));
    dirValue += shortestDelta(dirValue, targetRotation) * (1 - Math.exp(-dt / DIR_TAU_S));

    // Binary level switch: crossing the zoom threshold fades to the next level
    // with a timed animation instead of a zoom-proportional crossfade.
    //
    // Zoomed in, the pitch subdivides (binary halving) to keep ~3x3 glyphs on
    // screen. Zoomed out, level 0 is held — one cross per image corner — as
    // long as the on-screen spacing stays above ~4x the glyph size; only when
    // it would drop below that does the lattice switch to a coarser uniform
    // grid (the cell pitch doubled per level, anchored top-left and extended
    // past the content), so the glyphs never merge and the grid stays regular
    // even across group gaps. The chosen level is a pure function of the
    // scale, so zooming back in restores the finer lattice (both directions
    // animate through the same fade).
    const vpw = state.viewport.w, vph = state.viewport.h;
    const D = Math.max(1, Math.min(vpw, vph)) / DENSITY_DIV;
    const pitch = basePitch * state.view.scale; // on-screen base spacing
    let targetLevel;
    if (pitch >= D) {
      targetLevel = clamp(Math.floor(Math.log2(D / pitch)), MIN_PATTERN_LEVEL, 0);
    } else {
      targetLevel = clamp(Math.ceil(Math.log2(MIN_PATTERN_SPACING / pitch)), 0, MAX_PATTERN_LEVEL);
    }
    if (fadeTo >= 0) {
      if (targetLevel === level) {
        // Zoomed back before the fade finished: animate back to level.
        fadeFrom = fadeTo;
        fadeTo = level;
        fadeT = 1 - fadeT;
      } else if (targetLevel !== fadeTo) {
        // Retarget mid-fade (fast continuous zooming).
        fadeFrom = level;
        fadeTo = targetLevel;
        fadeT = 0;
      }
    } else if (targetLevel !== level) {
      fadeFrom = level;
      fadeTo = targetLevel;
      fadeT = 0;
    }
    if (fadeTo >= 0) {
      fadeT = Math.min(1, fadeT + dt / PATTERN_FADE_S);
      if (fadeT >= 1) {
        level = fadeTo;
        fadeFrom = -1;
        fadeTo = -1;
      }
    }

    // Content-follow auto mode: once the content leaves the screen, engage the
    // arrow and snap the direction to the nearest edge/corner instantly; while
    // away, further changes ease through the target knob.
    const offScreen = isContentOffScreen();
    if (offScreen) {
      const a = contentDirection();
      if (a !== null) {
        if (!wasOffScreen) {
          if (!arrowOn) {
            arrowOn = true;
            autoHolding = true;
          }
          targetRotation += shortestDelta(targetRotation, a);
          dirValue = targetRotation;
        } else {
          const snapped = targetRotation + shortestDelta(targetRotation, a);
          if (Math.abs(snapped - targetRotation) > 0.5) targetRotation = snapped;
        }
      }
    } else if (autoHolding) {
      autoHolding = false;
      arrowOn = false;
    }
    wasOffScreen = offScreen;
  }

  if (needsRedraw()) render.requestRender();
  requestAnimationFrame(tick);
}

function fadeSig() {
  return fadeTo >= 0 ? fadeFrom * 100 + fadeTo : level;
}

/** True when an animation value changed enough to repaint. */
function needsRedraw() {
  if (!lastDrawn) return true;
  if (redrawVersion !== lastDrawn.version) return true;
  if (Math.abs(morphValue - lastDrawn.morph) > 0.001) return true;
  if (Math.abs(dirValue - lastDrawn.dir) > 0.05) return true;
  if (Math.abs(fadeT - lastDrawn.fadeT) > 0.001) return true;
  if (fadeSig() !== lastDrawn.fadeSig) return true;
  return false;
}

// ------------------------------------------------------------------ drawing

/**
 * Paint the lattice into the renderer's context. Called by render.js after the
 * background fill and before the images, so the pattern never covers content.
 */
export function draw(ctx) {
  // Record the drawn snapshot up front so the early returns (no lattice, or a
  // zero viewport) still settle needsRedraw — otherwise the tick loop would
  // request a repaint every frame on locations without images. draw() never
  // mutates the animation values, so capturing them first is identical to
  // capturing them after painting.
  lastDrawn = {
    version: redrawVersion,
    morph: morphValue,
    dir: dirValue,
    fadeT: fadeT,
    fadeSig: fadeSig(),
  };
  if (!hasLattice) return;
  const s = state.view.scale;
  const vpw = state.viewport.w, vph = state.viewport.h;
  if (!vpw || !vph || s <= 0) return;

  const x0 = -state.view.vx / s, y0 = -state.view.vy / s;
  const x1 = (vpw - state.view.vx) / s, y1 = (vph - state.view.vy) / s;

  const p = morphValue;
  // Pre-rotate to the nearest 90deg increment, sweep the remaining <=45deg.
  const norm = normalize(dirValue);
  const preRot = Math.round(norm / 90) * 90;
  const R = preRot + p * shortestDelta(preRot, norm);

  const geo = glyphGeometry(p);
  const rg = geo.map(([ax, ay, bx, by]) => {
    const [rax, ray] = rot(ax, ay, R);
    const [rbx, rby] = rot(bx, by, R);
    return [rax, ray, rbx, rby];
  });

  // Fixed on-screen glyph size (CSS px): the same visual size on every
  // display; only the lattice spacing scales with the viewport.
  const Gs = GLYPH_PX / GLYPH_ARM; // screen px per glyph-frame unit

  const levels = [];
  if (fadeTo >= 0) {
    const e = fadeT * fadeT * (3 - 2 * fadeT); // smoothstep
    levels.push([fadeFrom, 1 - e], [fadeTo, e]);
  } else {
    levels.push([level, 1]);
  }

  ctx.lineWidth = 1;
  ctx.lineCap = "butt";
  for (const [lv, alpha] of levels) {
    if (alpha <= 0.004) continue;
    const xLines = lv > 0
      ? uniformRange(X_ANCHOR, X_PITCH, lv, x0, x1)
      : lineRange(xBase, lv, x0, x1);
    const yLines = lv > 0
      ? uniformRange(Y_ANCHOR, Y_PITCH, lv, y0, y1)
      : lineRange(yBase, lv, y0, y1);
    if (!xLines.length || !yLines.length) continue;
    ctx.globalAlpha = alpha;
    // Two passes so the glyphs can alternate between the two cross colours
    // (K&S checkerboard). The colour is a pure function of the glyph's scene
    // position over a base-pitch grid — floor() keeps a lattice point the same
    // colour at every level, so shared points never blend during a crossfade.
    for (let c = 0; c < 2; c++) {
      ctx.strokeStyle = c ? crossColor : crossColorAlt;
      ctx.beginPath();
      let any = false;
      for (const x of xLines) {
        const dx = state.view.vx + x * s;
        const xi = Math.floor(x / pitchX);
        for (const y of yLines) {
          if (((xi + Math.floor(y / pitchY)) & 1) !== c) continue;
          const dy = state.view.vy + y * s;
          any = true;
          for (const [rax, ray, rbx, rby] of rg) {
            ctx.moveTo(dx + rax * Gs, dy + ray * Gs);
            ctx.lineTo(dx + rbx * Gs, dy + rby * Gs);
          }
        }
      }
      if (any) ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
}

// -------------------------------------------------------------------- init

/** Wire the overlay. Call once at startup, after the renderer. */
export function init() {
  rebuildLattice();
  refreshColors();
  state.on("images-changed", rebuildLattice);
  state.on("mode-changed", refreshColors);
  requestAnimationFrame(tick);
}
