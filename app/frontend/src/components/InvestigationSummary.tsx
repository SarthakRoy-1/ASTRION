import { useId } from "react";

import {
  summariseInvestigation,
  toolLabel,
  toolStatusLabel,
  toolStatusTone,
} from "@/lib/presentation";
import { StatusPill } from "./StatusPill";
import type { ToolUse } from "@/lib/types";

import styles from "./InvestigationSummary.module.css";

/**
 * What the agent actually consulted, after the fact.
 *
 * The backend answers a request in one response rather than streaming, so this
 * is a summary of a completed investigation — not a live activity feed. It
 * reports the real step count and the real per-tool status; nothing here is
 * simulated, timed, or animated to look like progress that already finished.
 *
 * Capabilities are grouped, but each grouped row lists the tools it covers and
 * the raw step count is stated alongside, so grouping compresses the display
 * without hiding what ran. Tool *arguments* are never shown: they are part of
 * the agent's planning, and the contract promises what was consulted and how
 * it finished, not how the query was phrased.
 *
 * If the API later streams tool events, this component's shape is what a live
 * indicator would fill in — same rows, updated as they complete.
 */
export function InvestigationSummary({ tools }: { tools: ToolUse[] }) {
  const headingId = useId();

  if (tools.length === 0) return null;

  const steps = summariseInvestigation(tools);
  const failures = tools.filter((tool) => toolStatusTone(tool.status) !== "ok");

  return (
    <section className={styles.section} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.heading}>
        Investigation
        <span className={styles.count}>
          {tools.length} {tools.length === 1 ? "step" : "steps"}
        </span>
      </h3>

      <ul className={styles.list}>
        {steps.map((step) => (
          <li key={step.category} className={styles.item}>
            <span className={styles.marker} data-tone={step.tone} aria-hidden="true" />
            <span className={styles.itemBody}>
              <span className={styles.category}>{step.categoryLabel}</span>
              <span className={styles.tools}>{step.tools.join(" · ")}</span>
            </span>
          </li>
        ))}
      </ul>

      {failures.length > 0 && (
        <ul className={styles.failures}>
          {failures.map((tool) => (
            <li key={`${tool.step}-${tool.tool_name}`} className={styles.failure}>
              <StatusPill tone={toolStatusTone(tool.status)}>
                {toolStatusLabel(tool.status)}
              </StatusPill>
              <span className={styles.failureText}>
                {toolLabel(tool.tool_name)}
                {tool.message ? `: ${tool.message}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
