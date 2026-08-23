"use client";

import type { HealthResponse } from "@/lib/types";

import styles from "./SystemStatus.module.css";

/**
 * What this deployment is actually running, stated once and quietly.
 *
 * Two things it exists to answer. First, every answer is stamped "As of
 * 16 Aug 2026" — a date that reads as a bug until you know the system judges
 * business decisions against a fixed dataset snapshot rather than the wall
 * clock. Second, the whole stack runs with no model API key by default, which
 * is worth stating plainly rather than leaving a reader to assume otherwise.
 *
 * It renders nothing until health is known and nothing if the probe failed:
 * a status line is not worth a spinner, and never worth an error.
 */
export function SystemStatus({ health }: { health: HealthResponse | null }) {
  if (!health) return null;

  const parts: string[] = [];

  parts.push(
    health.provider_mode === "deterministic"
      ? "Deterministic engine — no model API key"
      : health.model
        ? `Model-assisted (${health.model})`
        : "Model-assisted",
  );

  if (health.documents_indexed > 0) {
    parts.push(
      `${health.documents_indexed} policy ${
        health.documents_indexed === 1 ? "document" : "documents"
      } indexed`,
    );
  }

  if (health.dataset_snapshot) {
    parts.push(`data as of ${health.dataset_snapshot}`);
  }

  // Only worth saying when it is *not* the case: the confirmation gate is
  // described everywhere else on the assumption that actions can run.
  if (!health.state_changing_actions_enabled) {
    parts.push("actions disabled");
  }

  return (
    <p className={styles.status}>
      {parts.map((part, index) => (
        <span key={part} className={styles.part}>
          {index > 0 && (
            <span className={styles.divider} aria-hidden="true">
              ·
            </span>
          )}
          {part}
        </span>
      ))}
    </p>
  );
}
