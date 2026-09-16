"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { SkeletonRows } from "@/components/ui/Loading";
import { fetchDocumentChunks } from "@/lib/documents-client";
import { ApiError } from "@/lib/client";
import type { DocumentMetadataResponse, DocumentChunkResponse } from "@/lib/documents-types";

import styles from "./DocumentDetail.module.css";

interface DocumentDetailProps {
  document: DocumentMetadataResponse;
  onClose: () => void;
  onDelete?: () => Promise<void>;
}

export function DocumentDetail({ document, onClose, onDelete }: DocumentDetailProps) {
  const [chunks, setChunks] = useState<DocumentChunkResponse[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let active = true;
    
    async function load() {
      setLoading(true);
      try {
        const res = await fetchDocumentChunks(document.document_id);
        if (active) {
          setChunks(res.chunks);
          setError(null);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof ApiError ? err.message : String(err));
          setChunks(null);
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    
    void load();
    return () => { active = false; };
  }, [document.document_id]);

  const handleDelete = async () => {
    if (!onDelete) return;
    if (!window.confirm("Are you sure you want to delete this document?")) return;
    
    setDeleting(true);
    try {
      await onDelete();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setDeleting(false);
    }
  };

  return (
    <div className={styles.detailView}>
      <header className={styles.header}>
        <div className={styles.titleArea}>
          <h2 className={styles.title}>{document.title || document.source_file}</h2>
          <div className={styles.metaValue}>{document.document_id}</div>
        </div>
        <div className={styles.actions}>
          {onDelete && (
            <Button variant="ghost" onClick={handleDelete} disabled={deleting}>
              {deleting ? "Deleting..." : "Delete"}
            </Button>
          )}
          <Button variant="secondary" onClick={onClose} disabled={deleting}>
            Close
          </Button>
        </div>
      </header>

      {error && (
        <Callout tone="fail" title="Error">
          {error}
        </Callout>
      )}

      <div className={styles.metadataGrid}>
        <div className={styles.metaItem}>
          <span className={styles.metaLabel}>Type</span>
          <span className={styles.metaValue}>{document.document_type.replace(/_/g, " ")}</span>
        </div>
        <div className={styles.metaItem}>
          <span className={styles.metaLabel}>Status</span>
          <span className={styles.metaValue}>{document.status}</span>
        </div>
        {document.customer_name && (
          <div className={styles.metaItem}>
            <span className={styles.metaLabel}>Customer</span>
            <span className={styles.metaValue}>{document.customer_name}</span>
          </div>
        )}
        <div className={styles.metaItem}>
          <span className={styles.metaLabel}>Pages</span>
          <span className={styles.metaValue}>{document.page_count}</span>
        </div>
        <div className={styles.metaItem}>
          <span className={styles.metaLabel}>File</span>
          <span className={styles.metaValue}>{document.source_file}</span>
        </div>
        <div className={styles.metaItem}>
          <span className={styles.metaLabel}>Authoritative</span>
          <span className={styles.metaValue}>{document.is_authoritative ? "Yes" : "No"}</span>
        </div>
      </div>

      <div className={styles.chunksHeader}>
        <h3 className={styles.chunksTitle}>Document Text Chunks</h3>
        <span className={styles.metaValue}>{chunks?.length || 0} chunks extracted</span>
      </div>

      {loading ? (
        <SkeletonRows rows={3} label="Loading chunks..." />
      ) : chunks && chunks.length > 0 ? (
        <div className={styles.chunksList}>
          {chunks.map((chunk) => (
            <div key={chunk.chunk_id} className={styles.chunk}>
              <div className={styles.chunkHeader}>
                <span className={styles.chunkTopic}>{chunk.topic.replace(/_/g, " ")}</span>
                <span>
                  Page {chunk.page_number}
                  {chunk.section_title ? ` · ${chunk.section_title}` : ""}
                </span>
              </div>
              <div className={styles.chunkText}>{chunk.text}</div>
            </div>
          ))}
        </div>
      ) : (
        <Callout tone="info" title="No text chunks">
          This document has no extracted text content.
        </Callout>
      )}
    </div>
  );
}
