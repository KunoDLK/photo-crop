/**
 * Time-limited share-link API.
 *
 * ``POST /api/share`` mints a server-stored ``?key=`` secret for the current
 * (book, page) location (owner or granted account only); the key grants the
 * shared book/page in every region until it is revoked or expires. The server
 * keeps the authoritative record, so keys can be listed, extended, and
 * revoked from the admin pages. ``GET /api/share/info`` validates a key and
 * returns its metadata.
 */

/** Mint a share key for a location; returns { key, book, page, expires_at }. */
export async function createShareLink(book, page, duration) {
  const res = await fetch("/api/share", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ book, page, duration }),
  });
  if (!res.ok) {
    let msg = "HTTP " + res.status;
    try { msg = (await res.json()).error || msg; } catch (e) { /* keep default */ }
    throw new Error(msg);
  }
  return res.json();
}

/**
 * Validate a share key and read its metadata.
 *
 * Returns ``{valid: true, book, page, expires_at, revoked}`` for a live key,
 * or ``{valid: false}`` for unknown, revoked, or expired ones. The server
 * treats possession of the key as the credential, so no session is needed.
 */
export async function fetchShareInfo(key) {
  const res = await fetch(
    "/api/share/info?key=" + encodeURIComponent(key),
    { cache: "no-store" },
  );
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}
