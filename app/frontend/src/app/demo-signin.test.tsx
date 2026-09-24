import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import SupportPage from "./page";
import health from "@/test/fixtures/health.json";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

// The public demo is switched off in the shipped frontend for now
// (`PUBLIC_DEMO_SIGN_IN_ENABLED`), but every part of it is kept so it can be
// restored. These tests keep that dormant flow honest by turning the switch on
// for this file; `landing.test.tsx` asserts that the shipped default hides it.
vi.mock("@/lib/features", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/features")>()),
  PUBLIC_DEMO_SIGN_IN_ENABLED: true,
}));

/**
 * The public demo, from the visitor's side.
 *
 * The deployment this covers sleeps when idle and wakes with an empty disk, so
 * the first person to arrive after a quiet hour used to be met by a sign-in
 * page reporting that the database had not been built and inviting them to run
 * two Python scripts. What replaced it is one button.
 *
 * These tests are about the frontend's half of that, which is narrow and worth
 * stating exactly:
 *
 * - it asks the *server* whether a demo exists, rather than deciding from a
 *   build-time variable that may disagree with the backend it is talking to;
 * - it sends no credential, because it has none to send;
 * - and when the backend is awake but still building, it says so in words a
 *   visitor can act on instead of repeating an operator's instructions.
 */

interface Recorded {
  url: string;
  method: string;
  body: unknown;
}

/**
 * A backend for the sign-in page alone.
 *
 * Deliberately not `stubApi`: every fixture there describes a signed-in
 * deployment, and the whole subject here is the state *before* anybody is
 * signed in.
 */
function stubSignIn(
  options: {
    demoLoginEnabled?: boolean;
    /** Replaces the `/api/auth/me` reply until the demo sign-in succeeds. */
    meError?: { status: number; code: string; message: string };
  } = {},
) {
  const calls: Recorded[] = [];
  let signedIn = false;

  const json = (status: number, body: unknown) =>
    Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      statusText: "",
      json: async () => body,
    } as Response);

  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      calls.push({
        url,
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(init.body as string) : null,
      });

      if (url.includes("/health")) {
        return json(200, {
          ...health,
          auth_mode: "session",
          demo_login_enabled: options.demoLoginEnabled ?? true,
        });
      }
      if (url.includes("/api/principals")) return json(200, { principals: [] });

      if (url.includes("/api/auth/demo-login")) {
        signedIn = true;
        return json(200, {
          status: "authenticated",
          mfa_required: false,
          user_id: "USR-demo",
          org_id: "ORG-demo",
        });
      }

      if (url.includes("/api/auth/me")) {
        if (options.meError && !signedIn) {
          const { status, code, message } = options.meError;
          return json(status, { error: { code, message, details: {} } });
        }
        if (!signedIn) {
          return json(401, {
            error: { code: "unauthenticated", message: "Sign in.", details: {} },
          });
        }
        return json(200, {
          user_id: "USR-demo",
          display_name: "Demo support",
          org_id: "ORG-demo",
          org_name: "ASTRION Demo",
          role: "support",
          permissions: ["view_orders", "view_tickets"],
          account_scope: ["ACCT-001"],
          memberships: [],
          auth_mode: "session",
        });
      }

      if (url.includes("/api/workspaces")) {
        return json(200, {
          workspaces: [
            {
              workspace_id: "ORG-demo",
              name: "ASTRION Demo",
              slug: "astrion-demo",
              created_at_utc: "2026-08-01T00:00:00Z",
              role: "support",
              permissions: ["view_orders", "view_tickets"],
            },
          ],
          active_workspace_id: "ORG-demo",
          needs_workspace: false,
        });
      }

      if (url.includes("/api/operations/signals")) {
        return json(200, {
          signals: [],
          count: 0,
          highest_severity: null,
          reference_time: null,
          scope_account_ids: [],
        });
      }

      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    }),
  );

  return { calls };
}

