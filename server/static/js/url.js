/**
 * URL hash handling.
 *
 * The hash holds a short server-assigned location id (see api/locations.js).
 * Writing it uses ``history.replaceState`` so navigation never spams history.
 */

/** Write a short location id into the hash (null clears it). */
export function setHash(id) {
  const target = id ? "#" + id : location.pathname + location.search;
  history.replaceState(null, "", target);
}

/** Read the current location id from the hash (null when empty). */
export function currentId() {
  const h = location.hash.replace(/^#/, "");
  return h || null;
}
