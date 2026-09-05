import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import AuditPage from "./page";
import { sessionUser, sessionWorkspace } from "@/test/helpers";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";
import type { AuditEntry } from "@/lib/auth-types";

/**
 * The audit view.
 *
 * The trail was built long before it was reachable: the interface told users
 * their work was audited, granted `read_audit_log` to three roles, and offered
 * nothing to read it with. These tests exist so that cannot silently become
 * true again — and so the screen keeps saying the two things a reader needs
 * that a plain list would not: whether the chain verifies, and that a refusal
 * is the control working rather than a fault.
 */

function entry(overrides: Partial<AuditEntry> = {}): AuditEntry {
  return {
    seq: 1,
    event_id: `EVT-${Math.random().toString(16).slice(2)}`,
    occurred_at_utc: "2026-08-16T09:30:00+00:00",
    event_type: "action.executed",
    outcome: "success",
    actor_user_id: "USR-ada",
    actor_role: "operations",
    org_id: "ORG-test",
    target_type: "order",
    target_id: "ORD-2002",
    request_id: "REQ-abc",
    details: { action_type: "issue_service_credit" },
    ...overrides,
  };
}

function stubAudit(reply: {
  status?: number;
  body?: unknown;
  networkError?: boolean;
  /** Holds the audit response open so the in-flight state is observable. */
  hold?: Promise<void>;
}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();

      if (url.includes("/api/auth/audit")) {
        if (reply.hold) await reply.hold;
        if (reply.networkError) throw new TypeError("Failed to fetch");
        const status = reply.status ?? 200;
        return {
          ok: status >= 200 && status < 300,
          status,
          statusText: "",
          json: async () => reply.body,
        } as Response;
      }
      if (url.includes("/health")) {
        return json({ status: "ok", auth_mode: "session", documents_indexed: 0 });
      }
      if (url.includes("/api/principals")) return json({ principals: [] });
      if (url.includes("/api/auth/me")) return json(sessionUser());
      if (url.includes("/api/workspaces")) {
        return json({
          workspaces: [sessionWorkspace()],
          active_workspace_id: "ORG-test",
          needs_workspace: false,
        });
      }
      throw new Error(`unexpected request: ${url}`);
    }) as unknown as typeof fetch,
  );
}

function json(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "",
    json: async () => body,
  } as Response;
}

function listing(events: AuditEntry[], overrides: Record<string, unknown> = {}) {
  return {
    org_id: "ORG-test",
    chain_intact: true,
    first_invalid_seq: null,
    events,
    ...overrides,
  };
}

function render() {
  setTestRoute("/workspace/audit");
  renderApp(<AuditPage />);
}

