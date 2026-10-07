/**
 * Mosaic proxy-status API.
 *
 * A thin wrapper over the server's proxy-generation status endpoint. Mosaic
 * pages whose per-cell proxy chains are missing report not-ready plus build
 * progress; the client holds off tile requests and polls this until ready.
 */

import { withKey } from "../util.js";

/**
 * Fetch a mosaic page's proxy-generation state.
 *
 * Carries any held share keys like every other content request. Returns
 * ``{book, page, version, enabled, ready, ready_cells, total_cells, percent,
 * generating}``. Throws on a non-2xx response (e.g. the book is not a mosaic),
 * so callers can drop the poller.
 */
export async function fetchProxyStatus(bookId, pageId) {
  const res = await fetch(
    withKey(
      `/api/mosaic/${encodeURIComponent(bookId)}/proxy?page=${encodeURIComponent(pageId)}`,
    ),
    { cache: "no-store" },
  );
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}
