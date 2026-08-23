/**
 * Book/page listing API.
 *
 * Thin typed wrappers over the server's JSON endpoints. They normalize responses
 * into the shape the layout/state modules expect and surface errors as thrown
 * exceptions so callers decide how to handle them.
 */

/** Fetch the root listing of books (with cover metadata + change signature). */
export async function fetchBooks(force) {
  const res = await fetch("/api/books" + (force ? "?force=1" : ""), { cache: "no-store" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

/** Fetch the page listing for a single book (+ change signature). */
export async function fetchPages(bookId, force) {
  const res = await fetch(
    `/api/books/${encodeURIComponent(bookId)}/pages` + (force ? "?force=1" : ""),
    { cache: "no-store" },
  );
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

/** Fetch detailed metadata (dims, file size, content hash) for one image. */
export async function fetchImageInfo(bookId, pageId) {
  const res = await fetch(
    `/api/books/${encodeURIComponent(bookId)}/pages/${encodeURIComponent(pageId)}/info`,
    { cache: "no-store" },
  );
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}
