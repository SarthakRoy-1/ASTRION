import { useId } from "react";

import { StatusPill } from "./StatusPill";
import {
  summariseInvestigation,
  toolActivityLabel,
  toolStatusLabel,
  toolStatusTone,
} from "@/lib/presentation";
import { agreementGoverned } from "@/lib/trust-presentation";
import type { ChatResponse, ToolUse } from "@/lib/types";

import styles from "./AgentActivity.module.css";

/**
 * What the agent did, in the order it did it.
 *
 * The backend answers a request in one response rather than streaming, so this
 * is a record of a completed investigation — not a live activity feed. It
 * reports the real steps, the real per-step status and the backend's own
 * one-line summary of each. Nothing here is simulated, timed, or animated to
 * look like progress that already finished.
 *
 * Two things it is careful about, for opposite reasons:
 *
 * - **It is not a developer log.** No arguments, no ids, no raw tool names.
 *   Each row is a sentence about what was consulted.
 * - **It is not chain-of-thought.** The agent's reasoning is never exposed,
 *   here or anywhere. Every row is a structured fact the API returned about a
 *   call that actually happened; nothing narrates *why* it made the call.
 *
 * The closing rows come from the same structured fields the rest of the turn
 * renders — the authority override from `trust`, the conclusion from
 * `outcome` — so the sequence ends where the reader's eye started, and the
 * summary cannot disagree with the answer above it.
 */
export function AgentActivity({
  tools,
  trust,
  outcome,
}: {
  tools: ToolUse[];
  trust: ChatResponse["trust"];
  outcome: ChatResponse["outcome"];
}) {
  const headingId = useId();

  if (tools.length === 0) return null;

  // Grouped capabilities, stated once in the heading. The rows below are the
  // sequence; this is the shape of the investigation at a glance.
  const capabilities = summariseInvestigation(tools).map(
    (step) => step.categoryLabel,
  );

  const overrode = agreementGoverned(trust);

  return (
    <section className={styles.section} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.heading}>
        Investigation
        <span className={styles.count}>
          {tools.length} {tools.length === 1 ? "step" : "steps"}
        </span>
      </h3>

      {/* Beside the heading rather than inside it: the section's accessible
          name comes from the heading, and folding a capability list into it
          made the region answer to the name of any capability it happened to
          use. Each capability is its own node so the summary stays readable to
          anything scanning for a single one of them. */}
      <p className={styles.capabilityRow}>
        {capabilities.map((capability, index) => (
          <span key={capability} className={styles.capabilities}>
            {index > 0 ? (
              <span className={styles.divider} aria-hidden="true">
                {"·"}
              </span>
            ) : null}
            {capability}
          </span>
        ))}
      </p>

      <ol className={styles.list}>
        {tools.map((tool) => {
          const tone = toolStatusTone(tool.status);
          const failed = tone !== "ok";
          return (
            <li key={`${tool.step}-${tool.tool_name}`} className={styles.item}>
              <span className={styles.marker} data-tone={tone} aria-hidden="true" />
              <span className={styles.body}>
                <span className={styles.label}>
                  {toolActivityLabel(tool.tool_name)}
                  {failed ? (
                    <span className={styles.status}>
                      <StatusPill tone={tone} quiet>
                        {toolStatusLabel(tool.status)}
                      </StatusPill>
                    </span>
                  ) : null}
                </span>
                {/* The backend's own summary, or its explanation of a step
                    that did not succeed. Never both invented here. */}
                {tool.message || tool.summary ? (
                  <span
                    className={`${styles.detail} ${failed ? styles.failed : ""}`}
                  >
                    {tool.message ?? tool.summary}
                  </span>
                ) : null}
              </span>
            </li>
          );
        })}

        {overrode ? (
          <li className={`${styles.item} ${styles.outcome}`}>
            <span className={styles.marker} aria-hidden="true" />
            <span className={styles.body}>
              <span className={styles.outcomeLabel}>
                The account&apos;s signed agreement overrode the standard policy
              </span>
            </span>
          </li>
        ) : null}

        <li className={`${styles.item} ${styles.outcome}`}>
          <span className={styles.marker} aria-hidden="true" />
          <span className={styles.body}>
            <span className={styles.outcomeLabel}>{conclusionLabel(outcome)}</span>
          </span>
        </li>
      </ol>
    </section>
  );
}

/**
 * How the investigation ended, in the backend's own vocabulary.
 *
 * `uncertain` is a success case in this system — the SOP forbids promising a
 * credit when fault or timing is unknown — so it is stated as a result the
 * agent reached, never as a failure of the request.
 */
function conclusionLabel(outcome: ChatResponse["outcome"]): string {
  switch (outcome) {
    case "answered":
      return "Answer ready";
    case "uncertain":
      return "Could not be determined from the available data";
    case "needs_confirmation":
      return "Action prepared, awaiting confirmation";
    case "refused":
      return "Request refused";
    case "error":
      return "The investigation did not complete";
    default:
      return String(outcome).replace(/_/g, " ");
  }
}
