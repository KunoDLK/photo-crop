/**
 * Tile HTTP API.
 *
 * Builds tile URLs and fetches/decodes them into ImageBitmaps. Relies on the
 * server's immutable cache headers for browser caching of already-visited areas
 * (no manual IndexedDB here).
 */

/** Build the absolute URL for a tile. */
export function tileUrl(book, page, level, tx, ty) {
  return `/tiles/${encodeURIComponent(book)}/${encodeURIComponent(page)}/${level}/${tx}/${ty}.jpg`;
}

/**
 * Fetch and decode a single tile into an ImageBitmap.
 *
 * Uses the browser HTTP cache by default so repeat requests are near-instant.
 * Throws on a non-2xx response (callers decide whether to retry or skip).
 */
export async function fetchTile(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("HTTP " + res.status);
  const blob = await res.blob();
  return createImageBitmap(blob);
}
