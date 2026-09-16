"use client";

import type { DocumentMetadataResponse } from "@/lib/documents-types";
import styles from "./DocumentList.module.css";

interface DocumentListProps {
  documents: DocumentMetadataResponse[];
  onSelect: (documentId: string) => void;
}

export function DocumentList({ documents, onSelect }: DocumentListProps) {
  return (
    <div className={styles.list}>
      {documents.map((doc) => (
        <div
          key={doc.document_id}
          className={styles.documentRow}
          onClick={() => onSelect(doc.document_id)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onSelect(doc.document_id);
            }
          }}
        >
          <div className={styles.info}>
            <div className={styles.title}>{doc.title || doc.source_file}</div>
            <div className={styles.meta}>
              <span>{doc.document_type.replace(/_/g, " ")}</span>
              <span>{doc.page_count} pages</span>
              {doc.customer_name ? <span>Customer: {doc.customer_name}</span> : <span>General</span>}
            </div>
          </div>
          <div>
            <span
              className={`${styles.badge} ${
                doc.status === "ACTIVE" || doc.status === "CURRENT"
                  ? styles.badgeActive
                  : styles.badgeDeprecated
              }`}
            >
              {doc.status}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
