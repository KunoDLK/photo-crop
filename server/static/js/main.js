/**
 * Bootstrap and wiring.
 *
 * The only module that imports everything and constructs the object graph. It
 * builds the tile cache, queue, scheduler, renderer, and nav, installs
 * input/keys/UI, then loads the initial (root) listing. No domain logic here.
 */

import * as state from "./state.js";
import * as render from "./render.js";
import * as interaction from "./interaction.js";
import * as keys from "./keys.js";
import * as ui from "./ui.js";
import * as nav from "./nav.js";
import * as url from "./url.js";
import * as compositor from "./compositor.js";
import * as scheduler from "./tiles/scheduler.js";
import { TileCache } from "./tiles/tileCache.js";
import { TileQueue } from "./tiles/queue.js";
import * as ocrOverlay from "./ocr/overlay.js";
import * as search from "./ocr/search.js";
import * as nameFilter from "./nameFilter.js";
import * as help from "./help.js";
import * as share from "./share.js";
import * as login from "./login.js";
import * as access from "./access.js";
import * as fullscreen from "./fullscreen.js";

async function bootstrap() {
  const viewEl = document.getElementById("view");
  const leftEl = document.getElementById("left");

  // Keep tile resource-timing entries long enough for the cache-hit readout to
  // find each one (browser-cache hits are detected via transferSize === 0).
  if (performance.setResourceTimingBufferSize) {
    performance.setResourceTimingBufferSize(100000);
  }

  const cache = new TileCache();
  compositor.init({ cache });
  render.initDebug({ cache });

  const queue = new TileQueue(
    (req, bitmap) => scheduler.handleTile(req, bitmap),
    () => scheduler.reconcile(), // stale (identity-changed) tile discarded: re-decide
  );
  scheduler.init({ cache, queue, requestRender: render.requestRender });

  render.initRenderer(viewEl, leftEl);
  interaction.init({ scheduler, nav });
  interaction.installInteraction(viewEl);
  fullscreen.init({ viewEl });
  keys.init({ nav });
  keys.installKeys();
  ui.init({ cache, queue, nav });
  ocrOverlay.init({ viewEl });
  search.init({ nav });
  nameFilter.init({ nav });
  help.init();
  share.init();
  login.init();
  // Resolve the viewer identity before the initial navigation so the boot-time
  // auth-changed event (below) is never mistaken for a login/logout.
  await access.init();

  state.on("images-removed", (removed) => {
    for (const im of removed) cache.dropImage(im.id);
  });

  // The renderer re-fits the view after a large viewport change (rotation,
  // browser bars collapsing); reconcile tile targets for the new size.
  state.on("viewport-resized", () => {
    scheduler.reconcile();
  });

  state.setFrameHook(() => {
    if (state.tileDebug) ui.updateStats();
  });

  // Console helper: dump every tile currently held in the decoded cache, grouped
  // by image with per-level/tile coordinates and pin state.
  window.dumpTiles = () => {
    const byImage = new Map();
    for (const key of cache.map.keys()) {
      const parts = key.split(":");
      const id = Number(parts[0]);
      const level = Number(parts[1]);
      const tx = Number(parts[2]);
      const ty = Number(parts[3]);
      if (!byImage.has(id)) byImage.set(id, []);
      byImage.get(id).push({ level, tx, ty, pinned: cache.pinned.has(key) });
    }
    const imById = new Map(state.images.map((im) => [im.id, im]));
    let total = 0;
    const groups = [...byImage.entries()].sort((a, b) => a[0] - b[0]);
    for (const [id, tiles] of groups) {
      const im = imById.get(id);
      const label = im
        ? `${im.kind} "${im.name}" ${im.bookId}/${im.pageId}`
        : `id ${id} (removed)`;
      total += tiles.length;
      console.group(
        `#${id} ${label} — ${tiles.length} tile(s), target L${im ? im.targetLevel : "?"}, max L${im ? im.maxLevel : "?"}`
      );
      tiles.sort((a, b) => a.level - b.level || a.tx - b.tx || a.ty - b.ty);
      for (const t of tiles) {
        console.log(`L${t.level} ${t.tx},${t.ty}${t.pinned ? " (pinned)" : ""}`);
      }
      console.groupEnd();
    }
    console.log(`TOTAL: ${total} tile(s) across ${byImage.size} image(s)`);
    return { total, byImage };
  };

  // Debug: expose live state for inspection (e.g. badge hit rects).
  window.__state = state;

  // Navigate from the current path: a bare root segment is a share-link id.
  function navigateFromPath() {
    nav.navigateToPath(location.pathname);
  }

  window.addEventListener("popstate", navigateFromPath);

  // Intercept clicks on real <a href> links to bare root paths (the crawler
  // links rendered into #seo-content) and route them through the SPA, so users
  // never see a full page load. Modifier clicks, middle clicks, and
  // target="_blank" fall through to a normal page load, which the server also
  // serves with injected content.
  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey
        || e.shiftKey || e.altKey || !(e.target instanceof Element)) return;
    const a = e.target.closest("a[href]");
    if (!a || (a.target && a.target !== "_self")) return;
    const path = (a.getAttribute("href") || "").split("#")[0];
    if (!path.startsWith("/") || path.startsWith("//")) return;
    if (path !== "/" && url.idFromPath(path) === null) return;
    e.preventDefault();
    nav.navigateToPath(path);
  });

  // Restore the location from the launch path (short id) if present.
  await navigateFromPath();

  // Automatic access switching: when the viewer logs in or out, drop the
  // decoded tiles and invalidate in-flight fetches (the access variant of
  // every image flips, so stale blur/real bitmaps must be refetched) and
  // reload the current location. Registered after the initial navigation, so
  // the boot-time identity fetch never triggers it.
  state.on("auth-changed", async () => {
    cache.clear();
    queue.invalidate();
    if (state.viewer && state.viewer.authenticated) {
      // Signed in: refetch the current location in place (private books
      // appear, blurred pages flip to real tiles).
      nav.reload();
      return;
    }
    // Signed out: revert to the anonymous view — reload in place when the book
    // still exists publicly, otherwise return to the root book list.
    if (state.location.type === "book" && state.location.book) {
      const bookId = state.location.book.id;
      try {
        const res = await fetch(`/api/books/${encodeURIComponent(bookId)}/pages`, {
          cache: "no-store",
        });
        if (res.ok) {
          nav.reload();
          return;
        }
      } catch (e) {
        /* fall through to the root */
      }
    }
    nav.showBooks();
  });
}

bootstrap();
