import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import OperationsPage from "./page";
import SupportPage from "../page";
import { stubApi } from "@/test/helpers";
import { setTestRoute, testRoute, usePathname } from "@/test/next-navigation";
import { renderApp } from "@/test/render";
import type { OperationalSignal, SignalReport } from "@/lib/operations-types";

/**
 * Both areas, mounted the way the layout mounts them.
 *
 * The conversation and the session live above the router, so navigating from
 * Operations to Support swaps the page body and nothing else. Rendering a
 * second tree instead would create a second set of providers and lose exactly
 * the state this journey is about.
 */
function Areas() {
  const pathname = usePathname();
  return pathname.startsWith("/operations") ? <OperationsPage /> : <SupportPage />;
}

/**
 * The operations inbox.
 *
 * These replace the tests that drove the earlier pop-up panel. Every one of
 * that panel's assertions survives — the ranking shown honestly, doubt shown
 * where the detector expressed it, an empty result that is not an all-clear, a
 * failure that is not an empty result, and nothing on the screen that acts —
 * re-pointed at the screen those behaviours now live on, plus what the panel
 * could not do: be linked to, and hand a signal to the assistant without
 * losing it.
 *
 * The shapes mirror what `GET /api/operations/signals` returns; the backend's
 * own contract tests pin that, so these concentrate on the interface.
 */

function signal(overrides: Partial<OperationalSignal> = {}): OperationalSignal {
  return {
    signal_id: "SLA-TKT-001",
    signal_type: "sla_risk",
    severity: "critical",
    priority_score: 52,
    priority_factors: [
      { name: "severity", points: 40, basis: "detector severity is critical" },
      { name: "signal_type", points: 15, basis: "sla_risk needs faster handling" },
      { name: "documented", points: -3, basis: "matches 1 documented section" },
    ],
    title: "TKT-001 has no first response",
    detail: "No first response after 150 minutes, past every computable target.",
    affected_account_ids: ["ACCT-001"],
    affected_account_count: 1,
    affected_ticket_count: 1,
    affected_order_count: 0,
    records: [
      {
        kind: "ticket",
        record_id: "TKT-001",
        account_id: "ACCT-001",
        label: "Shipment creation failing",
      },
    ],
    documentation_chunk_ids: ["doc#c01"],
    first_observed_at: "2026-08-16T08:30:00+05:30",
    last_observed_at: "2026-08-16T11:00:00+05:30",
    trust_status: "confident",
    trust_reasons: [],
    recommended_next_step: "Respond now and escalate.",
    ...overrides,
  };
}

function report(signals: OperationalSignal[]): SignalReport {
  return {
    signals,
    count: signals.length,
    highest_severity: signals[0]?.severity ?? null,
    reference_time: "2026-08-16T11:00:00+05:30",
    scope_account_ids: ["ACCT-001"],
  };
}

async function renderOperations(signals: SignalReport | { status: number }) {
  const user = userEvent.setup();
  setTestRoute("/operations");
  const stub = stubApi({ session: { signals: signals as SignalReport } });
  renderApp(<OperationsPage />);
  return { user, stub };
}

