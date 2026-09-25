import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import WorkspacePage from "./page";
import { stubApi } from "@/test/helpers";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

/**
 * How people join a workspace, as its owner sees it: the code, and the means of
 * replacing the password. Anyone who is not the owner sees neither.
 */

const OWNER = {
  role: "owner" as const,
  workspace_code: "K7Q2M9XPAB",
  permissions: [
    "run_agent",
    "read_records",
    "members.read",
    "members.invite",
    "workspace.read",
    "workspace.credentials",
  ],
};

async function renderWorkspace(session: Parameters<typeof stubApi>[0] = {}) {
  const user = userEvent.setup();
  setTestRoute("/workspace");
  const stub = stubApi(session);
  renderApp(<WorkspacePage />);
  await screen.findByRole("heading", { name: "Northstar Logistics", level: 1 });
  return { user, stub };
}

async function fillAndSubmit(
  user: ReturnType<typeof userEvent.setup>,
  password: string,
  confirm: string,
) {
  const form = screen.getByRole("form", { name: /change the workspace password/i });
  await user.type(within(form).getByLabelText("New workspace password"), password);
  await user.type(within(form).getByLabelText("Confirm new workspace password"), confirm);
  await user.click(within(form).getByRole("button", { name: /change workspace password/i }));
  return form;
}

describe("the owner's workspace access", () => {
  it("shows the workspace code and a way to change the password", async () => {
    await renderWorkspace({ session: { workspace: OWNER } });

    const panel = screen.getByRole("region", { name: /workspace access/i });
    expect(within(panel).getByLabelText("Workspace code")).toHaveTextContent("K7Q2M9XPAB");
    expect(within(panel).getByLabelText("New workspace password")).toHaveAttribute(
      "type",
      "password",
    );
    // The current password is not on offer to read or to type: only a hash of it exists.
    expect(within(panel).queryByLabelText(/current/i)).toBeNull();
    expect(within(panel).getByText(/isn.t shown/i)).toBeInTheDocument();
  });

  it("changes the password, sends it twice, and clears the fields", async () => {
    const { user, stub } = await renderWorkspace({ session: { workspace: OWNER } });

    const form = await fillAndSubmit(user, "a-fresh-passphrase", "a-fresh-passphrase");

    const call = stub.calls.find((c) => c.url.includes("/password"));
    expect(call?.method).toBe("POST");
    expect(call?.url).toContain("/api/workspaces/ORG-test/password");
    expect(call?.body).toEqual({
      new_password: "a-fresh-passphrase",
      confirm_new_password: "a-fresh-passphrase",
    });
    expect(await screen.findByText(/the old workspace password no longer works/i)).toBeInTheDocument();
    expect(within(form).getByLabelText("New workspace password")).toHaveValue("");
    expect(within(form).getByLabelText("Confirm new workspace password")).toHaveValue("");
  });

  it("refuses a mismatched confirmation without asking the server", async () => {
    const { user, stub } = await renderWorkspace({ session: { workspace: OWNER } });

    await fillAndSubmit(user, "a-fresh-passphrase", "a-fresh-passphrasE");

    expect(screen.getByText("The workspace passwords do not match.")).toBeInTheDocument();
    expect(stub.calls.some((c) => c.url.includes("/password"))).toBe(false);
  });

  it("shows the server's refusal", async () => {
    const { user } = await renderWorkspace({
      session: {
        workspace: OWNER,
        passwordChange: {
          status: 400,
          body: {
            error: {
              code: "invalid_request",
              message: "Workspace password must be at least 8 characters.",
              details: {},
            },
          },
        },
      },
    });

    await fillAndSubmit(user, "long-enough-here", "long-enough-here");

    expect(await screen.findByRole("alert")).toHaveTextContent(/at least 8 characters/i);
  });
});

describe("everyone else", () => {
  it("is shown neither the code nor the password form", async () => {
    await renderWorkspace({ session: { workspace: { role: "operations" } } });

    expect(screen.queryByRole("region", { name: /workspace access/i })).toBeNull();
    expect(screen.queryByText("K7Q2M9XPAB")).toBeNull();
    expect(screen.queryByLabelText("New workspace password")).toBeNull();
  });

  it("does not list the change-password capability among a non-owner's grants", async () => {
    await renderWorkspace({ session: { workspace: { role: "operations" } } });

    const grants = screen.getByRole("region", { name: /what your role grants/i });
    expect(within(grants).queryByText("Change the workspace password")).toBeNull();
  });

  it("names the capability in words for an owner", async () => {
    await renderWorkspace({ session: { workspace: OWNER } });

    const grants = screen.getByRole("region", { name: /what your role grants/i });
    expect(within(grants).getByText("Change the workspace password")).toBeInTheDocument();
    expect(within(grants).queryByText("workspace.credentials")).toBeNull();
  });
});

describe("adding people where invitations are off", () => {
  it("points to the workspace code instead of offering an invitation", async () => {
    await renderWorkspace({ session: { workspace: OWNER, invitationsEnabled: false } });

    expect(await screen.findByText(/invitations are turned off on this deployment/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /invite someone/i })).toBeNull();
  });

  it("still offers invitations where they are on", async () => {
    await renderWorkspace({ session: { workspace: OWNER, invitationsEnabled: true } });

    expect(await screen.findByRole("heading", { name: /invite someone/i })).toBeInTheDocument();
  });
});
