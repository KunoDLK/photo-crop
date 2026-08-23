/**
 * Location short-ID API.
 *
 * A location (book + optional page) maps to a compact server-assigned base62 id
 * used in the URL hash. The mapping is cached client-side so panning over pages
 * only requests each id once.
 */

const idCache = new Map();

/** Fetch (creating if needed) the short id for a (book, page) location. */
export async function getLocationId(book, page) {
  const key = book + "\u0000" + (page || "");
  if (idCache.has(key)) return idCache.get(key);

  const params = new URLSearchParams({ book });
  if (page) params.set("page", page);
  const res = await fetch("/api/locations?" + params.toString(), { cache: "no-store" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  idCache.set(key, data.id);
  return data.id;
}

/** Resolve a short id to { book, page }, or null if unknown. */
export async function resolveLocation(id) {
  const res = await fetch("/api/locations/" + encodeURIComponent(id), { cache: "no-store" });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}