describe("operations inbox", () => {
  it("ranks signals with their severity, scope and priority", async () => {
    await renderOperations(report([signal()]));

    const row = await screen.findByRole("button", {
      name: /TKT-001 has no first response/i,
    });
    expect(within(row).getByText("Critical")).toBeInTheDocument();
    expect(within(row).getByText("SLA risk")).toBeInTheDocument();
    expect(within(row).getByText("52")).toBeInTheDocument();
    // The recommended step is on the row: the point of the ranking is what to
    // do about the top item, not that there is a top item.
    expect(within(row).getByText(/Respond now and escalate/)).toBeInTheDocument();
  });

  it("states what the signals were measured against", async () => {
    // "150 minutes overdue" is meaningless without saying overdue relative to
    // what. The dataset snapshot is the clock, never the wall clock.
    await renderOperations(report([signal()]));
    expect(
      await screen.findByText(/measured against the dataset snapshot/i),
    ).toBeInTheDocument();
  });

  it("shows the ranking arithmetic so the order can be checked", async () => {
    await renderOperations(report([signal()]));

    expect(
      await screen.findByText(/How this was prioritised/i),
    ).toBeInTheDocument();
    expect(screen.getByText("detector severity is critical")).toBeInTheDocument();
    expect(screen.getByText("+40")).toBeInTheDocument();
    // Reductions are shown as reductions, not hidden.
    expect(screen.getByText("-3")).toBeInTheDocument();
    expect(screen.getByText("total")).toBeInTheDocument();
  });

  it("names the ranking factors in words rather than wire identifiers", async () => {
    await renderOperations(report([signal()]));

    expect(
      await screen.findByText(/How severe the detector rated it/i),
    ).toBeInTheDocument();
    // The one screen whose whole purpose is to be checkable by a support lead
    // must not read like a database dump.
    expect(screen.queryByText("signal_type")).not.toBeInTheDocument();
  });

  it("shows why a signal was detected, not just that it was", async () => {
    await renderOperations(report([signal()]));

    expect(
      await screen.findByText(/past every computable target/i),
    ).toBeInTheDocument();
    expect(screen.getByText("TKT-001")).toBeInTheDocument();
  });

  it("marks an unsettled signal and states the doubt", async () => {
    await renderOperations(
      report([
        signal({
          trust_status: "conditional",
          trust_reasons: ["Severity has not been classified."],
        }),
      ]),
    );

    // On the row, next to the claim — not as a footnote below it.
    const row = await screen.findByRole("button", { name: /TKT-001/i });
    expect(within(row).getByText("Conditional")).toBeInTheDocument();

    expect(screen.getByText("Not settled")).toBeInTheDocument();
    expect(
      screen.getByText("Severity has not been classified."),
    ).toBeInTheDocument();
  });

  it("does not mark a settled signal as doubtful", async () => {
    await renderOperations(report([signal()]));
    await screen.findByRole("button", { name: /TKT-001/i });
    expect(screen.queryByText("Not settled")).not.toBeInTheDocument();
  });

  it("presents the next step as a suggestion, never as something done", async () => {
    await renderOperations(report([signal()]));
    expect(await screen.findByText(/Suggested next step/i)).toBeInTheDocument();
    expect(
      screen.getByText(/at most, prepares an action for you to confirm/i),
    ).toBeInTheDocument();
  });

  it("says an empty result is an absence of detections, not an all-clear", async () => {
    await renderOperations(report([]));
    expect(
      await screen.findByText(/not a guarantee that nothing is wrong/i),
    ).toBeInTheDocument();
  });

  it("surfaces a failure instead of rendering an empty list", async () => {
    // An error rendered as "no signals" would read as an all-clear, which is
    // the most dangerous thing this screen could get wrong.
    await renderOperations({ status: 500 });

    await waitFor(() =>
      expect(screen.getByRole("alert")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/Nothing below is a statement about whether anything is wrong/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/not a guarantee that nothing is wrong/i),
    ).not.toBeInTheDocument();
  });

  it("selects a signal into the URL so it can be linked to", async () => {
    const { user } = await renderOperations(
      report([signal(), signal({ signal_id: "ANOM-2", title: "Second signal" })]),
    );

    await user.click(await screen.findByRole("button", { name: /Second signal/i }));
    expect(testRoute()).toBe("/operations?signal=ANOM-2");
  });

  it("opens the signal named in the URL rather than the first one", async () => {
    setTestRoute("/operations?signal=ANOM-2");
    stubApi({
      session: {
        signals: report([
          signal(),
          signal({
            signal_id: "ANOM-2",
            title: "Second signal",
            detail: "The detail that belongs to the second signal.",
          }),
        ]),
      },
    });
    renderApp(<OperationsPage />);

    expect(
      await screen.findByText(/detail that belongs to the second signal/i),
    ).toBeInTheDocument();
  });

  it("hands investigation to the assistant rather than acting itself", async () => {
    const { user, stub } = await renderOperations(report([signal()]));

    await user.click(
      await screen.findByRole("button", { name: /investigate with the assistant/i }),
    );

    await waitFor(() => {
      const chat = stub.calls.filter((call) => call.url.includes("/api/chat"));
      expect(chat).toHaveLength(1);
      // The question names the signal, so the agent fetches it through a tool
      // under its own scope rather than being told what this screen believes.
      expect((chat[0]!.body as { message: string }).message).toContain(
        "SLA-TKT-001",
      );
    });
    // And it takes the reader to the answer.
    expect(testRoute()).toBe("/");
  });

  it("refuses the area to a role without the operations permission", async () => {
    setTestRoute("/operations");
    stubApi({ session: { permissions: ["run_agent"] } });
    renderApp(<OperationsPage />);

    expect(
      await screen.findByText(/not available to your role/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/most urgent first/i)).not.toBeInTheDocument();
  });
});

describe("investigating a signal", () => {
  it("carries the signal onto the answer it produced", async () => {
    const user = userEvent.setup();
    setTestRoute("/operations");
    stubApi({ session: { signals: report([signal()]) } });
    renderApp(<Areas />);

    await user.click(
      await screen.findByRole("button", { name: /investigate with the assistant/i }),
    );

    // Support, reached without a reload and without losing the signal: the
    // whole point of holding the conversation above the router.
    const banner = await screen.findByText("TKT-001 has no first response");
    expect(banner).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /back to the signal/i }),
    ).toHaveAttribute("href", "/operations?signal=SLA-TKT-001");
  });

  it("returns to the signal it came from", async () => {
    const user = userEvent.setup();
    setTestRoute("/operations");
    stubApi({ session: { signals: report([signal()]) } });
    renderApp(<Areas />);

    await user.click(
      await screen.findByRole("button", { name: /investigate with the assistant/i }),
    );
    await user.click(
      await screen.findByRole("link", { name: /back to the signal/i }),
    );

    expect(testRoute()).toBe("/operations?signal=SLA-TKT-001");
    expect(
      await screen.findByText(/How this was prioritised/i),
    ).toBeInTheDocument();
  });
});
