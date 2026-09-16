import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * The active workspace name has to be readable where it is rendered.
 *
 * jsdom computes no cascade from CSS modules, so a rendered-style assertion
 * would pass whatever the stylesheet said. What regressed was a token: the
 * dark-on-light body colour used inside the always-dark header, which put the
 * workspace name at roughly 1.1:1. This pins the token the header needs.
 */
function declarations(css: string, className: string): string {
  const start = css.indexOf(`.${className} {`);
  if (start === -1) return "";
  return css.slice(start, css.indexOf("}", start));
}

describe("the workspace name in the header", () => {
  const css = readFileSync(
    resolve(__dirname, "WorkspaceSwitcher.module.css"),
    "utf8",
  );

  it("uses the header's light text token", () => {
    expect(declarations(css, "name")).toContain("color: var(--text-inverse)");
  });

  it("does not use the dark body text token on the dark header", () => {
    expect(declarations(css, "name")).not.toContain("--text-primary");
  });
});
