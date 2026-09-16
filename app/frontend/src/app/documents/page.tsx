"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { SkeletonRows } from "@/components/ui/Loading";
import { useSession } from "@/app/providers";
import { ApiError } from "@/lib/client";
import { fetchDocuments, deleteDocument, uploadDocument } from "@/lib/documents-client";
import type { DocumentListResponse, DocumentMetadataResponse } from "@/lib/documents-types";

import styles from "./documents.module.css";
import { DocumentList } from "@/components/documents/DocumentList";
import { DocumentDetail } from "@/components/documents/DocumentDetail";
import { DocumentUpload } from "@/components/documents/DocumentUpload";

export default function DocumentsPage() {
  return (
    <Suspense fallback={null}>
      <Documents />
    </Suspense>
  );
}

function Documents() {
  const session = useSession();
  const router = useRouter();
  const params = useSearchParams();

  const [data, setData] = useState<DocumentListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [showUpload, setShowUpload] = useState(false);

  const selectedId = params.get("doc");
  // The backend's own permission name (`Permission.MANAGE_DOCUMENTS`). Hiding
  // the controls is a courtesy only: the server re-checks every upload and
  // delete, and refuses the supplied source pack whatever this page shows.
  const canManage = session.can("manage_documents");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await fetchDocuments());
    } catch (cause) {
      setData(null);
      setError(
        cause instanceof ApiError
          ? cause
          : new ApiError(String(cause), { code: "client_error", status: 0 }),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, session.activeWorkspace?.workspace_id]);

  const documents = data?.documents ?? [];
  const selected = documents.find((doc) => doc.document_id === selectedId) ?? null;

  const select = useCallback(
    (docId: string | null) => {
      if (docId) {
        router.replace(`/documents?doc=${encodeURIComponent(docId)}`, { scroll: false });
      } else {
        router.replace("/documents", { scroll: false });
      }
    },
    [router]
  );

  return (
    <div className={styles.layout}>
      <header className={styles.header}>
        <h1 className={styles.title}>Document Management</h1>
        {canManage && !showUpload && (
          <div className={styles.actions}>
            <Button onClick={() => setShowUpload(true)}>Upload Document</Button>
          </div>
        )}
      </header>

      {error ? (
        <Callout tone="fail" title="Failed to load documents">
          {error.message}
        </Callout>
      ) : null}

      <main className={styles.main}>
        {showUpload && canManage ? (
          <DocumentUpload 
            onCancel={() => setShowUpload(false)} 
            onSuccess={() => { setShowUpload(false); void load(); }}
          />
        ) : selected ? (
          <DocumentDetail 
            document={selected} 
            onClose={() => select(null)} 
            onDelete={canManage ? async () => {
              await deleteDocument(selected.document_id);
              select(null);
              void load();
            } : undefined}
          />
        ) : loading ? (
          <SkeletonRows rows={5} label="Loading documents..." />
        ) : documents.length > 0 ? (
          <div className={styles.list}>
            <DocumentList documents={documents} onSelect={select} />
          </div>
        ) : (
          <EmptyState title="No documents">
            <p>There are no documents in this workspace yet.</p>
          </EmptyState>
        )}
      </main>
    </div>
  );
}
