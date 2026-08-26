/**
 * Session API: current viewer identity, login, logout.
 *
 * Thin wrappers over ``/api/me``, ``/api/login`` and ``/api/logout``. Login
 * exchanges credentials for the httpOnly session cookie (the browser stores
 * it); the returned profile is what the client caches in ``state.viewer``.
 */

/** The current viewer profile, or null on a network error. */
export async function fetchMe() {
  const res = await fetch("/api/me", { cache: "no-store" });
  if (!res.ok) return null;
  return res.json();
}

/** Log in with owner/account credentials; throws with the server message. */
export async function login(username, password) {
  const res = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    let msg = "HTTP " + res.status;
    try { msg = (await res.json()).error || msg; } catch (e) { /* keep default */ }
    throw new Error(msg);
  }
  return res.json();
}

/** Clear the session cookie. */
export async function logout() {
  await fetch("/api/logout", { method: "POST" });
}
