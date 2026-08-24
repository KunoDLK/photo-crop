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
import { resolveLocation } from "./api/locations.js";
import * as compositor from "./compositor.js";
import * as scheduler from "./tiles/scheduler.js";
import { TileCache } from "./tiles/tileCache.js";
import { TileQueue } from "./tiles/queue.js";
import * as ocrOverlay from "./ocr/overlay.js";
import * as search from "./ocr/search.js";
import * as nameFilter from "./nameFilter.js";

async function bootstrap() {
  const viewEl = document.getElementById("view");
  const leftEl = document.getElementById("left");

  const cache = new TileCache();
  compositor.init({ cache });
  render.initDebug({ cache });

  const queue = new TileQueue((req, bitmap) => scheduler.handleTile(req, bitmap));
  scheduler.init({ cache, queue, requestRender: render.requestRender });

  render.initRenderer(viewEl, leftEl);
  interaction.init({ scheduler, nav });
  interaction.installInteraction(viewEl);
  keys.init({ nav });
  keys.installKeys();
  ui.init({ cache, queue, nav });
  ocrOverlay.init({ viewEl });
  search.init({ nav });
  nameFilter.init({ nav });

  state.on("images-removed", (removed) => {
    for (const im of removed) cache.dropImage(im.id);
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

  // Navigate when the hash changes (share link opened, back/forward, manual edit).
  async function navigateFromHash() {
    const id = url.currentId();
    if (!id) {
      await nav.showBooks();
      return;
    }
    try {
      const loc = await resolveLocation(id);
      if (loc && loc.book) await nav.openFromURL(loc);
      else await nav.showBooks();
    } catch (e) {
      await nav.showBooks();
    }
  }

  window.addEventListener("hashchange", navigateFromHash);

  // Restore the location from the URL hash (short id) if present.
  await navigateFromHash();
}

bootstrap();
