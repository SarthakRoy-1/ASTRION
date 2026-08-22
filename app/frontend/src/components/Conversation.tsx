"use client";

import { useEffect, useRef } from "react";

import { AgentMessage } from "./AgentMessage";
import { ErrorNotice } from "./ErrorNotice";
import { ExamplePrompts } from "./ExamplePrompts";
import { UserMessage } from "./UserMessage";
import type { Role } from "@/lib/types";
import type { Turn } from "@/hooks/useConversation";

import styles from "./Conversation.module.css";

/**
 * The transcript.
 *
 * Marked as a polite live region so a screen-reader user hears the agent's
 * reply arrive without it interrupting whatever they are reading. The scroll
 * nudge is intentionally minimal — new content is scrolled to, and nothing
 * else moves, because a chat that yanks the viewport while you are reading an
 * evidence card is worse than one that does not scroll at all.
 */
export function Conversation({
  turns,
  sending,
  role,
  onExample,
  onRespondToAction,
}: {
  turns: Turn[];
  sending: boolean;
  role: Role;
  onExample: (prompt: string) => void;
  onRespondToAction: (turnId: string, decision: "approve" | "reject") => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, sending]);

  if (turns.length === 0) {
    return (
      <div className={styles.empty}>
        <h2 className={styles.emptyTitle}>Ask about an order, ticket or policy</h2>
        <p className={styles.emptyBody}>
          Answers are drawn from ParcelPilot&apos;s policies, SOPs, product
          documentation and signed customer agreements, with every source shown.
          Cancellation fees and service credits are calculated in code, not
          written by the model, and nothing is changed without your explicit
          confirmation.
        </p>
        <ExamplePrompts disabled={sending} onSelect={onExample} />
      </div>
    );
  }

  return (
    <div className={styles.transcript}>
      <h2 className="visually-hidden">Conversation</h2>
      <ol className={styles.list} aria-live="polite" aria-busy={sending}>
        {turns.map((turn) => (
          <li key={turn.id} className={styles.turn}>
            {turn.kind === "user" && <UserMessage text={turn.text} />}
            {turn.kind === "agent" && (
              <AgentMessage
                response={turn.response}
                action={turn.action}
                role={role}
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
      <div ref={endRef} />
    </div>
  );
}
