/**
 * A test double for `next/navigation`.
 *
 * Aliased in `vitest.config.ts` rather than mocked per file, so every test
 * mounts the real shell — navigation, active state and all — instead of a
 * stripped-down page that would not exercise it.
 *
 * It is a real, tiny router: `push` and `replace` change the location this
 * module reports, and the components subscribe to it. That matters for the
 * operations screen, whose selected signal lives in the query string — a mock
 * that only recorded calls would let a broken selection pass.
 */

import { useSyncExternalStore } from "react";

interface Location {
  pathname: string;
  query: URLSearchParams;
}

let current: Location = { pathname: "/", query: new URLSearchParams() };
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function parse(href: string): Location {
  const [pathname = "/", search = ""] = href.split("?");
  return { pathname, query: new URLSearchParams(search) };
}

/** Put the router at a location. Call before rendering. */
export function setTestRoute(href: string): void {
  current = parse(href);
  emit();
}

/** Where the router is now — for asserting that navigation happened. */
export function testRoute(): string {
  const search = current.query.toString();
  return search ? `${current.pathname}?${search}` : current.pathname;
}

/** Back to the root, between tests. */
export function resetTestRoute(): void {
  current = { pathname: "/", query: new URLSearchParams() };
  listeners.clear();
}

function navigate(href: string) {
  current = parse(href);
  emit();
}

export function usePathname(): string {
  return useSyncExternalStore(
    subscribe,
    () => current.pathname,
    () => current.pathname,
  );
}

export function useSearchParams(): URLSearchParams {
  return useSyncExternalStore(
    subscribe,
    () => current.query,
    () => current.query,
  );
}

export function useRouter() {
  return {
    push: (href: string) => navigate(href),
    replace: (href: string) => navigate(href),
    back: () => {},
    forward: () => {},
    refresh: () => {},
    prefetch: () => {},
  };
}