describe("the audit trail", () => {
  it("renders an entry as a sentence about what somebody did", async () => {
    stubAudit({ body: listing([entry()]) });
    render();

    const entryRow = (
      await screen.findByText("Confirmed and executed an action")
    ).closest("li")!;
    expect(within(entryRow).getByText("USR-ada")).toBeInTheDocument();
    expect(within(entryRow).getByText("order ORD-2002")).toBeInTheDocument();
    // Wire identifiers never reach the screen.
    expect(screen.queryByText("action.executed")).toBeNull();
  });

  it("shows a refusal as the control working, not as a fault", async () => {
    stubAudit({
      body: listing([
        entry({
          event_type: "authz.denied",
          outcome: "denied",
          details: { permission: "approve_high_value_action" },
        }),
      ]),
    });
    render();

    expect(await screen.findByText("Refused: not permitted")).toBeInTheDocument();
    expect(screen.getByText("Refused")).toBeInTheDocument();
  });

  it("says the chain verifies when it does", async () => {
    stubAudit({ body: listing([entry()]) });
    render();

    expect(await screen.findByText(/chain verified/i)).toBeInTheDocument();
    expect(screen.queryByText(/has been altered/i)).toBeNull();
  });

  it("warns loudly when the chain does not verify", async () => {
    // A list rendered as though it were intact is worse than no list.
    stubAudit({
      body: listing([entry()], { chain_intact: false, first_invalid_seq: 7 }),
    });
    render();

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/has been altered/i)).toBeInTheDocument();
    expect(within(alert).getByText(/entry 7/)).toBeInTheDocument();
    expect(within(alert).getByText(/unproven/i)).toBeInTheDocument();
  });

  it("shows a loading state while the trail is in flight", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    stubAudit({ body: listing([entry()]), hold: held });
    render();

    expect(
      await screen.findByText(/loading the audit trail/i),
    ).toBeInTheDocument();

    release!();
    expect(
      await screen.findByText("Confirmed and executed an action"),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText(/loading the audit trail/i)).toBeNull(),
    );
  });

  it("says an empty trail is empty, not broken", async () => {
    stubAudit({ body: listing([]) });
    render();

    expect(await screen.findByText(/nothing recorded yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders the server's refusal for a role without the permission", async () => {
    // The backend is the boundary. A support user who types the URL gets its
    // refusal, not a blank page that merely looks like one.
    stubAudit({
      status: 403,
      body: {
        error: {
          code: "forbidden",
          message:
            "This action requires the 'read_audit_log' permission, which your role in this workspace does not grant.",
        },
      },
    });
    render();

    expect(
      await screen.findByText(/cannot read the audit trail/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/read_audit_log/)).toBeInTheDocument();
    // Nothing is offered to retry with: retrying will not grant a permission.
    expect(screen.queryByRole("button", { name: /try again/i })).toBeNull();
  });

  it("reports an API failure rather than an empty trail", async () => {
    stubAudit({ networkError: true });
    render();

    expect(
      await screen.findByText(/could not load the audit trail/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/nothing recorded yet/i)).toBeNull();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("filters by event type without claiming to search the whole trail", async () => {
    const user = userEvent.setup();
    stubAudit({
      body: listing([
        entry({ seq: 2, event_type: "action.executed" }),
        entry({ seq: 1, event_type: "login.succeeded", target_id: null }),
      ]),
    });
    render();

    await screen.findByText("Confirmed and executed an action");
    expect(screen.getByText("Signed in")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText(/event type/i), "actions");

    expect(screen.getByText("Confirmed and executed an action")).toBeInTheDocument();
    expect(screen.queryByText("Signed in")).toBeNull();
    expect(
      screen.getByText(/applies to what is loaded here, not to the whole trail/i),
    ).toBeInTheDocument();
  });

  it("filters by who did it", async () => {
    const user = userEvent.setup();
    stubAudit({
      body: listing([
        entry({ seq: 2, actor_user_id: "USR-ada" }),
        entry({
          seq: 1,
          actor_user_id: "USR-bo",
          event_type: "login.succeeded",
          target_id: null,
        }),
      ]),
    });
    render();

    await screen.findByText("Confirmed and executed an action");
    await user.selectOptions(screen.getByLabelText(/who/i), "USR-bo");

    expect(screen.getByText("Signed in")).toBeInTheDocument();
    expect(screen.queryByText("Confirmed and executed an action")).toBeNull();
  });

  it("says so when a filter matches nothing", async () => {
    const user = userEvent.setup();
    stubAudit({ body: listing([entry({ event_type: "login.succeeded" })]) });
    render();

    await screen.findByText("Signed in");
    await user.selectOptions(screen.getByLabelText(/event type/i), "actions");

    expect(screen.getByText(/no entries match this filter/i)).toBeInTheDocument();
  });

  it("summarises a long list instead of printing every identifier", async () => {
    // `agent.invoked` records every chunk it read. Printed in full, one entry
    // pushes the next four events off the screen.
    stubAudit({
      body: listing([
        entry({
          event_type: "agent.invoked",
          details: {
            trust_status: "confident",
            source_chunk_ids: ["c01", "c02", "c03", "c04", "c05", "c06"],
            operational_signal_ids: [],
          },
        }),
      ]),
    });
    render();

    expect(
      await screen.findByText("c01, c02, c03 and 3 more"),
    ).toBeInTheDocument();
    // An empty list is not a fact about anything.
    expect(screen.queryByText(/operational signal ids/i)).toBeNull();
  });

  it("keeps an unrecognised event visible rather than dropping it", async () => {
    // A trail that silently omits rows it was not taught about is worse than
    // one that shows an ugly name.
    stubAudit({ body: listing([entry({ event_type: "something.new" })]) });
    render();

    expect(await screen.findByText("Something new")).toBeInTheDocument();
  });
});
