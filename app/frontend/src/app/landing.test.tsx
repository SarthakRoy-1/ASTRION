import { readFileSync } from "node:fs";
import { join } from "node:path";

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import SupportPage from "./page";
import SignInPage from "./sign-in/page";
import { LoadingTruck, LOADING_TRUCK_SRC } from "@/components/LoadingTruck";
import { WatchDemoButton } from "@/components/landing/WatchDemoButton";
import health from "@/test/fixtures/health.json";
import { stubApi } from "@/test/helpers";
import { setTestRoute, testRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

/**
 * The public front door.
 *
 * `/` is the landing page for anyone not signed in, its two ways in lead to
 * the application's existing sign-in and registration forms, and it exposes no
 * demo account — whatever the server offers. None of it may cost a signed-in
 * user their product, or make a cold start look like a sign-out.
 */

/** A reachable deployment with nobody signed in. */
function stubSignedOut({ demoLoginEnabled = true } = {}) {
  const urls: string[] = [];
  const json = (status: number, body: unknown) =>
    Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      statusText: "",
      json: async () => body,
    } as Response);

  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      urls.push(url);
      if (url.includes("/health")) {
        return json(200, {
          ...health,
          auth_mode: "session",
          demo_login_enabled: demoLoginEnabled,
        });
      }
      if (url.includes("/api/principals")) return json(200, { principals: [] });
      if (url.includes("/api/auth/me")) {
        return json(401, {
          error: { code: "unauthenticated", message: "Sign in.", details: {} },
        });
      }
      throw new Error(`unexpected request: ${url}`);
    }),
  );
  return { urls };
}

const HEADLINE = /build what.s next with ai that understands/i;

async function renderLanding(options?: { demoLoginEnabled?: boolean }) {
  const user = userEvent.setup();
  setTestRoute("/");
  const stub = stubSignedOut(options);
  renderApp(<SupportPage />);
  await screen.findByRole("heading", { level: 1, name: HEADLINE });
  // Let the session settle, so what is asserted is the resting state.
  await waitFor(() =>
    expect(stub.urls.some((url) => url.includes("/api/auth/me"))).toBe(true),
  );
  return { user, stub };
}

