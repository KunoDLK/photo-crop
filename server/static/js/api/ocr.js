/**
 * OCR + search HTTP API.
 *
 * Thin typed wrappers over the server's OCR/search endpoints, mirroring
 * api/books.js. OCR page data is returned with word/line boxes in source-pixel
 * coordinates; search returns matching pages plus their matching word boxes.
 */

import { withKey } from "../util.js";

/** Fetch the OCR result (word/line boxes) for a single page. */
export async function fetchPageOcr(book, page) {
  const res = await fetch(
    withKey(`/api/books/${encodeURIComponent(book)}/pages/${encodeURIComponent(page)}/ocr`),
    { cache: "no-store" },
  );
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

/** Search a book's OCR text for a literal string or regex. */
export async function searchBook(book, query, regex) {
  const params = new URLSearchParams({ book, q: query });
  if (regex) params.set("regex", "1");
  const res = await fetch(withKey("/api/search?" + params.toString()), { cache: "no-store" });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}
