/**
 * The only place the frontend talks to the backend.
 *
 * Everything above this file works with typed results; everything below it is
 * HTTP. Two rules the client holds so no component has to:
 *
 * - **A failure never returns as a success.** Any non-2xx response becomes a
 *   thrown `ApiError` carrying the backend's structured code. A component
 *   cannot accidentally render an error body as an answer.
 * - **No business logic passes through here.** The client sends the identity
 *   the user selected and the message they typed. It does not compute scope,
 *   decide what a role may do, or interpret a policy result — all of that is
 *   the backend's, and duplicating any of it here would create a second
 *   implementation that drifts.
 */

import type {
  ActionConfirmationRequest,
  ActionConfirmationResponse,
  ApiErrorEnvelope,
  ChatRequest,
  ChatResponse,
  HealthResponse,
  PrincipalsResponse,
  PrincipalView,
} from "./types";

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

/** Header the backend reads the caller's asserted identity from. */
const IDENTITY_HEADER = "X-ParcelPilot-User";

export function apiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  return (configured || DEFAULT_API_BASE_URL).replace(/\/+$/, "");
}

/**
 * A failed request, with the backend's own error code preserved.
 *
 * `code` is what the UI branches on — an authorization refusal reads
 * differently from a provider outage — while `message` is the backend's
 * user-safe text. Neither is ever invented here.
 */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;

  constructor(
    message: string,
    options: {
      code: string;
      status: number;
      details?: Record<string, unknown>;
      requestId?: string | null;
    },
  ) {
    super(message);
    this.name = "ApiError";
    this.code = options.code;
    this.status = options.status;
    this.details = options.details ?? {};
    this.requestId = options.requestId ?? null;
  }

  /** True when the caller asked for something outside their account scope. */
  get isAuthorization(): boolean {
    return this.code === "forbidden" || this.code === "unauthenticated";
  }

  /** True when the language-model provider, not the request, was at fault. */
  get isProvider(): boolean {
    return (
      this.code === "provider_error" ||
      this.code === "provider_timeout" ||
      this.code === "provider_not_configured"
    );
  }

  /** True when the action's state moved on and the request no longer applies. */
  get isActionConflict(): boolean {
    return (
      this.code === "action_not_pending" ||
      this.code === "action_session_mismatch" ||
      this.code === "action_execution_failed"
    );
  }
}

const NETWORK_ERROR_MESSAGE =
  "Could not reach the ParcelPilot API. Check that the backend is running.";

async function request<T>(
  path: string,
  init: RequestInit & { identity?: string } = {},
): Promise<T> {
  const { identity, headers, ...rest } = init;

  const requestHeaders: Record<string, string> = {
    Accept: "application/json",
    ...(headers as Record<string, string> | undefined),
  };
  if (identity) {
    requestHeaders[IDENTITY_HEADER] = identity;
  }

  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      ...rest,
      headers: requestHeaders,
    });
  } catch (cause) {
    // A dead backend is not a server error; saying "500" here would send the
    // reader looking in the wrong place.
    throw new ApiError(NETWORK_ERROR_MESSAGE, {
      code: "network_error",
      status: 0,
      details: { cause: cause instanceof Error ? cause.message : String(cause) },
    });
  }

  const body = await readJson(response);

  if (!response.ok) {
    const envelope = body as ApiErrorEnvelope | null;
    const error = envelope?.error;
    throw new ApiError(error?.message ?? response.statusText ?? "Request failed", {
      code: error?.code ?? "http_error",
      status: response.status,
      details: error?.details ?? {},
      requestId: error?.request_id ?? null,
    });
  }

  return body as T;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    // A body that is not JSON (a proxy error page, an empty 502) must not
    // become a silent `undefined` that a component then renders as blank.
    return null;
  }
}

function postJson<T>(path: string, payload: unknown, identity?: string): Promise<T> {
  return request<T>(path, {
    method: "POST",
    identity,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** Liveness, the active provider mode, and whether the dataset is built. */
export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

/**
 * The demo identities this deployment accepts.
 *
 * The context selector is populated from this rather than from a list baked
 * into the UI: the server owns the identity directory, and a frontend copy
 * would be a second source of truth for who exists and what they may see.
 */
export async function listPrincipals(): Promise<PrincipalView[]> {
  const body = await request<PrincipalsResponse>("/api/principals");
  return body.principals ?? [];
}

/**
 * Send one natural-language request.
 *
 * Note what is *not* sent: no account scope, no role, no permissions. The
 * identity is asserted and the server resolves what it may reach. The
 * `account_scope` field the API accepts can only narrow, never widen, and the
 * UI does not use it — letting the browser choose a scope, even a narrower
 * one, would blur where authority comes from.
 */
export function sendChat(input: {
  message: string;
  identity: string;
  sessionId: string | null;
}): Promise<ChatResponse> {
  const payload: ChatRequest = {
    message: input.message,
    user_id: input.identity,
    ...(input.sessionId ? { session_id: input.sessionId } : {}),
  };
  return postJson<ChatResponse>("/api/chat", payload, input.identity);
}

/**
 * Approve or reject one prepared action.
 *
 * The fingerprint the API returned with the preview is echoed back, so the
 * backend can verify that what the operator reviewed is what runs. The session
 * id is required: an action prepared in one conversation cannot be confirmed
 * from another.
 */
export function confirmAction(input: {
  actionId: string;
  decision: "approve" | "reject";
  identity: string;
  sessionId: string;
  fingerprint: string;
}): Promise<ActionConfirmationResponse> {
  const payload: ActionConfirmationRequest = {
    decision: input.decision,
    user_id: input.identity,
    session_id: input.sessionId,
    expected_fingerprint: input.fingerprint,
  };
  return postJson<ActionConfirmationResponse>(
    `/api/actions/${encodeURIComponent(input.actionId)}/confirm`,
    payload,
    input.identity,
  );
}
