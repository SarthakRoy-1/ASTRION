import type { NextConfig } from "next";

/**
 * The UI is a pure client of the FastAPI backend: it holds no database
 * connection, no secrets, and no business rules. The only configuration it
 * needs is where that backend lives — and the headers a browser will enforce
 * on its behalf.
 *
 * The API sets its own headers (see `app/backend/api/middleware.py`), but the
 * API returns JSON. *This* is the response that loads scripts and renders a
 * document, so this is where the interesting policy lives.
 */

/** Where the browser is allowed to send API requests. */
const apiOrigin = (() => {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (!configured) return "http://127.0.0.1:8000";
  try {
    return new URL(configured).origin;
  } catch {
    // A malformed value must not silently widen the policy to everything.
    return "http://127.0.0.1:8000";
  }
})();

const isProduction = process.env.NODE_ENV === "production";

/**
 * The Content-Security-Policy, assembled rather than pasted.
 *
 * Two directives carry almost all of the value and are worth reading closely:
 *
 * - `script-src 'self'` with no `'unsafe-eval'`, and no `'unsafe-inline'` in
 *   production. This is what turns an injected `<script>` from code execution
 *   into an inert node. React escapes by default and this app renders no
 *   HTML from data, so nothing legitimate needs either escape hatch.
 * - `frame-ancestors 'none'` — clickjacking. Without it, an attacker frames
 *   the real UI invisibly and a user's click lands on the confirm button of a
 *   state-changing action they never saw.
 *
 * `connect-src` is pinned to the configured API origin, so exfiltration to a
 * third party fails at the browser even if something did manage to run.
 *
 * Development keeps `'unsafe-inline'` and `'unsafe-eval'` because Next's dev
 * server and React Fast Refresh genuinely require them. Shipping that policy
 * to production would be the single most common way a CSP ends up decorative,
 * which is why the two are branched here rather than merged into one string
 * somebody later forgets to read.
 */
const contentSecurityPolicy = [
  "default-src 'self'",
  isProduction
    ? "script-src 'self'"
    : "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
  // Next.js injects component styles as inline <style> tags; there is no build
  // mode in which it does not. Inline *style* is a far narrower exposure than
  // inline script — it cannot execute — and the alternative is a per-request
  // nonce, which a statically exported app cannot produce.
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self' ${apiOrigin}`,
  // Nothing in this application embeds, plugs in, or frames anything.
  "object-src 'none'",
  "frame-src 'none'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  // The app posts JSON through fetch, never through a form submission, so no
  // origin is a legitimate form target.
  "form-action 'self'",
  "manifest-src 'self'",
  ...(isProduction ? ["upgrade-insecure-requests"] : []),
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  // Belt and braces with frame-ancestors, for anything that predates CSP.
  { key: "X-Frame-Options", value: "DENY" },
  // Stops a browser second-guessing a declared Content-Type — the route by
  // which a non-HTML response gets rendered as HTML and its contents executed.
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Send the origin cross-site and the full path only same-origin, so a
  // conversation or action id never leaks in a Referer header.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value:
      "geolocation=(), camera=(), microphone=(), payment=(), usb=(), " +
      "magnetometer=(), gyroscope=(), interest-cohort=()",
  },
  { key: "X-Permitted-Cross-Domain-Policies", value: "none" },
  // Keeps this origin out of another document's process, which is the
  // precondition for the cross-origin-isolation side-channel defences.
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "Cross-Origin-Resource-Policy", value: "same-origin" },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  // Next.js otherwise writes its own agent instruction files into this directory.
  // The repository documents itself in README.md and docs/architecture.md.
  agentRules: false,

  // Announcing the app's own version and framework tells an attacker which
  // published advisories to try first, and tells a user nothing.
  poweredByHeader: false,

  async headers() {
    const headers = [...securityHeaders];
    if (isProduction) {
      // Only in production, and only ever from a TLS origin. Announcing HSTS
      // from a plaintext dev server pins a scheme localhost cannot honour and
      // makes it unreachable until the browser's cache is cleared by hand.
      headers.push({
        key: "Strict-Transport-Security",
        value: "max-age=63072000; includeSubDomains; preload",
      });
    }
    return [{ source: "/:path*", headers }];
  },
};

export default nextConfig;
