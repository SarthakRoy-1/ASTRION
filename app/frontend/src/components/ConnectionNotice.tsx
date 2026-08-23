import type { ConnectionState } from "@/hooks/useConversation";

import styles from "./ConnectionNotice.module.css";

/**
 * What the page is waiting for while the backend comes back.
 *
 * The deployment sleeps when idle, and the request that wakes it can take the
 * better part of a minute. Two things follow, and this component exists for
 * both. A page that says nothing for that long reads as broken; and a page
 * that says "cannot reach the API" reads as broken *and* wrong, because the
 * backend is on its way up and will answer.
 *
 * So it is deliberately not an error. Neutral colour, `role="status"` rather
 * than `alert`, and no reference code — there is nothing here for anyone to
 * act on or report. Once the bounded wake-up strategy really is spent, that is
 * a fault, and `ErrorNotice` takes over saying so.
 */
export function ConnectionNotice({ state }: { state: ConnectionState }) {
  const message = messageFor(state);
  if (!message) return null;

  return (
    <p className={styles.notice} role="status">
      <span className={styles.pulse} aria-hidden="true" />
      {message}
    </p>
  );
}

function messageFor(state: ConnectionState): string | null {
  switch (state) {
    case "connecting":
      return "Connecting to the ParcelPilot API…";
    case "waking":
      // Naming the cause and the cost: a wait you understand is a wait you can
      // sit through, and the number keeps anyone from reloading at 30 seconds.
      return "Waking the ParcelPilot API… This can take up to a minute after a period of inactivity.";
    default:
      return null;
  }
}
