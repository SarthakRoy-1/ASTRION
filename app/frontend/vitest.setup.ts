import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// jsdom implements no layout, so it ships no `scrollIntoView`. The transcript
// calls it to keep the newest turn in view. Stubbing it here rather than
// guarding the call site keeps a jsdom gap out of production code.
Element.prototype.scrollIntoView = function scrollIntoView() {};

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
});
