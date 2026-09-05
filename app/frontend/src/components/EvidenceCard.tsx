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
 * The authority tier is on the *collapsed* row, not hidden inside. Whether a
 * passage came from a signed customer agreement or from an operational
 * document is the difference between two opposite answers, and a reader
 * scanning a list of six citations must not have to open each one to find out.
 *
 * A superseded document is styled as historical rather than merely labelled:
 * muted, with its name struck through. It stays in the list because explaining
 * that a rule changed needs the rule that changed, and it must be impossible
 * to skim as current policy.
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
    <details className={styles.card} data-deprecated={deprecated || undefined}>
      <summary className={styles.summary}>
        <span className={styles.heading}>
          <span className={styles.name}>{sourceName(source)}</span>
          <span className={styles.locator}>
            <span className={styles.authority}>
              {authorityLabel(source.authority_tier)}
            </span>
            <span aria-hidden="true"> · </span>
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
            <StatusPill tone="neutral">Context only</StatusPill>
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
          {source.topic && (
            <div className={styles.metaRow}>
              <dt>Topic</dt>
              <dd>{source.topic}</dd>
            </div>
          )}
          {source.account_id && (
            <div className={styles.metaRow}>
              <dt>Account</dt>
              <dd className={styles.mono}>{source.account_id}</dd>
            </div>
          )}
        </dl>

        <blockquote className={styles.excerpt}>{source.excerpt}</blockquote>

        {source.citation && (
          <p className={styles.citation}>{source.citation}</p>
        )}

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
