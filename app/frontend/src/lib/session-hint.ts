/**
 * Whether this browser has recently been signed in.
 *
 * Used for exactly one presentation decision: what `/` shows while the
 * backend has not yet said who the visitor is. A first-time visitor gets the
 * public landing page at once — it needs nothing from the API, which may take a
 * minute to wake — while someone returning to their workspace keeps seeing the
 * application waiting for its backend, so a cold start never looks like a
 * sign-out.
 *
 * It is a hint, never a credential. The session cookie is `HttpOnly` and this
 * code cannot read it; the value here authorises nothing, and a wrong value
 * only changes which of two screens is shown for the seconds before
 * `/api/auth/me` answers.
 */

const STORAGE_KEY = "astrion.signed-in";

export function readSessionHint(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    // Storage can be unavailable (private windows, blocked site data).
    return false;
  }
}

export function writeSessionHint(signedIn: boolean): void {
  try {
    if (signedIn) window.localStorage.setItem(STORAGE_KEY, "1");
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // A preference we could not store is a preference we do without.
  }
}

/** Nothing to subscribe to: the hint only changes through this tab's own writes. */
export function subscribeSessionHint(): () => void {
  return () => {};
}
