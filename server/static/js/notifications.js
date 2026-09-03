/**
 * Transient notification queue for the status banner.
 *
 * The access banner is a single floating pill, so only one message can be
 * visible at a time. This module serializes them: a handler shows one message,
 * lets it sit for DISPLAY_MS, slides it away, then starts the next. Producers
 * pick the priority with the two send methods — ``enqueue`` appends to the
 * back (routine notices, e.g. boot-time share-link status), ``enqueueNext``
 * jumps to the front so it displays as soon as the current message finishes
 * (urgent state, e.g. login/logout or a region-unavailable notice).
 */

let bannerEl = null;
let queue = [];
let timer = null;
let showing = false;

/** How long each notification stays visible before sliding away. */
const DISPLAY_MS = 2000;

/** Find the banner element. Safe to call once the DOM is ready. */
export function init() {
  bannerEl = document.getElementById("access-banner");
  pump();
}

/** Add a notification to the back of the queue. */
export function enqueue(text) {
  queue.push(text);
  pump();
}

/** Add a notification to the front of the queue (shows after the current one). */
export function enqueueNext(text) {
  queue.unshift(text);
  pump();
}

/** Show the next queued notification once the banner is ready and idle. */
function pump() {
  if (!bannerEl || showing || queue.length === 0) return;
  showing = true;
  const text = queue.shift();
  bannerEl.textContent = text;
  bannerEl.hidden = false;
  bannerEl.classList.remove("dismissed");
  timer = setTimeout(() => {
    timer = null;
    showing = false;
    hide();
    pump();
  }, DISPLAY_MS);
}

/** Slide the banner away (the CSS transition does the animation). */
function hide() {
  if (!bannerEl || bannerEl.hidden) return;
  bannerEl.classList.add("dismissed");
}
