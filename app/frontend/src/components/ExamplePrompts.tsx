"use client";

import { useId } from "react";

import styles from "./ExamplePrompts.module.css";

/**
 * Starter questions, so the product is demonstrable in one click.
 *
 * These are *inputs only*. Each one sends a real request through the real
 * agent; no answer, citation or figure is associated with them anywhere in the
 * frontend. The agent will answer a question not on this list exactly as well,
 * and the copy below says so — a demo that quietly implies only the scripted
 * questions work would be misrepresenting the system.
 *
 * The set is chosen to exercise different paths: a policy calculation with a
 * customer-agreement override, a question missing the input it needs, a
 * product-documentation lookup, and an action that must be confirmed.
 */
const PROMPTS: readonly string[] = [
  "Can Northstar cancel ORD-1001 without a cancellation fee? Explain why.",
  "Is ORD-2002 eligible for a failed pickup service credit?",
  "A pickup is three hours late because of carrier fault. Should I get a service credit?",
  "Why does a SwiftShip order still show BOOKED after the driver collected it?",
  "Investigate TKT-501 and escalate it if the outage warrants it.",
];

export function ExamplePrompts({
  disabled,
  onSelect,
}: {
  disabled: boolean;
  onSelect: (prompt: string) => void;
}) {
  const headingId = useId();

  return (
    <section className={styles.section} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.heading}>
        Try one of these
      </h3>
      <ul className={styles.list}>
        {PROMPTS.map((prompt) => (
          <li key={prompt}>
            <button
              type="button"
              className={styles.prompt}
              disabled={disabled}
              onClick={() => onSelect(prompt)}
            >
              {prompt}
            </button>
          </li>
        ))}
      </ul>
      <p className={styles.note}>
        These are only starting points. Ask about any order, ticket, account or
        policy in the dataset — the agent looks everything up at request time.
      </p>
    </section>
  );
}
