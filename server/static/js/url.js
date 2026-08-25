/**
 * Share-link URL handling.
 *
 * Location links are plain path segments at the site root (`/93050a0`), which
 * lets social crawlers (which never run JS or see URL hashes) render server-side
 * previews from the path. Written with history.replaceState so navigation never
 * spams history. The hash scheme was removed; no legacy links are supported.
 */

/** Write a location id into the path (null clears back to the root). */
export function setPath(id) {
  history.replaceState(null, "", id ? "/" + id : "/");
}

/** The location id from a bare root path segment (`/93050a0`), or null. */
export function currentId() {
  const m = location.pathname.match(/^\/([^/]+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}
