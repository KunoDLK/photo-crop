/**
 * First-run usage tip.
 *
 * Shows a compact key-binding guide on the first load and fades it out on the
 * first interaction (any pointer/wheel/key input). The overlay is
 * pointer-events: none, so the interaction that dismisses it also reaches the
 * app. Whether it has been seen is persisted in localStorage.
 */

const SEEN_KEY = "bookviewer.helpSeen";
const FADE_MS = 800;

let tip = null;
let dismissed = false;

/** Wire the tip overlay. Call once at startup. */
export function init() {
  tip = document.getElementById("user-tip");
  if (!tip) return;
  if (localStorage.getItem(SEEN_KEY)) return;

  tip.hidden = false;
  const events = ["pointerdown", "wheel", "keydown", "touchstart"];

  const dismiss = () => {
    if (dismissed) return;
    dismissed = true;
    for (const ev of events) window.removeEventListener(ev, dismiss);
    localStorage.setItem(SEEN_KEY, "1");
    tip.classList.add("fading");
    setTimeout(() => { tip.hidden = true; }, FADE_MS);
  };

  for (const ev of events) window.addEventListener(ev, dismiss, { passive: true });
}
