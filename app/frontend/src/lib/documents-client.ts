import { ApiError, apiBaseUrl } from "./client";
import type { ApiErrorEnvelope } from "./types";
import type { DocumentListResponse, DocumentChunksResponse, IngestionStatusResponse } from "./documents-types";

const NETWORK_ERROR_MESSAGE =
  "Could not reach the ASTRION API. Check that the backend is running.";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      ...options,
      headers: { 
        ...(options?.headers && !(options.headers instanceof Headers) ? options.headers : {}),
      },
      credentials: "include",
    });
  } catch (cause) {
    throw new ApiError(NETWORK_ERROR_MESSAGE, {
      code: "network_error",
      status: 0,
      details: { cause: cause instanceof Error ? cause.message : String(cause) },
    });
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const error = (body as ApiErrorEnvelope | null)?.error;
    throw new ApiError(error?.message ?? response.statusText ?? "Request failed", {
      code: error?.code ?? "http_error",
      status: response.status,
      details: error?.details ?? {},
      requestId: error?.request_id ?? null,
    });
  }
  return body as T;
}

export function fetchDocuments(): Promise<DocumentListResponse> {
  return request<DocumentListResponse>("/api/documents");
}

export function fetchDocumentChunks(documentId: string): Promise<DocumentChunksResponse> {
  return request<DocumentChunksResponse>(`/api/documents/${encodeURIComponent(documentId)}/chunks`);
}

export function fetchIngestionStatus(): Promise<IngestionStatusResponse> {
  return request<IngestionStatusResponse>("/api/documents/ingestion-status");
}

export function triggerReindex(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/api/documents/reindex", { method: "POST" });
}

export function deleteDocument(documentId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(`/api/documents/${encodeURIComponent(documentId)}`, { method: "DELETE" });
}

export async function uploadDocument(file: File): Promise<{ ok: boolean, counts: { documents: number, chunks: number } }> {
  const formData = new FormData();
  formData.append("file", file);

  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}/api/documents/upload`, {
      method: "POST",
      body: formData,
      credentials: "include",
    });
  } catch (cause) {
    throw new ApiError(NETWORK_ERROR_MESSAGE, {
      code: "network_error",
      status: 0,
      details: { cause: cause instanceof Error ? cause.message : String(cause) },
    });
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const error = (body as ApiErrorEnvelope | null)?.error;
    throw new ApiError(error?.message ?? response.statusText ?? "Upload failed", {
      code: error?.code ?? "http_error",
      status: response.status,
      details: error?.details ?? {},
      requestId: error?.request_id ?? null,
    });
  }
  return body as { ok: boolean, counts: { documents: number, chunks: number } };
}
