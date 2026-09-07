import { NextResponse, type NextRequest } from "next/server";

/**
 * The Content-Security-Policy, issued per request with a nonce.
 *
 * **Why this file exists.** The policy was previously a static header in
 * `next.config.ts`, and `script-src 'self'` with no escape hatch is the right
 * policy — but Next.js streams its React payload into the document as two
 * inline `<script>` blocks, and a static policy has no way to permit exactly
 * those. In production the browser blocked them, React never received its
 * payload, hydration failed with error #412, and the deployed application
 * rendered its server markup and then did nothing at all: no session, no API
 * call, a permanent "Connecting to the ASTRION API…". The policy was
 * enforcing correctly and breaking the product.
 *
 * The fix is not to weaken it. `'unsafe-inline'` would readmit exactly the
 * injected-script attack the directive exists to stop, in an application whose
 * whole point is that a confirmation gate cannot be bypassed. A per-request
 * nonce admits *these* two scripts and nothing else, which is what the policy
 * meant in the first place, and `'strict-dynamic'` lets the bundle they
 * bootstrap load its own chunks without readmitting host-based sources.
 *
 * The cost is that pages render per request rather than being prerendered.
 * That costs this application nothing: every page is a client component that
 * fetches its own data at runtime, so there was never any server-rendered
 * content worth caching.
 *
 * Everything else — frame-ancestors, object-src, base-uri, form-action, and
 * the pinned `connect-src` that keeps exfiltration from reaching a third
 * party — is carried across unchanged from the static policy.
 */

/** Where the browser is allowed to send API requests. */
function apiOrigin(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (!configured) return "http://127.0.0.1:8000";
  try {
    return new URL(configured).origin;
  } catch {
    // A malformed value must not silently widen the policy to everything.
    return "http://127.0.0.1:8000";
  }
}

function policy(nonce: string, isProduction: boolean): string {
  return [
    "default-src 'self'",
    // `'strict-dynamic'` means: trust what this nonce loads, and nothing
    // because of where it came from. `'self'` stays for browsers that do not
    // implement it, where it is the previous policy exactly.
    //
    // `'unsafe-eval'` in development only, because React Fast Refresh genuinely
    // needs it and a policy that breaks hot reload is a policy someone will
    // turn off. Production gets neither escape hatch — which is the whole
    // point, and is asserted in `middleware.test.ts`.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'` +
      (isProduction ? "" : " 'unsafe-eval'"),
    // Next.js injects component styles as inline <style> tags; there is no
    // build mode in which it does not. Inline *style* is a far narrower
    // exposure than inline script — it cannot execute.
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src 'self' ${apiOrigin()}`,
    // Nothing in this application embeds, plugs in, or frames anything.
    "object-src 'none'",
    "frame-src 'none'",
    // Clickjacking: without this an attacker frames the real UI invisibly and
    // a user's click lands on the confirm button of an action they never saw.
    "frame-ancestors 'none'",
    "base-uri 'self'",
    // The app posts JSON through fetch, never through a form submission.
    "form-action 'self'",
    "manifest-src 'self'",
    ...(isProduction ? ["upgrade-insecure-requests"] : []),
  ].join("; ");
}

export function middleware(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = policy(nonce, process.env.NODE_ENV === "production");

  // Next.js reads the nonce back out of the *request* header and stamps it on
  // the script tags it emits. Setting only the response header would produce a
  // policy that permits a nonce nothing carries.
  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("content-security-policy", csp);

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("content-security-policy", csp);
  return response;
}

export const config = {
  matcher: [
    /*
     * Documents only. Static chunks, images and the favicon are served
     * straight from disk and carry no inline script, so running this on them
     * would spend a nonce per asset for nothing — and, worse, would make
     * every asset a dynamic response.
     */
    {
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
