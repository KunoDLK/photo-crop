/**
 * Mosaic proxy-generation gate + progress readout.
 *
 * A mosaic page's coarse-band tiles render from per-cell proxy chains the
 * server may still be generating. While a visible image's proxy state is not
 * ready, the scheduler holds off its tile requests (tiles/scheduler.js
 * `proxyBlocked`) — otherwise the client would fire a wave of cold renders at
 * a store that is busy building chains. This module turns that wait into
 * feedback: it polls the server's proxy-status endpoint, refreshes each
 * image's proxy state, drives the "warming NN%" placeholder (render.js), and,
 * once every page is ready, clears the warming status and reconciles the
 * scheduler so tiles start flowing.
 */

import { MOSAIC_PROXY_POLL_MS } from "./config.js";
import * as state from "./state.js";
import { fetchProxyStatus } from "./api/proxy.js";

let reconcile = null;
let requestRender = null;
let timer = null; // repeating poll for the current warming set
let busy = false; // a poll is in flight (skip overlapping ticks)

/** Provide the callbacks the poller drives. Call once at startup. */
export function init(deps) {
  reconcile = deps.reconcile;
  requestRender = deps.requestRender;
  state.on("images-changed", sync);
  state.on("images-removed", sync);
  state.on("location-changed", sync);
}

/** Images whose proxy chains are not ready (per the last known state). */
function warmingImages() {
  return state.images.filter(
    (im) => im.proxy && im.proxy.enabled && !im.proxy.ready,
  );
}

/**
 * Re-evaluate the warming set after a layout change: mark content-less warming
 * images so they show the progress placeholder, and keep exactly one poll
 * running while any remain. Images that already show content keep it (their
 * cached tiles keep rendering) while their requests stay gated.
 */
export function sync() {
  const warming = warmingImages();
  for (const im of warming) {
    if (im.status !== "ready") im.status = "warming";
  }
  if (!warming.length) {
    stop();
    return;
  }
  if (timer == null) {
    poll();
    timer = setInterval(poll, MOSAIC_PROXY_POLL_MS);
  }
  if (requestRender) requestRender();
}

/** Stop polling (no warming images remain, or a new location loaded). */
export function stop() {
  if (timer != null) {
    clearInterval(timer);
    timer = null;
  }
}

/** Poll every warming page once, then unblock any page that finished. */
async function poll() {
  if (busy) return;
  const targets = [...new Set(warmingImages().map((im) => im.bookId + "\0" + im.pageId))]
    .map((key) => {
      const [bookId, pageId] = key.split("\0");
      return { bookId, pageId };
    });
  if (!targets.length) {
    stop();
    return;
  }
  busy = true;
  let progressed = false;
  let finished = false;
  try {
    for (const { bookId, pageId } of targets) {
      let status;
      try {
        status = await fetchProxyStatus(bookId, pageId);
      } catch (e) {
        continue; // transient error, or no longer a mosaic: retry next tick
      }
      for (const im of state.images) {
        if (im.bookId !== bookId || im.pageId !== pageId) continue;
        const before = im.proxy ? im.proxy.percent : null;
        im.proxy = status;
        if (status.ready) {
          if (im.status === "warming") im.status = "idle";
          finished = true;
        } else if (im.status !== "ready" && before !== status.percent) {
          progressed = true;
        }
      }
    }
  } finally {
    busy = false;
  }
  if (finished) {
    if (!warmingImages().length) stop();
    reconcile();
  }
  if (progressed || finished) requestRender();
}
