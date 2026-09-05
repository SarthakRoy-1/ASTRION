import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import JoinPage from "./page";
import { sessionUser, sessionWorkspace, stubApi } from "@/test/helpers";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

/**
 * Accepting an invitation.
 *
 * `POST /api/invitations/accept` and its client wrapper both existed before
 * Phase 4, and nothing in the interface ever called either — so every
 * invitation the members screen issued was unacceptable, and the onboarding
 * copy pointed at a link with no screen behind it. These tests exist so that
 * cannot silently become true again.
 */

/** A signed-in user who belongs to no workspace yet — that is, an invitee. */
function stubInvitee() {
  const accept = vi.fn(async () => sessionWorkspace());

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";

    if (url.includes("/api/invitations/accept")) {
      const workspace = await accept();
      return json(workspace);
    }
    if (url.includes("/health")) {
      return json({ status: "ok", auth_mode: "session", documents_indexed: 0 });
    }
    if (url.includes("/api/principals")) return json({ principals: [] });
    if (url.includes("/api/auth/me")) {
      return json(sessionUser({ display_name: "Bo Invitee" }));
    }
    if (url.includes("/api/workspaces")) {
      return json(
        accept.mock.calls.length > 0
          ? {
              workspaces: [sessionWorkspace()],
              active_workspace_id: "ORG-test",
              needs_workspace: false,
            }
          : { workspaces: [], active_workspace_id: null, needs_workspace: true },
      );
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  });

  vi.stubGlobal("fetch", fetchMock);
  return { accept };
}

/**
 * Wait for the session to settle before touching anything.
 *
 * The frame swaps its whole subtree as the session resolves — loading, then
 * signed-out, then onboarding — so a control queried before that has finished
 * is a detached node by the time it is clicked. The signed-in name is the
 * first thing on screen that only appears once identity has landed.
 */
async function settled() {
  await screen.findByText(/signed in as/i);
}

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    json: async () => body,
  } as Response);
}

describe("accepting an invitation", () => {
  it("is reachable by someone who belongs to no workspace yet", async () => {
    // Gating this behind "has a workspace" would make every invitation
    // unacceptable, since accepting one is how an invitee gets their first.
    stubInvitee();
    setTestRoute("/join?token=inv-token-123");
    renderApp(<JoinPage />);

    expect(
      await screen.findByRole("heading", { name: /accept your invitation/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /create your first workspace/i }),
    ).toBeNull();
  });

  it("says which identity will be joined, because only one can accept", async () => {
    stubInvitee();
    setTestRoute("/join?token=inv-token-123");
    renderApp(<JoinPage />);

    expect(await screen.findByText("Bo Invitee")).toBeInTheDocument();
    expect(
      screen.getByText(/only be accepted by the address it was sent to/i),
    ).toBeInTheDocument();
  });

  it("posts the token and confirms which workspace was joined", async () => {
    const { accept } = stubInvitee();
    const user = userEvent.setup();
    setTestRoute("/join?token=inv-token-123");
    renderApp(<JoinPage />);

    await settled();
    await user.click(
      screen.getByRole("button", { name: /accept invitation/i }),
    );

    await waitFor(() => expect(accept).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByRole("heading", { name: /you have joined/i }),
    ).toBeInTheDocument();
  });

  it("says so plainly when the link carries no token", async () => {
    stubInvitee();
    setTestRoute("/join");
    renderApp(<JoinPage />);

    expect(
      await screen.findByText(/link is missing its token/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /accept invitation/i }),
    ).toBeNull();
  });

  it("shows the server's refusal verbatim rather than guessing at it", async () => {
    const user = userEvent.setup();
    setTestRoute("/join?token=expired");

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.includes("/api/invitations/accept")) {
          return json(
            {
              error: {
                code: "invalid_request",
                message: "That invitation has expired.",
              },
            },
            400,
          );
        }
        if (url.includes("/health")) {
          return json({ status: "ok", auth_mode: "session", documents_indexed: 0 });
        }
        if (url.includes("/api/principals")) return json({ principals: [] });
        if (url.includes("/api/auth/me")) return json(sessionUser());
        if (url.includes("/api/workspaces")) {
          return json({
            workspaces: [],
            active_workspace_id: null,
            needs_workspace: true,
          });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    renderApp(<JoinPage />);
    await settled();
    await user.click(
      screen.getByRole("button", { name: /accept invitation/i }),
    );

    expect(
      await screen.findByText("That invitation has expired."),
    ).toBeInTheDocument();
  });
});

// `stubApi` is not used here: this flow needs a session that owns no workspace,
// which is a shape the shared stub deliberately does not offer.
void stubApi;
