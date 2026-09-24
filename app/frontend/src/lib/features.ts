/**
 * Frontend presentation switches.
 *
 * Each one decides only what the public pages *show*. None of them grants,
 * removes or bypasses anything: every capability behind them is still decided
 * by the backend, which is unaffected by any value here.
 */

/**
 * Whether the sign-in page offers the one-click public demo.
 *
 * Off for now. The backend's `POST /api/auth/demo-login`, the seeded demo
 * workspace, `useWorkspaceSession().signInToDemo` and the `DemoAccess` panel in
 * `SignInPanel` are all intact; setting this to `true` restores the button,
 * which still appears only when `/health` reports `demo_login_enabled`.
 */
export const PUBLIC_DEMO_SIGN_IN_ENABLED: boolean = false;

/**
 * Where the landing page's "Watch Demo" button leads.
 *
 * `null` until a recording exists. While it is `null` the button is rendered
 * but announces itself as unavailable and does nothing; setting a URL turns it
 * into a link to the video with no other change.
 */
export const DEMO_VIDEO_URL: string | null = null;
