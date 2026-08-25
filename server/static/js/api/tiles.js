/**
 * Tile HTTP API.
 *
 * Builds tile URLs and fetches/decodes them into ImageBitmaps. Relies on the
 * server's immutable cache headers for browser caching of already-visited areas
 * (no manual IndexedDB here).
 */

/** Build the absolute URL for a tile (versioned by the page file's mtime). */
export function tileUrl(book, page, version, level, tx, ty) {
  return `/tiles/${encodeURIComponent(book)}/${encodeURIComponent(page)}/${version}/${level}/${tx}/${ty}.jpg`;
}

/**
 * Fetch and decode a single tile into an ImageBitmap.
 *
 * Uses the browser HTTP cache by default so repeat requests are near-instant.
 * Throws on a non-2xx response (callers decide whether to retry or skip).
 *
 * Returns ``{ bitmap, hit }`` where ``hit`` is true when the request was served
 * without a fresh render: from the browser HTTP cache (transferSize 0), the
 * Cloudflare edge cache (cf-cache-status), or the origin disk cache
 * (X-Tile-Cache). Used for the debug-bar cache-hit readout.
 */
export async function fetchTile(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  const blob = await res.blob();
  const bitmap = await createImageBitmap(blob);
  let hit =
    res.headers.get("X-Tile-Cache") === "hit" ||
    ["HIT", "REVALIDATED"].includes(res.headers.get("cf-cache-status") ?? "");
  if (!hit) {
    // Browser-cache hit: no bytes are transferred over the network. The entry
    // may lag the response by a tick, so a missing entry counts as a miss.
    const [entry] = performance.getEntriesByName(new URL(url, location.href).href);
    if (entry && entry.transferSize === 0) hit = true;
  }
  return { bitmap, hit };
}
