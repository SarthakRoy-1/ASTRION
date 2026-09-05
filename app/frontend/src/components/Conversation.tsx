"use client";

import { useEffect, useRef } from "react";

import { AgentMessage } from "./AgentMessage";
import { ErrorNotice } from "./ErrorNotice";
import { ExamplePrompts } from "./ExamplePrompts";
import { UserMessage } from "./UserMessage";
import type { Turn } from "@/hooks/useConversation";

import styles from "./Conversation.module.css";

/**
 * The transcript.
 *
 * The scroll nudge is intentionally minimal — new content is scrolled to, and
 * nothing else moves, because a chat that yanks the viewport while you are
 * reading an evidence card is worse than one that does not scroll at all.
 *
 * What it scrolls *to* is the top of the newest turn, not the end of the
 * transcript. A full agent turn is a conclusion, a decision, an action and a
 * dozen evidence cards; anchoring on its end left the reader looking at the
 * investigation summary with the answer several screens above.
 *
 * The live region is deliberately *narrow*. Marking the whole list polite
 * meant a screen reader read out an entire agent turn on arrival — the trust
 * block, the decision card, every citation and the whole activity log, a
 * minute of speech before the listener could interrupt. Instead a short
 * status sentence is announced, and the turn itself is left to be read at the
 * reader's own pace with the headings the components already provide.
 */
export function Conversation({
  turns,
  sending,
  canConfirmActions,
  canApproveHighValue,
  onExample,
  onRespondToAction,
}: {
  turns: Turn[];
  sending: boolean;
  canConfirmActions: boolean;
  canApproveHighValue: boolean;
  onExample: (prompt: string) => void;
  onRespondToAction: (turnId: string, decision: "approve" | "reject") => void;
}) {
  const latestTurnRef = useRef<HTMLLIElement>(null);

  useEffect(() => {
    latestTurnRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [turns.length, sending]);

  if (turns.length === 0) {
    return (
      // The same landmark as a populated transcript, so "where the
      // conversation is" does not move depending on whether one has started.
      <section className={styles.empty} aria-label="Conversation">
        <h2 className={styles.emptyTitle}>Ask about an order, ticket or policy</h2>
        <p className={styles.emptyBody}>
          Answers are drawn from ParcelPilot&apos;s policies, SOPs, product
          documentation and signed customer agreements, with every source shown.
          Cancellation fees and service credits are calculated in code, not
          written by the model, and nothing is changed without your explicit
          confirmation.
        </p>
        <ExamplePrompts disabled={sending} onSelect={onExample} />
      </section>
    );
  }

  return (
    <section className={styles.transcript} aria-label="Conversation">
      <h2 className="visually-hidden">Conversation</h2>

      {/* One short sentence per state, so the arrival of an answer is
          announced without the whole answer being read aloud. */}
      <p className="visually-hidden" role="status">
        {sending ? "Investigating." : lastTurnAnnouncement(turns)}
      </p>

      <ol className={styles.list} aria-busy={sending}>
        {turns.map((turn, index) => (
          <li
            key={turn.id}
            className={styles.turn}
            ref={index === turns.length - 1 ? latestTurnRef : undefined}
          >
            {turn.kind === "user" && <UserMessage text={turn.text} />}
            {turn.kind === "agent" && (
              <AgentMessage
                response={turn.response}
                action={turn.action}
                canConfirmActions={canConfirmActions}
                canApproveHighValue={canApproveHighValue}
                onRespondToAction={(decision) => onRespondToAction(turn.id, decision)}
              />
            )}
            {turn.kind === "error" && <ErrorNotice error={turn.error} />}
          </li>
        ))}

        {sending && (
          <li className={styles.turn}>
            <p className={styles.pending}>
              <span className={styles.spinner} aria-hidden="true" />
              Investigating…
            </p>
          </li>
        )}
      </ol>
    </section>
  );
}

/**
 * What just happened, in one sentence.
 *
 * Names the outcome rather than reading the answer, so a listener learns that
 * a reply arrived and what kind it is, then chooses whether to read it.
 */
function lastTurnAnnouncement(turns: Turn[]): string {
  const last = turns[turns.length - 1];
  if (!last) return "";
  if (last.kind === "error") return "The request failed.";
  if (last.kind === "user") return "";
  if (last.response.proposed_action) {
    return "The agent replied and prepared an action for your confirmation.";
  }
  if (last.response.outcome === "uncertain") {
    return "The agent replied: it could not determine this from the available data.";
  }
  return "The agent replied.";
}
