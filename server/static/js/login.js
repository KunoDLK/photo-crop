/**
 * Login modal and toolbar lock button.
 *
 * The lock button opens a centred modal: a username/password form for
 * anonymous viewers, or a "Signed in as … / Log out" view for authenticated
 * ones. A successful login refreshes ``state.viewer`` and reloads the current
 * location so private books appear without a full page load. Dismissed by
 * Cancel, or Enter (submit) / Escape routed here from keys.js while open.
 */

import * as state from "./state.js";
import { login, logout } from "./api/auth.js";

let panel = null;
let form = null;
let user = null;
let pass = null;
let message = null;
let loggedInView = null;
let open = false;

/** Wire the lock button and modal. Call once at startup. */
export function init() {
  panel = document.getElementById("login-panel");
  form = document.getElementById("login-form");
  user = document.getElementById("login-user");
  pass = document.getElementById("login-pass");
  message = document.getElementById("login-message");
  loggedInView = document.getElementById("login-logged-in");
  const btn = document.getElementById("btn-auth");
  const cancel = document.getElementById("btn-login-cancel");
  const submit = document.getElementById("btn-login-go");
  const logoutBtn = document.getElementById("btn-login-logout");
  if (btn) btn.addEventListener("click", toggle);
  if (cancel) cancel.addEventListener("click", close);
  if (submit && form) form.addEventListener("submit", (e) => {
    e.preventDefault();
    submitCredentials();
  });
  if (logoutBtn) logoutBtn.addEventListener("click", doLogout);
  state.on("auth-changed", refreshButton);
}

/** Open the modal when anonymous, log out when authenticated (lock toggle). */
function toggle() {
  if (state.viewer && state.viewer.authenticated) doLogout();
  else openModal();
}

/** True while the login modal is visible (keys.js intercepts Enter/Escape). */
export function isOpen() {
  return open;
}

/** Show the login modal with focus on the username field. */
export function openModal() {
  if (!panel) return;
  open = true;
  panel.hidden = false;
  if (message) message.textContent = "";
  refreshButton();
  if (user) user.focus();
}

/** Hide the login modal. */
export function close() {
  open = false;
  if (panel) panel.hidden = true;
}

/** Submit the credentials; the auth-changed handler refetches the listing. */
async function submitCredentials() {
  const username = user.value.trim();
  const password = pass.value;
  if (!username || !password) return;
  if (message) message.textContent = "Logging in…";
  try {
    const profile = await login(username, password);
    state.setViewer(profile); // auth-changed → tiles cleared + listing refetched
    close();
    pass.value = "";
    state.setStatus("Signed in as " + profile.username);
  } catch (e) {
    if (message) message.textContent = e.message;
  }
}

/** Log out; the auth-changed handler reverts the view. */
async function doLogout() {
  try { await logout(); } catch (e) { /* session dies on the server; ignore */ }
  state.setViewer({ authenticated: false, username: null, is_owner: false, grants: [] });
  close();
  state.setStatus("Signed out");
}

/** Reflect the viewer identity on the lock button and the modal body. */
function refreshButton() {
  const btn = document.getElementById("btn-auth");
  const v = state.viewer;
  // Admin entry point exists only for the owner; everyone else gets nothing.
  const admin = document.getElementById("btn-admin");
  if (admin) admin.hidden = !(v && v.authenticated && v.is_owner);
  if (!v || !v.authenticated) {
    if (btn) btn.textContent = "Log in";
    if (form) form.hidden = false;
    if (loggedInView) loggedInView.hidden = true;
    return;
  }
  if (btn) btn.textContent = v.username + (v.is_owner ? " (owner)" : "");
  if (form) form.hidden = true;
  if (loggedInView) {
    loggedInView.hidden = false;
    const label = document.getElementById("login-signed-in");
    if (label) label.textContent = "Signed in as " + v.username + (v.is_owner ? " (owner)" : "");
  }
}
