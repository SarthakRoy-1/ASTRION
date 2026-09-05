"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { SignalDetail } from "@/components/operations/SignalDetail";
import { SignalList } from "@/components/operations/SignalList";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { SkeletonRows } from "@/components/ui/Loading";
import { useChat, useSession } from "@/app/providers";
import { ApiError } from "@/lib/client";
import { fetchSignals } from "@/lib/operations-client";
import {
  formatReferenceTime,
  investigationQuestion,
} from "@/lib/operations-presentation";
import type { OperationalSignal, SignalReport } from "@/lib/operations-types";

import styles from "./operations.module.css";

/**
 * Operations: "what needs my attention right now, and why?"
 *
 * An inbox, not a dashboard. The list ranks; the detail explains. Both render
 * exactly what the server's deterministic detectors produced — this page never
 * re-ranks, never re-scores, and never turns an absence of detections into an
 * all-clear.
 *
 * The selected signal lives in the URL, so a row can be linked to, survives a
 * reload, and can be returned to from an investigation that started here. The
 * conversation state that investigation lands in is held above the router, so
 * moving to Support and back loses neither the transcript nor the signal.
 */
export default function OperationsPage() {
  return (
    // `useSearchParams` suspends during prerender; the fallback is the same
    // loading state the page shows while the signals themselves are in flight,
    // so the transition is invisible rather than a flash of nothing.
    <Suspense fallback={<OperationsFrame>{null}</OperationsFrame>}>
      <Operations />
    </Suspense>
  );
}

function Operations() {
  const session = useSession();
  const chat = useChat();
  const router = useRouter();
  const params = useSearchParams();

  const [report, setReport] = useState<SignalReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const detailRef = useRef<HTMLDivElement>(null);

  const workspaceId = session.activeWorkspace?.workspace_id ?? null;
  const selectedId = params.get("signal");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await fetchSignals());
    } catch (cause) {
      setReport(null);
      setError(
        cause instanceof ApiError
          ? cause
          : new ApiError(String(cause), { code: "client_error", status: 0 }),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  // Re-detected when the active workspace changes: signals are derived from
  // that tenant's records, and showing one workspace's list under another's
  // name would be the worst kind of stale.
  useEffect(() => {
    void load();
  }, [load, workspaceId]);

  const signals = report?.signals ?? [];
  const selected =
    signals.find((signal) => signal.signal_id === selectedId) ?? signals[0] ?? null;

  const select = useCallback(
    (signalId: string) => {
      router.replace(`/operations?signal=${encodeURIComponent(signalId)}`, {
        scroll: false,
      });
      // Below the two-column breakpoint the detail sits under the list, so a
      // selection made near the top of a long list would otherwise appear
      // off-screen. Only ever a scroll — focus stays on the row, so arrowing
      // through the list keeps working.
      if (
        typeof window !== "undefined" &&
        window.matchMedia("(max-width: 999px)").matches
      ) {
        detailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    },
    [router],
  );

  function investigate(signal: OperationalSignal) {
    // Hands the question to the existing agent rather than building a second
    // investigation path. The origin travels with it so the answer stays
    // traceable back to this signal.
    void chat.send(investigationQuestion(signal.signal_id), {
      signalId: signal.signal_id,
      title: signal.title,
    });
    router.push("/");
  }

  if (!session.can("operations.read")) {
    return (
      <OperationsFrame>
        <EmptyState title="Operations is not available to your role">
          Seeing operational signals needs the <code>operations.read</code>{" "}
          permission, which your role in this workspace does not grant. A
          workspace admin can change your role.
        </EmptyState>
      </OperationsFrame>
    );
  }

  return (
    <OperationsFrame referenceTime={report?.reference_time ?? null}>
      <div className={styles.grid}>
        <div className={styles.listPane}>
          {loading ? (
            <SkeletonRows rows={4} label="Detecting operational signals." />
          ) : error ? (
            // An error rendered as "no signals" would read as an all-clear,
            // which is the most dangerous thing this screen could get wrong.
            <Callout tone="fail" role="alert" title="Detection failed">
              {error.message} Nothing below is a statement about whether
              anything is wrong.
              <div style={{ marginTop: "var(--space-3)" }}>
                <Button size="sm" onClick={() => void load()}>
                  Try again
                </Button>
              </div>
            </Callout>
          ) : signals.length === 0 ? (
            <EmptyState title="Nothing detected in this workspace">
              That is an absence of detected signals, not a guarantee that
              nothing is wrong — detection runs over the tickets and orders in
              scope, and only reports patterns it can evidence.
            </EmptyState>
          ) : (
            <>
              <p className={styles.count}>
                {signals.length} signal{signals.length === 1 ? "" : "s"}, most
                urgent first
              </p>
              <SignalList
                signals={signals}
                selectedId={selected?.signal_id ?? null}
                onSelect={select}
              />
            </>
          )}
        </div>

        <div className={styles.detailPane} ref={detailRef}>
          {selected ? (
            <div className={styles.detailCard}>
              <SignalDetail signal={selected} onInvestigate={investigate} />
            </div>
          ) : loading || error || signals.length === 0 ? null : (
            <p className={styles.placeholder}>
              Choose a signal to see why it was detected.
            </p>
          )}
        </div>
      </div>
    </OperationsFrame>
  );
}

/** The page's fixed heading, shared by every state it can be in. */
function OperationsFrame({
  referenceTime,
  children,
}: {
  referenceTime?: string | null;
  children: React.ReactNode;
}) {
  return (
    <main id="main" className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>What needs attention</h1>
        <p className={styles.subtitle}>
          Patterns detected across this workspace&apos;s tickets and orders,
          ranked by a deterministic score you can read in full. Nothing here
          acts on its own.
        </p>
        {referenceTime ? (
          <p className={styles.snapshot}>
            Measured against the dataset snapshot{" "}
            <time dateTime={referenceTime}>
              {formatReferenceTime(referenceTime)}
            </time>
            , not the current time.
          </p>
        ) : null}
      </header>
      {children}
    </main>
  );
}
