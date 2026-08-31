/**
 * Access UI: region/blur labels, private badges, and the auth banner.
 *
 * Reads the per-image ``access`` (server-resolved per request: full, blurred,
 * or nonexistent) and the per-book ``visibility`` from the listings and renders
 * them as DOM elements inside the OCR scene, so they inherit the single pan/zoom
 * transform. The dark tint over unavailable pages is painted on the canvas by
 * render.js (cheap at any zoom); this module adds the "Unavailable in your
 * region…" text only when such a page is zoomed in near page size, plus a
 * badge on private books; the top banner reports the session and
 * region-limited counts. Status lines go through the notification queue
 * (notifications.js) as urgent messages, so they display as soon as the
 * current one finishes; boot-time share-link notices follow at the back.
 * Also owns the boot-time ``/api/me`` fetch that publishes ``state.viewer``.
 */

import * as state from "./state.js";
import * as notifications from "./notifications.js";
import { fetchMe } from "./api/auth.js";
import { BLUR_TEXT_VIEWPORT_FRACTION } from "./config.js";

let sceneEl = null;
let labels = new Map(); // image id -> "Unavailable…" text element (zoomed-in pages only)
let badges = []; // { el } — private pills per cell
let dirty = false;
let lastTransform = "";

// Re-evaluation counter: any content change (login, navigation, layout) bumps
// it so a repeated status line is pushed to the queue again on a new screen.
let bannerEpoch = 0;
let lastBannerSig = null;

/**
 * Wire the access layer to its scene element and load the viewer identity.
 *
 * The access elements live in their own ``#access-scene`` sibling of the OCR
 * scene, with the same transform applied every frame: the OCR overlay rebuilds
 * its spans by wiping its scene's children, which would otherwise destroy these
 * labels whenever the user focuses or navigates.
 */
export async function init() {
  sceneEl = document.getElementById("access-scene");
  if (!sceneEl) return;

  state.on("images-changed", () => { dirty = true; bannerEpoch++; });
  state.on("images-removed", () => { dirty = true; bannerEpoch++; });
  state.on("focus-changed", () => updateBanner());
  state.on("auth-changed", () => { bannerEpoch++; updateBanner(); });

  const me = await fetchMe();
  state.setViewer(me); // null when the endpoint is unreachable
  updateBanner();
}

/**
 * Rebuild the badges for the current listing. Called from the render loop via
 * `update()` whenever the layout changed; the elements live in scene
 * coordinates so the existing transform keeps them glued to the images.
 */
function rebuild() {
  if (!sceneEl) return;
  for (const el of labels.values()) el.remove();
  labels.clear();
  for (const b of badges) b.el.remove();
  badges = [];
  for (const im of state.images) {
    if (im.kind === "book") {
      if (im.visibility === "private") badges.push(makeBadge(im, "Private"));
    }
  }
  dirty = false;
  updateBanner();
}

/** A small pill pinned to the top-right of an image's cell. */
function makeBadge(im, text) {
  const el = document.createElement("div");
  el.className = "access-badge";
  el.textContent = text;
  el.style.left = (im.cellX + im.cell - 72) + "px";
  el.style.top = (im.labelY + 4) + "px";
  sceneEl.appendChild(el);
  return { el };
}

/**
 * The "Unavailable in your region [until …]" text, sized to the image's box in
 * scene coordinates so it grows and shrinks with the pan/zoom transform. Only
 * created for pages the viewer has actually zoomed into (see refreshLabels).
 */
function makeBlurLabel(im) {
  const el = document.createElement("div");
  el.className = "blur-label";
  const until = im.access.until;
  el.textContent = "Unavailable in your region" + (until ? " until " + until : "");
  el.style.left = im.drawX + "px";
  el.style.top = im.drawY + "px";
  el.style.width = im.drawW + "px";
  el.style.height = im.drawH + "px";
  el.style.fontSize = Math.max(10, im.drawW * 0.055) + "px";
  sceneEl.appendChild(el);
  return el;
}

/**
 * Per-frame sync hook (render loop): keep the scene transform glued to the
 * view (mirroring the OCR scene) and rebuild on layout changes. The dark tint
 * itself is canvas-drawn (render.js); here only the zoom-gated text labels
 * are managed, so DOM work happens only when a page crosses the threshold.
 */
export function update() {
  if (!sceneEl) return;
  const { scale, vx, vy } = state.view;
  const t = `translate(${vx}px, ${vy}px) scale(${scale})`;
  if (t !== lastTransform) {
    lastTransform = t;
    sceneEl.style.transform = t;
  }
  if (dirty) rebuild();
  refreshLabels();
  updateBanner();
}

/**
 * Create/remove the "Unavailable in your region" text so it exists only for
 * blurred pages the viewer is actually looking at up close: the image must be
 * on screen and at least BLUR_TEXT_VIEWPORT_FRACTION of the viewport wide.
 * The canvas dark tint covers every zoom level; the text is the rare extra.
 */
function refreshLabels() {
  if (!sceneEl) return;
  const vpw = state.viewport.w, vph = state.viewport.h;
  if (!vpw || !vph) return;
  const sc = state.view.scale;
  for (const im of state.images) {
    if (im.kind !== "page" || !im.access || im.access.status !== "blurred") continue;
    const dx = state.view.vx + im.drawX * sc;
    const dy = state.view.vy + im.drawY * sc;
    const onW = im.drawW * sc;
    const onH = im.drawH * sc;
    const want = onW >= vpw * BLUR_TEXT_VIEWPORT_FRACTION
      && dx + onW >= 0 && dx <= vpw && dy + onH >= 0 && dy <= vph;
    const el = labels.get(im.id);
    if (want && !el) labels.set(im.id, makeBlurLabel(im));
    else if (!want && el) {
      el.remove();
      labels.delete(im.id);
    }
  }
}

/**
 * Push the current status — signed-in line, or a region-unavailable notice —
 * into the notification queue. Runs every frame, so the signature check
 * ensures repeated calls never re-enqueue the same message; only content or
 * epoch changes (login, navigation, layout) actually emit. Status is urgent,
 * so it uses enqueueNext and displays as soon as the current notification
 * finishes; boot-time share-link notices (queued at the back) follow it.
 */
function updateBanner() {
  const text = statusText();
  const sig = bannerEpoch + "|" + (text || "");
  if (sig === lastBannerSig) return;
  lastBannerSig = sig;
  if (text) notifications.enqueueNext(text);
}

/** The banner line for the current session/region, or null when silent. */
function statusText() {
  const v = state.viewer;
  if (v && v.authenticated) {
    return "Signed in as " + v.username + (v.is_owner ? " (owner)" : "");
  }
  let blurred = 0;
  let until = null;
  for (const im of state.images) {
    if (im.kind !== "page" || !im.access || im.access.status !== "blurred") continue;
    blurred++;
    if (im.access.until && (!until || im.access.until < until)) until = im.access.until;
  }
  if (blurred === 0) return null;
  const n = blurred === state.images.length
    ? "These pages are unavailable in your region"
    : `${blurred} page${blurred === 1 ? " is" : "s are"} unavailable in your region`;
  return n + (until ? ` until ${until}` : "");
}
