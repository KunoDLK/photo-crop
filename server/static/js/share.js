/**
 * Share modal: copies the current URL and shows its QR code.
 *
 * The Share button writes the current URL to the clipboard and opens a centred
 * floating panel with a QR code of it (rendered server-side at ``/api/qr``);
 * a green tick appears next to "URL copied to clipboard" once the clipboard
 * write completes. Dismissed by the Okay button, or Enter/Escape routed here
 * from keys.js while the panel is open.
 */

let panel = null;
let qrImg = null;
let tick = null;
let textEl = null;
let open = false;

/** Wire the Share button and panel. Call once at startup. */
export function init() {
  panel = document.getElementById("share-panel");
  qrImg = document.getElementById("share-qr");
  tick = document.getElementById("share-tick");
  textEl = document.getElementById("share-text");
  const btn = document.getElementById("btn-share");
  const ok = document.getElementById("btn-share-ok");
  if (panel && btn) btn.addEventListener("click", show);
  if (ok) ok.addEventListener("click", close);
}

/** True while the share panel is visible (keys.js intercepts Enter/Escape). */
export function isOpen() {
  return open;
}

/** Copy the current URL and show the QR panel; the tick marks copy success. */
export async function show() {
  if (!panel) return;
  open = true;
  panel.hidden = false;
  tick.hidden = true;
  textEl.textContent = "URL copied to clipboard";
  qrImg.src = "/api/qr?url=" + encodeURIComponent(location.href);
  if (await copyText(location.href)) tick.hidden = false;
  else textEl.textContent = "Couldn't copy URL";
}

/** Hide the share panel. */
export function close() {
  open = false;
  if (panel) panel.hidden = true;
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
