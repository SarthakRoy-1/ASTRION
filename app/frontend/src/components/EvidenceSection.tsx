import { useId } from "react";

import { splitEvidence } from "@/lib/presentation";
import { EvidenceCard } from "./EvidenceCard";
import type { ChatResponse, SourceRef } from "@/lib/types";

import styles from "./EvidenceSection.module.css";

/**
 * The sources behind an answer, split the way the backend split them.
 *
 * Governing evidence decided the answer; contextual evidence was outranked or
 * deprecated. Both are shown, because explaining that a customer agreement
 * overrode general policy — or that a rule changed — requires the material
 * that lost. What the UI must never do is present them as equivalent.
 *
 * Three rules this section holds to:
 *
 * - **Retrieved is not authoritative.** The split keys on the backend's own
 *   `is_authoritative`, never on the fact that a document came back from a
 *   search. A reader who assumes every citation governed is the failure this
 *   grouping exists to prevent.
 * - **What lost says why it lost.** `trust.overrides` is the backend's own
 *   statement of each precedence decision, and it heads the outranked group
 *   rather than sitting somewhere the reader has to correlate it from.
 * - **The count is of documents, not of certainty.** "6 documents" says how
 *   much was consulted and nothing about how settled the answer is; that is
 *   the trust chip's job, and the two are kept apart deliberately.
 */
export function EvidenceSection({
  sources,
  trust,
}: {
  sources: SourceRef[];
  trust?: ChatResponse["trust"];
}) {
  // Every agent turn renders one of these. A literal id would repeat down the
  // transcript, and `aria-labelledby` resolves to the first match in the
  // document — so from turn two onward every region was named after turn one.
  const headingId = useId();

  if (sources.length === 0) return null;

  const { governing, contextual } = splitEvidence(sources);
  const overrides = trust?.overrides ?? [];

  return (
    <section className={styles.section} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.heading}>
        Sources
        <span className={styles.count}>
          {sources.length} {sources.length === 1 ? "document" : "documents"}
        </span>
      </h3>

      {governing.length > 0 && (
        <div className={styles.group}>
          {/* Deliberately just the word. The contrast the reader needs is
              carried by the "Context only" label below, which says what being
              outside this group means; repeating it here as "these decided the
              answer" adds a second sentence to say the same thing. */}
          <p className={styles.groupLabel}>Governing</p>
          <div className={styles.list}>
            {governing.map((source) => (
              <EvidenceCard key={source.chunk_id} source={source} governing />
            ))}
          </div>
        </div>
      )}

      {contextual.length > 0 && (
        <div className={`${styles.group} ${styles.contextual}`}>
          <p className={styles.groupLabel}>
            Context only
            <span className={styles.groupHint}>
              Outranked or superseded — not used to decide the answer
            </span>
          </p>

          {overrides.length > 0 && (
            <ul className={styles.overrides}>
              {overrides.map((override) => (
                <li key={override} className={styles.override}>
                  {override}
                </li>
              ))}
            </ul>
          )}

          <div className={styles.list}>
            {contextual.map((source) => (
              <EvidenceCard
                key={source.chunk_id}
                source={source}
                governing={false}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
