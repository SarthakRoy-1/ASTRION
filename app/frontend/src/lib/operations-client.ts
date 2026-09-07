/**
 * The operations-intelligence half of the API client.
 *
 * Separate from `client.ts` (the conversation contract) and `auth-client.ts`
 * (who is having it) for the same reason those are separate from each other:
 * three concerns, three files, one transport.
 *
 * The session travels as an `HttpOnly` cookie and only as a cookie — there is
 * no token in a variable here, and this code could not read one if there were.
 */

import { ApiError, apiBaseUrl } from "./client";
import type { OperationalSignal, SignalReport } from "./operations-types";
import type { ApiErrorEnvelope } from "./types";

const NETWORK_ERROR_MESSAGE =
  "Could not reach the ASTRION API. Check that the backend is running.";

async function request<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      headers: { Accept: "application/json" },
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

/** Every signal detected in the caller's workspace, already ranked by the server. */
export function fetchSignals(limit = 20): Promise<SignalReport> {
  return request<SignalReport>(`/api/operations/signals?limit=${limit}`);
}

/** One signal in full, re-derived server-side under the caller's own scope. */
export function fetchSignal(signalId: string): Promise<OperationalSignal> {
  return request<OperationalSignal>(
    `/api/operations/signals/${encodeURIComponent(signalId)}`,
  );
}
