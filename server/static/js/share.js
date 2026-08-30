/**
 * Share modal: plain link, or a time-limited keyed link for private works.
 *
 * The Share button copies the current URL and opens a centred panel with a QR
 * code of it. When the current location is private or region-locked, a warning
 * explains that a plain link won't show the work to everyone. An option row —
 * "No share" plus one duration per SHARE_DURATIONS — appears only for works
 * that aren't public (owner/granted viewers only): each duration mints a fresh
 * ``?key=`` token (``POST /api/share``), re-copies the keyed URL, refreshes
 * the QR, and highlights the selected button so the active choice is always
 * visible. Dismissed by the Okay button, or Enter/Escape routed here from
 * keys.js.
 */

import * as state from "./state.js";
import { SHARE_DURATIONS } from "./config.js";
import { createShareLink } from "./api/shares.js";

let panel = null;
let qrImg = null;
let tick = null;
let textEl = null;
let warningEl = null;
let optionsEl = null;
let rowEl = null;
let validEl = null;
let open = false;
let buttons = []; // { el, kind, index }
let selected = "none"; // "none" | "key-<index>"

/** Wire the Share button and panel. Call once at startup. */
export function init() {
  panel = document.getElementById("share-panel");
  qrImg = document.getElementById("share-qr");
  tick = document.getElementById("share-tick");
  textEl = document.getElementById("share-text");
  warningEl = document.getElementById("share-warning");
  optionsEl = document.getElementById("share-options");
  rowEl = document.getElementById("share-options-row");
  validEl = document.getElementById("share-valid");
  const btn = document.getElementById("btn-share");
  const ok = document.getElementById("btn-share-ok");
  if (panel && btn) btn.addEventListener("click", show);
  if (ok) ok.addEventListener("click", close);
  buildOptions();
}

/** True while the share panel is visible (keys.js intercepts Enter/Escape). */
export function isOpen() {
  return open;
}

/** Open the panel: warn about restricted works and share the plain link. */
export async function show() {
  if (!panel) return;
  open = true;
  panel.hidden = false;
  selected = "none";

  const warning = shareWarning();
  warningEl.textContent = warning;
  warningEl.hidden = !warning;
  // Time-limited keyed links appear whenever a warning applies — private
  // works, and region-restricted pages of public works (a keyed link works in
  // every region, exactly what the warning promises). Fully-open public works
  // produce no warning and therefore no options.
  optionsEl.hidden = !(warning && canCreateKey());

  await selectOption("none", -1);
}

/** Hide the share panel. */
export function close() {
  open = false;
  if (panel) panel.hidden = true;
}

/** Build the option-row buttons once ("No share" + one per duration). */
function buildOptions() {
  if (!rowEl) return;
  buttons = [];
  rowEl.textContent = "";
  const add = (label, kind, index) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "share-opt";
    b.textContent = label;
    b.addEventListener("click", () => selectOption(kind, index));
    rowEl.appendChild(b);
    buttons.push({ el: b, kind, index });
  };
  add("No share", "none", -1);
  SHARE_DURATIONS.forEach((d, i) => add(d.label, "key", i));
}

/** The plain URL of the current location (share key stripped). */
function plainUrl() {
  return location.origin + location.pathname;
}

/** The (book, page) a keyed token would grant, or null at the root. */
function currentScope() {
  const loc = state.location;
  if (loc.type !== "book" || !loc.book) return null;
  const im = state.focusedImage;
  return {
    book: loc.book.id,
    page: im && im.kind === "page" ? im.pageId : null,
  };
}

/**
 * The warning for the current location, or "" when a plain link works for
 * everyone. Private books warn outright; public books warn when the focused
 * page (or any page, at the book grid) is region-restricted. The owner's own
 * access always resolves "full", so the signal is ``region_locked`` (true for
 * the owner too whenever an anonymous viewer in some zone gets blurred) —
 * that is exactly what a recipient of the plain link would hit.
 */
function shareWarning() {
  const loc = state.location;
  if (loc.type !== "book" || !loc.book) return "";
  if (loc.book.visibility === "private") {
    return "This work is private — no one will be able to view it through a " +
      "plain link. Create a time-limited link below to share it.";
  }
  const im = state.focusedImage;
  let restricted = null;
  if (im && im.kind === "page" && im.access && im.access.region_locked) {
    restricted = im;
  } else if (!im) {
    for (const p of state.images) {
      if (p.kind === "page" && p.access && p.access.region_locked) {
        restricted = p;
        break;
      }
    }
  }
  if (restricted) {
    return "This content won't be available in all regions" +
      (restricted.access.until ? " until " + restricted.access.until : "") +
      ". A time-limited link works everywhere.";
  }
  return "";
}

/** True when the viewer may create a keyed link for the current location. */
function canCreateKey() {
  const v = state.viewer;
  if (!v || !v.authenticated) return false;
  if (v.is_owner) return true;
  const scope = currentScope();
  return !!(scope && v.grants && v.grants.includes(scope.book));
}

/**
 * Select an option: highlight it, then (re)copy its URL and refresh the QR.
 * "none" is the plain link; a duration mints a fresh token each press.
 */
async function selectOption(kind, index) {
  selected = kind;
  for (const b of buttons) {
    b.el.classList.toggle("selected", b.kind === kind && b.index === index);
  }
  let target = plainUrl();
  validEl.hidden = true;
  if (kind === "key") {
    const scope = currentScope();
    if (!scope) return;
    try {
      const data = await createShareLink(
        scope.book, scope.page, SHARE_DURATIONS[index].seconds,
      );
      target += "?key=" + encodeURIComponent(data.key);
      validEl.textContent = "Link valid until " + formatDate(data.expires_at);
      validEl.hidden = false;
    } catch (e) {
      textEl.textContent = "Couldn't create link: " + e.message;
      tick.hidden = true;
      return;
    }
  }
  qrImg.src = "/api/qr?url=" + encodeURIComponent(target);
  textEl.textContent = "URL copied to clipboard";
  tick.hidden = true;
  if (await copyText(target)) tick.hidden = false;
  else textEl.textContent = "Couldn't copy URL";
}

/** "5 Sep 2026" style date for the "valid until" line. */
function formatDate(iso) {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch (e) {
    return iso;
  }
}

/** Write text to the clipboard (execCommand fallback); true on success. */
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {
    /* fall through to the legacy path */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (e) {
    return false;
  }
}
