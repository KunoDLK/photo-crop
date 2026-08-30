/**
 * Share-link URL handling.
 *
 * Location links are plain path segments at the site root (`/93050a0`), which
 * lets social crawlers (which never run JS or see URL hashes) render server-side
 * previews from the path. Written with history.replaceState so navigation never
 * spams history. The hash scheme was removed; no legacy links are supported.
 */

/**
 * Write a location id into the path (null clears back to the root). Share keys
 * never re-appear here: the bv_share cookie (set when a keyed URL loads) and
 * the key kept in state and appended to requests carry the grant, so the
 * address bar stays clean.
 */
export function setPath(id) {
  history.replaceState(null, "", id ? "/" + id : "/");
}

/** The location id from a bare root path segment (`/93050a0`), or null. */
export function idFromPath(path) {
  const m = path.match(/^\/([^/]+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

/** The location id from the current path (`/93050a0`), or null. */
export function currentId() {
  return idFromPath(location.pathname);
}
