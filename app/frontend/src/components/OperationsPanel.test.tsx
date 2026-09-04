import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OperationsPanel } from "./OperationsPanel";

import type { OperationalSignal, SignalReport } from "@/lib/operations-types";

/**
 * The operations panel, driven by mocked API responses.
 *
 * The shapes below mirror what `GET /api/operations/signals` actually returns
 * — the backend's own contract tests pin that, so these can concentrate on
 * what the *panel* is responsible for: showing the ranking honestly, showing
 * doubt where the detector expressed it, and never acting on anything.
 */

const fetchSignals = vi.fn();

vi.mock("@/lib/operations-client", () => ({
  fetchSignals: (...args: unknown[]) => fetchSignals(...args),
  fetchSignal: vi.fn(),
}));

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

const noop = () => {};

beforeEach(() => {
  fetchSignals.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("OperationsPanel", () => {
  it("lists detected signals with their severity and priority", async () => {
    fetchSignals.mockResolvedValue(report([signal()]));
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    expect(
      await screen.findByRole("button", { name: /TKT-001 has no first response/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("critical")).toBeInTheDocument();
    expect(screen.getByText(/priority 52/)).toBeInTheDocument();
  });

  it("states what the signals were measured against", async () => {
    // "150 minutes overdue" is meaningless without saying overdue relative to
    // what. The dataset snapshot is the clock, never the wall clock.
    fetchSignals.mockResolvedValue(report([signal()]));
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    expect(await screen.findByText(/dataset snapshot/i)).toBeInTheDocument();
  });

  it("shows the ranking arithmetic so the order can be checked", async () => {
    fetchSignals.mockResolvedValue(report([signal()]));
    const user = userEvent.setup();
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    await user.click(
      await screen.findByRole("button", { name: /TKT-001 has no first response/i }),
    );

    expect(screen.getByText(/How this was prioritised/i)).toBeInTheDocument();
    expect(screen.getByText("detector severity is critical")).toBeInTheDocument();
    expect(screen.getByText("+40")).toBeInTheDocument();
    // Reductions are shown as reductions, not hidden.
    expect(screen.getByText("-3")).toBeInTheDocument();
    expect(screen.getByText("total")).toBeInTheDocument();
  });

  it("shows why a signal was detected, not just that it was", async () => {
    fetchSignals.mockResolvedValue(report([signal()]));
    const user = userEvent.setup();
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    await user.click(await screen.findByRole("button", { name: /TKT-001/i }));
    expect(
      screen.getByText(/past every computable target/i),
    ).toBeInTheDocument();
    expect(screen.getByText("TKT-001")).toBeInTheDocument();
  });

  it("marks an unsettled signal and states the doubt", async () => {
    fetchSignals.mockResolvedValue(
      report([
        signal({
          trust_status: "conditional",
          trust_reasons: ["Severity has not been classified."],
        }),
      ]),
    );
    const user = userEvent.setup();
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    expect(await screen.findByText(/conditional/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /TKT-001/i }));
    expect(screen.getByText("Not settled")).toBeInTheDocument();
    expect(
      screen.getByText("Severity has not been classified."),
    ).toBeInTheDocument();
  });

  it("does not mark a settled signal as doubtful", async () => {
    fetchSignals.mockResolvedValue(report([signal()]));
    const user = userEvent.setup();
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    await user.click(await screen.findByRole("button", { name: /TKT-001/i }));
    expect(screen.queryByText("Not settled")).not.toBeInTheDocument();
  });

  it("hands investigation to the existing agent rather than acting itself", async () => {
    const onInvestigate = vi.fn();
    fetchSignals.mockResolvedValue(report([signal()]));
    const user = userEvent.setup();
    render(<OperationsPanel onInvestigate={onInvestigate} onClose={noop} />);

    await user.click(await screen.findByRole("button", { name: /TKT-001/i }));
    await user.click(screen.getByRole("button", { name: /ask the assistant/i }));

    expect(onInvestigate).toHaveBeenCalledTimes(1);
    // The question names the signal, so the agent fetches it through a tool
    // rather than being told what the panel already believes.
    expect(onInvestigate).toHaveBeenCalledWith(
      expect.stringContaining("SLA-TKT-001"),
    );
  });

  it("presents the next step as a suggestion, never as something done", async () => {
    fetchSignals.mockResolvedValue(report([signal()]));
    const user = userEvent.setup();
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    await user.click(await screen.findByRole("button", { name: /TKT-001/i }));
    expect(screen.getByText(/Suggested next step:/i)).toBeInTheDocument();
  });

  it("says an empty result is an absence of detections, not an all-clear", async () => {
    fetchSignals.mockResolvedValue(report([]));
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    expect(
      await screen.findByText(/not a guarantee that nothing is wrong/i),
    ).toBeInTheDocument();
  });

  it("surfaces a failure instead of rendering an empty list", async () => {
    // An error rendered as "no signals" would read as an all-clear, which is
    // the most dangerous thing this panel could get wrong.
    fetchSignals.mockRejectedValue(new Error("backend unreachable"));
    render(<OperationsPanel onInvestigate={noop} onClose={noop} />);

    await waitFor(() =>
      expect(screen.getByText(/backend unreachable/i)).toBeInTheDocument(),
    );
    expect(
      screen.queryByText(/not a guarantee that nothing is wrong/i),
    ).not.toBeInTheDocument();
  });

  it("closes when asked", async () => {
    const onClose = vi.fn();
    fetchSignals.mockResolvedValue(report([signal()]));
    const user = userEvent.setup();
    render(<OperationsPanel onInvestigate={noop} onClose={onClose} />);

    await user.click(await screen.findByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
