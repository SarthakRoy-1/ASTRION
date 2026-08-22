import { splitEvidence } from "@/lib/presentation";
import { EvidenceCard } from "./EvidenceCard";
import type { SourceRef } from "@/lib/types";

import styles from "./EvidenceSection.module.css";

/**
 * The sources behind an answer, split the way the backend split them.
 *
 * Governing evidence decided the answer; contextual evidence was outranked or
 * deprecated. Both are shown, because explaining that a customer agreement
 * overrode general policy — or that a rule changed — requires the material
 * that lost. What the UI must never do is present them as equivalent.
 */
export function EvidenceSection({ sources }: { sources: SourceRef[] }) {
  if (sources.length === 0) return null;

  const { governing, contextual } = splitEvidence(sources);

  return (
    <section className={styles.section} aria-labelledby="sources-heading">
      <h3 id="sources-heading" className={styles.heading}>
        Sources
        <span className={styles.count}>
          {sources.length} {sources.length === 1 ? "document" : "documents"}
        </span>
      </h3>

      {governing.length > 0 && (
        <div className={styles.group}>
          <p className={styles.groupLabel}>Governing</p>
          <div className={styles.list}>
            {governing.map((source) => (
              <EvidenceCard key={source.chunk_id} source={source} governing />
            ))}
          </div>
        </div>
      )}

      {contextual.length > 0 && (
        <div className={styles.group}>
          <p className={styles.groupLabel}>
            Context only
            <span className={styles.groupHint}>
              Outranked or superseded — not used to decide the answer
            </span>
          </p>
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
