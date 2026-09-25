import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * The notification's colours, checked where they are defined.
 *
 * "New code sent" once rendered as cream text on a pale green surface: the text
 * followed the page (light, on the dark card) while the surface followed the tone
 * (pale, because only some tokens had been re-declared for dark). jsdom cannot
 * paint, so this reads the real CSS, resolves each tone's text and surface in
 * each palette, composites the translucent dark surfaces over the grounds they
 * actually sit on, and holds every pair to WCAG AA (4.5:1).
 */

const src = (...parts: string[]) => readFileSync(join(process.cwd(), "src", ...parts), "utf8");
const globals = src("app", "globals.css");
const darkTokens = src("components", "site", "darkTokens.module.css");
const callout = src("components", "ui", "Callout.module.css");

type RGBA = [number, number, number, number];

function block(css: string, selector: string): string {
  const start = css.indexOf(selector);
  expect(start, `${selector} block`).toBeGreaterThanOrEqual(0);
  return css.slice(css.indexOf("{", start) + 1, css.indexOf("\n}", start));
}

type Tokens = Map<string, string>;

function tokens(body: string): Tokens {
  const out: Tokens = new Map();
  for (const match of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    const [, name, value] = match;
    if (name !== undefined && value !== undefined) out.set(name, value.trim());
  }
  return out;
}

/** A token's value; a missing one is a failure of the test, not a silent `undefined`. */
function token(set: Tokens, name: string): string {
  const value = set.get(name);
  if (value === undefined) throw new Error(`${name} is not defined`);
  return value;
}

/** A token, read as a colour. */
function colour(set: Tokens, name: string): RGBA {
  return parse(token(set, name));
}

function parse(value: string): RGBA {
  const hex = /^#([0-9a-f]{6})$/i.exec(value);
  if (hex?.[1]) {
    const n = parseInt(hex[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1];
  }
  const rgba = /^rgba?\(([^)]+)\)$/.exec(value);
  if (rgba?.[1]) {
    const [r, g, b, a = "1"] = rgba[1].split(",").map((p) => p.trim());
    return [Number(r), Number(g), Number(b), Number(a)];
  }
  throw new Error(`cannot read colour ${value}`);
}

function over(fg: RGBA, bg: RGBA): RGBA {
  const a = fg[3];
  const mix = (i: 0 | 1 | 2) => fg[i] * a + bg[i] * (1 - a);
  return [mix(0), mix(1), mix(2), 1];
}

function luminance([r, g, b]: RGBA): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(a: RGBA, b: RGBA): number {
  const [la, lb] = [luminance(a), luminance(b)];
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

const TONES = ["ok", "caution", "fail", "neutral"] as const;

const light = tokens(block(globals, ":root"));
const dark = tokens(block(darkTokens, ".dark"));

/** What sits behind a notification: the page for light; for dark, the grounds it is used on. */
const PAGE = parse("#f7f5f1");
const DARK_GROUNDS: Record<string, RGBA> = {
  "app ground": parse("#04090e"),
  "app page surface": parse("#060d13"),
  "sign-in glass over dark sky": parse("#16181d"),
  // Glass over the brightest part of the artwork is the worst case it can meet.
  "sign-in glass over bright sky": parse("#3a4a5c"),
};

function ground(name: string): RGBA {
  const value = DARK_GROUNDS[name];
  if (value === undefined) throw new Error(`no ground called ${name}`);
  return value;
}

describe("notification tones are legible in every palette", () => {
  for (const tone of TONES) {
    it(`${tone}: light palette`, () => {
      const surface = over(colour(light, `--${tone}-soft`), PAGE);
      expect(contrast(colour(light, `--${tone}-text`), surface)).toBeGreaterThanOrEqual(4.5);
      // The glyph is drawn on the tone's solid colour.
      expect(contrast(colour(light, "--on-status"), colour(light, `--${tone}`))).toBeGreaterThanOrEqual(3);
    });

    for (const [name, ground] of Object.entries(DARK_GROUNDS)) {
      it(`${tone}: dark palette on the ${name}`, () => {
        const surface = over(colour(dark, `--${tone}-soft`), ground);
        expect(contrast(colour(dark, `--${tone}-text`), surface)).toBeGreaterThanOrEqual(4.5);
      });
    }

    it(`${tone}: the glyph is readable on the tone's solid colour in the dark palette`, () => {
      const solid = over(colour(dark, `--${tone}`), parse("#0d1218"));
      expect(contrast(colour(dark, "--on-status"), solid)).toBeGreaterThanOrEqual(4.5);
    });
  }

  it("info: dark text on the light connection surface, cream text on dark glass", () => {
    const glass = over(colour(dark, "--accent-soft"), ground("sign-in glass over bright sky"));
    expect(contrast(colour(dark, "--info-text"), glass)).toBeGreaterThanOrEqual(4.5);
    // The card's status override swaps the surface to light, and must swap the text with it.
    const scene = src("components", "auth", "SignInScene.module.css");
    const override = tokens(block(scene, '.card [role="status"]'));
    const surface = over(colour(override, "--accent-soft"), ground("app ground"));
    expect(contrast(colour(override, "--info-text"), surface)).toBeGreaterThanOrEqual(4.5);
  });
});

describe("the notification takes its text from its own tone, never from the page", () => {
  for (const tone of TONES) {
    it(`${tone} names its own text colour for title and body`, () => {
      const rule = block(callout, `.${tone} {`);
      expect(rule).toContain(`--callout-title: var(--${tone}-text)`);
      expect(rule).toContain(`--callout-body: var(--${tone}-text)`);
    });
  }

  it("uses the ambient text tokens only as the fallback", () => {
    expect(block(callout, ".title {")).toContain("var(--callout-title, var(--text-primary))");
    expect(block(callout, ".body {")).toContain("var(--callout-body, var(--text-secondary))");
  });

  it("defines a text token for every tone in both palettes", () => {
    for (const tone of [...TONES, "info"]) {
      expect(light.has(`--${tone}-text`), `light --${tone}-text`).toBe(true);
      expect(dark.has(`--${tone}-text`), `dark --${tone}-text`).toBe(true);
    }
  });
});
