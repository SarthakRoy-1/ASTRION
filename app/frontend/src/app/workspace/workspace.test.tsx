import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import WorkspacePage from "./page";
import { stubApi } from "@/test/helpers";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

/**
 * The workspace area.
 *
 * Two things it is responsible for beyond listing people. It has to make
 * "why can't I do this here?" answerable — the same person can hold a
 * different role in each workspace, and nothing used to say which one was in
 * force. And it has to keep the product's vocabulary: the API's internal
 * `org` / `org_id` never reaches a screen.
 */

async function renderWorkspace(options: Parameters<typeof stubApi>[0] = {}) {
  const user = userEvent.setup();
  setTestRoute("/workspace");
  const stub = stubApi({ session: {}, ...options });
  renderApp(<WorkspacePage />);
  await screen.findByRole("heading", { name: "Northstar Logistics", level: 1 });
  return { user, stub };
}

describe("workspace identity", () => {
  it("states who you are, your role here, and what it reaches", async () => {
    await renderWorkspace();

    const access = screen.getByRole("region", { name: /your access/i });
    expect(within(access).getByText("Ada Support")).toBeInTheDocument();
    expect(within(access).getByText("Operations")).toBeInTheDocument();
    // The slug is the only stable, human-readable way to tell two workspaces
    // with the same name apart.
    expect(within(access).getByText("northstar-logistics")).toBeInTheDocument();
    expect(within(access).getByText(/ACCT-001/)).toBeInTheDocument();
  });

  it("never calls a workspace an organization", async () => {
    await renderWorkspace();
    expect(screen.queryByText(/organi[sz]ation/i)).toBeNull();
    expect(screen.queryByText(/org_id/i)).toBeNull();
  });

  it("names each permission in words rather than as a wire identifier", async () => {
    await renderWorkspace();

    const grants = screen.getByRole("region", { name: /what your role grants/i });
    expect(within(grants).getByText("Invite people")).toBeInTheDocument();
    expect(within(grants).queryByText("members.invite")).toBeNull();
  });
});

describe("members", () => {
  it("lists members with their role", async () => {
    await renderWorkspace();

    const members = await screen.findByRole("region", { name: /^members$/i });
    expect(within(members).getByText(/Ada Support/)).toBeInTheDocument();
    expect(within(members).getByText("ada@northstar.example")).toBeInTheDocument();
  });

  it("explains what each role can do, without making the reader guess", async () => {
    await renderWorkspace();

    const reference = await screen.findByRole("region", {
      name: /what each role can do/i,
    });
    expect(
      within(reference).getByText(/Read-only\. Can ask questions/i),
    ).toBeInTheDocument();
  });

  it("confirms before removing someone, and can be cancelled", async () => {
    const { user } = await renderWorkspace();

    await user.click(await screen.findByRole("button", { name: /leave/i }));

    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByText(/lose access to Northstar Logistics/i),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: /cancel/i }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes the confirmation on Escape", async () => {
    const { user } = await renderWorkspace();

    await user.click(await screen.findByRole("button", { name: /leave/i }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("moves focus into the confirmation and back out again", async () => {
    const { user } = await renderWorkspace();

    const opener = await screen.findByRole("button", { name: /leave/i });
    await user.click(opener);

    // The dialog takes focus, so a keyboard user is not left behind it.
    const dialog = screen.getByRole("dialog");
    expect(dialog.contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");
    // And gives it back to the control that opened it, rather than dropping
    // the keyboard at the top of the document.
    expect(document.activeElement).toBe(opener);
  });

  it("hides the member list from a role that cannot read it", async () => {
    await renderWorkspace({ session: { permissions: ["run_agent"] } });

    expect(
      await screen.findByText(/cannot see the member list/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /^members$/i })).toBeNull();
  });

  it("offers no invitation form to a role that cannot invite", async () => {
    await renderWorkspace({
      session: { permissions: ["run_agent", "members.read"] },
    });

    await screen.findByRole("region", { name: /^members$/i });
    expect(screen.queryByRole("region", { name: /invite someone/i })).toBeNull();
  });
});
