import type { NextConfig } from "next";

/**
 * The UI is a pure client of the FastAPI backend: it holds no database
 * connection, no secrets, and no business rules. The only configuration it
 * needs is where that backend lives — and the headers a browser will enforce
 * on its behalf.
 *
 * The API sets its own headers (see `app/backend/api/middleware.py`), but the
 * API returns JSON. *This* is the response that loads scripts and renders a
 * document, so this is where the browser-facing policy lives.
 */

/**
 * The Content-Security-Policy is **not** here.
 *
 * It needs a per-request nonce, because Next.js streams its React payload into
 * the document as inline `<script>` blocks and a static `script-src 'self'`
 * blocks them — which it did, in production, leaving the deployed app rendered
 * but never hydrated. A header declared here cannot carry a nonce, so the
 * policy moved to `src/middleware.ts`, which issues one per request. Read that
 * file for the policy and why each directive is in it.
 *
 * The headers below are the ones that are the same on every response.
 */
const isProduction = process.env.NODE_ENV === "production";

const securityHeaders = [
  // Belt and braces with frame-ancestors (set in the middleware's CSP), for
  // anything that predates CSP.
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