describe("the landing page", () => {
  it("is what a signed-out visitor sees at /", async () => {
    await renderLanding();

    expect(screen.getByText(/ai for real work/i)).toBeInTheDocument();
    expect(
      screen.getByText(/astrion helps teams turn complex data, documents, and decisions/i),
    ).toBeInTheDocument();
    for (const value of [
      "Trusted by teams",
      "Secure & private",
      "From questions to action",
    ]) {
      expect(screen.getByText(value)).toBeInTheDocument();
    }

    // The landing page, not a form and not the product.
    expect(screen.queryByLabelText(/^email$/i)).toBeNull();
    expect(screen.queryByRole("navigation", { name: /primary/i })).toBeNull();
  });

  it("leads to the existing sign-in and registration forms", async () => {
    await renderLanding();

    expect(screen.getByRole("link", { name: /^sign in$/i })).toHaveAttribute(
      "href",
      "/sign-in",
    );
    const getStarted = screen.getAllByRole("link", { name: /get started/i });
    expect(getStarted).toHaveLength(2);
    for (const link of getStarted) {
      expect(link).toHaveAttribute("href", "/get-started");
    }
  });

  it("Sign in opens the sign-in form", async () => {
    const { user } = await renderLanding();

    await user.click(screen.getByRole("link", { name: /^sign in$/i }));

    expect(testRoute()).toBe("/sign-in");
    expect(
      await screen.findByRole("heading", { name: /^sign in$/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/^email$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
  });

  it("Get Started opens registration, the existing onboarding path", async () => {
    const { user } = await renderLanding();

    const [, heroCta] = screen.getAllByRole("link", { name: /get started/i });
    await user.click(heroCta!);

    expect(testRoute()).toBe("/get-started");
    expect(
      await screen.findByRole("heading", { name: /create your account/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
  });

  it("offers no demo account, even when the server has one", async () => {
    const { user } = await renderLanding({ demoLoginEnabled: true });

    expect(screen.queryByText(/demo account|public demo/i)).toBeNull();

    await user.click(screen.getByRole("link", { name: /^sign in$/i }));
    await screen.findByRole("heading", { name: /^sign in$/i });

    expect(screen.queryByRole("button", { name: /sign in to the demo/i })).toBeNull();
    expect(screen.queryByText(/public demo/i)).toBeNull();
  });
});

describe("Watch Demo", () => {
  it("is present as designed, and says it is not available yet", async () => {
    const { user } = await renderLanding();

    const button = screen.getByRole("button", { name: /^watch demo$/i });
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAccessibleDescription(/not available yet/i);

    await user.click(button);
    // Nothing happens: no navigation, no dialog, no video.
    expect(testRoute()).toBe("/");
    expect(document.querySelector("video")).toBeNull();
  });

  it("becomes a link to the recording once one exists", () => {
    render(<WatchDemoButton videoUrl="https://example.com/astrion-demo" />);

    const link = screen.getByRole("link", { name: /watch demo/i });
    expect(link).toHaveAttribute("href", "https://example.com/astrion-demo");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });
});

describe("while the backend is still waking", () => {
  it(
    "shows a first-time visitor the landing page, not a wait",
    async () => {
      setTestRoute("/");
      stubApi({ session: {}, sleeping: 2 });
      renderApp(<SupportPage />);

      expect(
        await screen.findByRole("heading", { level: 1, name: HEADLINE }),
      ).toBeInTheDocument();
      expect(screen.queryByText(/waking the astrion api/i)).toBeNull();

      // And a session that turns out to exist still reaches the product.
      await screen.findByText("Northstar Logistics", undefined, { timeout: 8000 });
      expect(screen.queryByRole("heading", { level: 1, name: HEADLINE })).toBeNull();
    },
    15_000,
  );
});

describe("the sign-in routes, when already signed in", () => {
  it("send a signed-in user on to the product", async () => {
    setTestRoute("/sign-in");
    stubApi({ session: {} });
    renderApp(<SignInPage />);

    await waitFor(() => expect(testRoute()).toBe("/"));
  });
});

describe("the connecting animation", () => {
  it("plays the delivery-truck loop where motion is welcome", () => {
    render(<LoadingTruck />);

    const video = screen.getByTestId("loading-truck") as HTMLVideoElement;
    expect(video.getAttribute("src")).toBe(LOADING_TRUCK_SRC);
    expect(video.muted).toBe(true);
    expect(video).toHaveAttribute("loop");
    // Decorative: the notice's text is what is announced.
    expect(video.closest("[aria-hidden='true']")).not.toBeNull();
  });

  it("is served from the committed brand asset", () => {
    const file = readFileSync(join(process.cwd(), "public", LOADING_TRUCK_SRC));

    // The EBML signature: a real WebM, not a missing file or a placeholder.
    expect([...file.subarray(0, 4)]).toEqual([0x1a, 0x45, 0xdf, 0xa3]);
  });

  it("falls back to the static dot when the asset cannot load", () => {
    const { container } = render(<LoadingTruck />);

    fireEvent.error(screen.getByTestId("loading-truck"));

    expect(screen.queryByTestId("loading-truck")).toBeNull();
    expect(container.querySelector("span > span")).not.toBeNull();
  });

  it("never plays for a reader who prefers reduced motion", () => {
    vi.spyOn(window, "matchMedia").mockImplementation(
      (query: string) =>
        ({
          matches: query.includes("prefers-reduced-motion"),
          media: query,
          addEventListener: () => {},
          removeEventListener: () => {},
        }) as unknown as MediaQueryList,
    );

    render(<LoadingTruck />);

    expect(screen.queryByTestId("loading-truck")).toBeNull();
  });
});
