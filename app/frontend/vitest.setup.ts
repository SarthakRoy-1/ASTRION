import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

import { resetTestRoute } from "./src/test/next-navigation";

// jsdom implements no layout, so it ships no `scrollIntoView`. The transcript
// calls it to keep the newest turn in view. Stubbing it here rather than
// guarding the call site keeps a jsdom gap out of production code.
Element.prototype.scrollIntoView = function scrollIntoView() {};

// Nor does it ship `matchMedia`. The operations screen asks whether it is
// below the two-column breakpoint before scrolling a selection into view;
// reporting "no match" gives tests the desktop layout, which is where the
// list and the detail are both on screen.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// Every test drives the UI through a mocked `fetch`, so no test can reach a
// real backend, a real database, or a real model provider. A test that forgets
// to install a response gets a loud failure rather than a silent network call.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => {
      throw new Error("unmocked fetch call: install a response in the test");
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  resetTestRoute();
});