async function renderSignIn(options: Parameters<typeof stubSignIn>[0] = {}) {
  const user = userEvent.setup();
  // The sign-in form lives at `/sign-in`; `/` is the public landing page.
  setTestRoute("/sign-in");
  const stub = stubSignIn(options);
  renderApp(<SupportPage />);
  // The form itself: while the API is still being reached the page already
  // shows a "Sign in" title over the connection notice.
  await screen.findByLabelText(/^password$/i);
  return { user, stub };
}

describe("entering the public demo", () => {
  it("offers a demo button when the server says there is one", async () => {
    await renderSignIn();

    expect(
      await screen.findByRole("button", { name: /sign in to the demo/i }),
    ).toBeInTheDocument();
  });

  it("offers none when the server says there is not", async () => {
    await renderSignIn({ demoLoginEnabled: false });

    // The deployment decides. A frontend that decided for itself could offer a
    // button its own backend would answer with a 404.
    expect(
      screen.queryByRole("button", { name: /sign in to the demo/i }),
    ).toBeNull();
    expect(screen.getByLabelText(/^work email address$/i)).toBeInTheDocument();
  });

  it("gets the visitor in without them typing anything", async () => {
    const { user, stub } = await renderSignIn();

    await user.click(
      await screen.findByRole("button", { name: /sign in to the demo/i }),
    );

    // The product, not the sign-in page: the demo workspace's name is the
    // first thing on screen that only appears once the session resolved.
    expect(await screen.findByText("ASTRION Demo")).toBeInTheDocument();

    const demo = stub.calls.filter((call) => call.url.includes("/demo-login"));
    expect(demo).toHaveLength(1);
    expect(demo[0]?.method).toBe("POST");
    // No body at all — there is no address and no password to send, which is
    // what keeps both out of the bundle and out of every log along the way.
    expect(demo[0]?.body).toBeNull();
  });

  it("never sends a credential on the way in", async () => {
    const { user, stub } = await renderSignIn();

    await user.click(
      await screen.findByRole("button", { name: /sign in to the demo/i }),
    );
    await screen.findByText("ASTRION Demo");

    for (const call of stub.calls) {
      expect(JSON.stringify(call.body ?? null)).not.toMatch(/password/i);
      expect(call.url).not.toMatch(/password|email=/i);
    }
    // And the ordinary login endpoint was never involved.
    expect(stub.calls.some((call) => call.url.endsWith("/api/auth/login"))).toBe(
      false,
    );
  });

  it("says the environment is starting rather than naming a script", async () => {
    // The deployed symptom exactly: the backend answers, its data does not
    // exist yet, and `/api/auth/me` refuses with `data_unavailable`.
    const { stub } = await renderSignIn({
      meError: {
        status: 503,
        code: "data_unavailable",
        message:
          "The ASTRION database has not been built. Run "
          + "`python scripts/ingest_dataset.py` and "
          + "`python scripts/ingest_documents.py`, then retry.",
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/still starting up/i)).toBeInTheDocument(),
    );
    // Nobody visiting a public demo can run either of these.
    expect(screen.queryByText(/ingest_dataset/)).toBeNull();
    expect(screen.queryByText(/python scripts/)).toBeNull();

    // And the way forward is still on screen rather than replaced by the error.
    expect(
      screen.getByRole("button", { name: /sign in to the demo/i }),
    ).toBeInTheDocument();
    expect(stub.calls.some((call) => call.url.includes("/api/auth/me"))).toBe(true);
  });

  it("recovers from that state when the visitor clicks", async () => {
    const { user } = await renderSignIn({
      meError: {
        status: 503,
        code: "data_unavailable",
        message: "The ASTRION database has not been built.",
      },
    });

    await user.click(
      await screen.findByRole("button", { name: /sign in to the demo/i }),
    );

    // The backend built what was missing and signed them in; the frontend did
    // nothing about the database except ask.
    expect(await screen.findByText("ASTRION Demo")).toBeInTheDocument();
  });
});
