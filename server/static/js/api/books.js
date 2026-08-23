/**
 * Book/page listing API.
 *
 * Thin typed wrappers over the server's JSON endpoints. They normalize responses
 * into the shape the layout/state modules expect and surface errors as thrown
 * exceptions so callers decide how to handle them.
 */

/** Fetch the root listing of books (with cover metadata). */
export async function fetchBooks() {
  const res = await fetch("/api/books", { cache: "no-store" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  return data.books || [];
}

/** Fetch the page listing for a single book. */
export async function fetchPages(bookId) {
  const res = await fetch(`/api/books/${encodeURIComponent(bookId)}/pages`, { cache: "no-store" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  return data.pages || [];
}
