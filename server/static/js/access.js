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
 * region-limited counts. The banner auto-slides away after a few seconds and
 * returns on any content change (login, navigation, layout). Also owns the
 * boot-time ``/api/me`` fetch that publishes ``state.viewer``.
 */

import * as state from "./state.js";
import { fetchMe } from "./api/auth.js";

let sceneEl = null;
let bannerEl = null;
let labels = new Map(); // image id -> "Unavailable…" text element (zoomed-in pages only)
let badges = []; // { el } — private pills per cell
let dirty = false;
let lastTransform = "";

// Show the text only when a blurred page is at least this fraction of the
// viewport width (i.e. near page size); below that, the canvas dark tint is
// the whole story. Keeps at most a couple of text elements in the DOM.
const BLUR_TEXT_VIEWPORT_FRACTION = 0.2;

// The banner slides up after this long showing the same content; any change
// to its text (or a navigation or login) brings it back and restarts the
// timer, so a repeated message on a new screen is never skipped.
const BANNER_DISMISS_MS = 4000;
let bannerTimer = null;
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
  bannerEl = document.getElementById("access-banner");
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
 * The banner under the toolbar: signed-in status, or a region notice when the
 * current book has pages the viewer cannot see in full. Auto-dismisses by
 * sliding up after BANNER_DISMISS_MS; any content change (login, navigation,
 * layout) brings it back and restarts the timer.
 */
function updateBanner() {
  if (!bannerEl) return;
  const v = state.viewer;
  if (v && v.authenticated) {
    bannerEl.textContent = "Signed in as " + v.username + (v.is_owner ? " (owner)" : "");
    bannerEl.hidden = false;
  } else {
    let blurred = 0;
    let until = null;
    for (const im of state.images) {
      if (im.kind !== "page" || !im.access || im.access.status !== "blurred") continue;
      blurred++;
      if (im.access.until && (!until || im.access.until < until)) until = im.access.until;
    }
    if (blurred > 0) {
      const n = blurred === state.images.length
        ? "These pages are unavailable in your region"
        : `${blurred} page${blurred === 1 ? " is" : "s are"} unavailable in your region`;
      bannerEl.textContent = n + (until ? ` until ${until}` : "");
      bannerEl.hidden = false;
    } else {
      bannerEl.hidden = true;
    }
  }
  // updateBanner() runs every frame; only re-arm the dismiss timer when the
  // banner's visibility or content actually changed.
  const sig = (bannerEl.hidden ? "h" : "v") + bannerEpoch + "|" + bannerEl.textContent;
  if (sig !== lastBannerSig) {
    lastBannerSig = sig;
    armBannerDismiss();
  }
}

/**
 * (Re)start the slide-away timer: restore the banner, measure its height into
 * ``--banner-h`` (the CSS slide collapses the flex slot by exactly that much),
 * then schedule the ``dismissed`` class.
 */
function armBannerDismiss() {
  clearTimeout(bannerTimer);
  bannerTimer = null;
  bannerEl.classList.remove("dismissed");
  if (bannerEl.hidden) return;
  bannerEl.style.setProperty("--banner-h", bannerEl.offsetHeight + "px");
  bannerTimer = setTimeout(() => {
    bannerTimer = null;
    bannerEl.classList.add("dismissed");
  }, BANNER_DISMISS_MS);
}
