import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { middleware } from "./middleware";

/**
 * The Content-Security-Policy, and the bug that moved it here.
 *
 * The policy used to be a static header. `script-src 'self'` is the right
 * directive and it was enforcing correctly — on Next.js's own inline
 * bootstrap scripts, which carry the React payload. In production the browser
 * blocked them, hydration failed, and the deployed application rendered its
 * server markup and then did nothing: no session request, no API call, a
 * permanent "Connecting to the ParcelPilot API…".
 *
 * These tests hold the fix to two promises: the policy is not weakened, and
 * the nonce actually varies. A policy that quietly grew `'unsafe-inline'`, or
 * one whose nonce was constant, would pass a page load and fail the only
 * thing the directive is for.
 */

function requestFor(url = "https://parcelpilot.example/") {
  return new NextRequest(new Request(url));
}

function cspOf(url?: string): string {
  const header = middleware(requestFor(url)).headers.get(
    "content-security-policy",
  );
  expect(header).toBeTruthy();
  return header!;
}

/** The policy a deployed build serves. Tests run under NODE_ENV=test. */
function productionCsp(url?: string): string {
  vi.stubEnv("NODE_ENV", "production");
  return cspOf(url);
}

function directive(csp: string, name: string): string {
  const found = csp
    .split(";")
    .map((part) => part.trim())
    .find((part) => part === name || part.startsWith(`${name} `));
  expect(found, `no ${name} directive in ${csp}`).toBeTruthy();
  return found!;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("the content security policy", () => {
  it("admits Next's inline bootstrap by nonce, not by opening the directive", () => {
    const script = directive(productionCsp(), "script-src");

    expect(script).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
    expect(script).toContain("'strict-dynamic'");
    // The weakening this whole file exists to avoid.
    expect(script).not.toContain("'unsafe-inline'");
    expect(script).not.toContain("'unsafe-eval'");
  });

  it("never admits inline script in development either", () => {
    // Development keeps `'unsafe-eval'`, because React Fast Refresh needs it
    // and a policy that breaks hot reload is one somebody switches off. It
    // does *not* get `'unsafe-inline'`: that is the directive that matters,
    // and the nonce already covers what dev legitimately needs.
    vi.stubEnv("NODE_ENV", "development");
    const script = directive(cspOf(), "script-src");

    expect(script).toContain("'unsafe-eval'");
    expect(script).not.toContain("'unsafe-inline'");
    expect(script).toMatch(/'nonce-[A-Za-z0-9+/=]+'/);
  });

  it("upgrades insecure requests only in production", () => {
    expect(productionCsp()).toContain("upgrade-insecure-requests");
    vi.unstubAllEnvs();
    vi.stubEnv("NODE_ENV", "development");
    // Pinning a scheme localhost cannot honour makes it unreachable until the
    // browser cache is cleared by hand.
    expect(cspOf()).not.toContain("upgrade-insecure-requests");
  });

  it("issues a different nonce on every request", () => {
    // A constant nonce is a constant password: it would be readable in any
    // response and reusable in any injection.
    const nonces = new Set(
      Array.from({ length: 5 }, () => {
        const match = /'nonce-([^']+)'/.exec(cspOf());
        return match?.[1];
      }),
    );
    expect(nonces.size).toBe(5);
    expect(nonces.has(undefined)).toBe(false);
  });

  it("tells Next the nonce as well as the browser", () => {
    // Setting only the response header would publish a policy admitting a
    // nonce that nothing in the document carries — the same broken page, with
    // a more convincing header.
    const response = middleware(requestFor());
    const csp = response.headers.get("content-security-policy")!;
    const nonce = /'nonce-([^']+)'/.exec(csp)![1];

    const forwarded = response.headers.get("x-middleware-override-headers");
    expect(forwarded).toContain("x-nonce");
    expect(
      response.headers.get("x-middleware-request-x-nonce"),
    ).toBe(nonce);
  });

  it("pins connect-src to the configured API origin", () => {
    // Exfiltration to a third party fails at the browser even if something did
    // manage to run.
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "https://api.parcelpilot.example/v1");
    expect(directive(cspOf(), "connect-src")).toBe(
      "connect-src 'self' https://api.parcelpilot.example",
    );
  });

  it("falls back to the same default the client uses", () => {
    // If the two disagreed, the browser would block every request the bundle
    // makes. Agreeing on the fallback is what keeps an unconfigured
    // deployment working rather than silently broken.
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "");
    expect(directive(cspOf(), "connect-src")).toBe(
      "connect-src 'self' http://127.0.0.1:8000",
    );
  });

  it("does not widen the policy for a malformed API origin", () => {
    vi.stubEnv("NEXT_PUBLIC_API_BASE_URL", "not a url");
    const connect = directive(cspOf(), "connect-src");
    expect(connect).not.toContain("*");
    expect(connect).toBe("connect-src 'self' http://127.0.0.1:8000");
  });

  it("keeps the directives that do not need a nonce", () => {
    const csp = cspOf();
    // Clickjacking, plugin content, base-tag hijacking and form exfiltration.
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("frame-src 'none'");
    expect(csp).toContain("base-uri 'self'");
    expect(csp).toContain("form-action 'self'");
    expect(csp).toContain("default-src 'self'");
  });
});
