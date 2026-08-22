import { authorityLabel, sourceName } from "@/lib/presentation";
import { StatusPill } from "./StatusPill";
import type { SourceRef } from "@/lib/types";

import styles from "./EvidenceCard.module.css";

/**
 * One citation, collapsed to its provenance and expandable to its text.
 *
 * Every field shown comes from the backend's `SourceRef`. The card never
 * infers authority, never re-ranks, and never paraphrases the excerpt — a
 * citation the reader cannot check against the source document would defeat
 * the point of showing one.
 *
 * `<details>` rather than a scripted disclosure: it is keyboard-operable,
 * announced correctly, and findable by the browser's own in-page search even
 * while collapsed.
 */
export function EvidenceCard({
  source,
  governing,
}: {
  source: SourceRef;
  governing: boolean;
}) {
  const deprecated = source.is_deprecated;

  return (
    <details className={styles.card}>
      <summary className={styles.summary}>
        <span className={styles.heading}>
          <span className={styles.name}>{sourceName(source)}</span>
          <span className={styles.locator}>
            Page {source.page}
            {source.section ? ` · §${source.section}` : ""}
          </span>
        </span>

        <span className={styles.badges}>
          {deprecated ? (
            <StatusPill tone="fail">Deprecated</StatusPill>
          ) : governing ? (
            <StatusPill tone="ok">Governing</StatusPill>
          ) : (
            <StatusPill tone="caution">Context only</StatusPill>
          )}
        </span>
      </summary>

      <div className={styles.body}>
        <dl className={styles.meta}>
          <div className={styles.metaRow}>
            <dt>File</dt>
            <dd className={styles.mono}>{source.source_file}</dd>
          </div>
          <div className={styles.metaRow}>
            <dt>Authority</dt>
            <dd>{authorityLabel(source.authority_tier)}</dd>
          </div>
          {source.account_id && (
            <div className={styles.metaRow}>
              <dt>Account</dt>
              <dd className={styles.mono}>{source.account_id}</dd>
            </div>
          )}
        </dl>

        <blockquote className={styles.excerpt}>{source.excerpt}</blockquote>

        {deprecated && (
          <p className={styles.warning}>
            This document is superseded. It is shown to explain what changed and
            cannot be used as current policy.
          </p>
        )}
      </div>
    </details>
  );
}
