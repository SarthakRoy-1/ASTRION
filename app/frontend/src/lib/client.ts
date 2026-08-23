/**
 * The only place the frontend talks to the backend.
 *
 * Everything above this file works with typed results; everything below it is
 * HTTP. Three rules the client holds so no component has to:
 *
 * - **A failure never returns as a success.** Any non-2xx response becomes a
 *   thrown `ApiError` carrying the backend's structured code. A component
 *   cannot accidentally render an error body as an answer.
 * - **No business logic passes through here.** The client sends the identity
 *   the user selected and the message they typed. It does not compute scope,
 *   decide what a role may do, or interpret a policy result — all of that is
 *   the backend's, and duplicating any of it here would create a second
 *   implementation that drifts.
 * - **Only a read-only request is ever repeated.** The hosted backend sleeps
 *   when idle and takes tens of seconds to come back, so a request may have to
 *   be made more than once before anyone is listening. That tolerance is
 *   offered to `GET /health` and `GET /api/principals` and to nothing else: a
 *   repeated POST could prepare a second action or write a second conversation
 *   turn, and no amount of convenience is worth that.
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

/* -- cold starts ------------------------------------------------------------
 *
 * The deployed backend runs on an instance that spins down after a period of
 * inactivity; the first request afterwards has to wait for it to come back,
 * which the host itself warns can take fifty seconds or more. Untreated, that
 * is indistinguishable from a dead server: the request fails, and the UI
 * announces that the API cannot be reached when in fact it is on its way up.
 *
 * The treatment is deliberately narrow — a bounded number of retries, with
 * growing gaps and a wall-clock ceiling, offered only to requests whose
 * repetition cannot change anything.
 */

export interface ColdStartPolicy {
  /** How long one attempt may run before it is abandoned as unanswered. */
  attemptTimeoutMs: number;
  /**
   * The gap before each retry. Its length *is* the retry count: there is no
   * path on which more attempts are made than there are entries here.
   */
  retryDelaysMs: readonly number[];
  /** Wall-clock ceiling. Once reached, no further retry is scheduled. */
  budgetMs: number;
}

/**
 * Sized for the free-tier cold start this deployment actually has.
 *
 * Seven attempts at most, and no retry scheduled once 75 seconds have elapsed,
 * which puts the ceiling at about 90 seconds — the final attempt may still be
 * running when the budget expires. Long enough to outlast a spin-up the host
 * describes as "50 seconds or more"; short enough that a genuinely dead
 * backend gets reported rather than waited on indefinitely.
 */
export const COLD_START_POLICY: ColdStartPolicy = {
  attemptTimeoutMs: 15_000,
  retryDelaysMs: [1_000, 2_000, 4_000, 8_000, 8_000, 8_000],
  budgetMs: 75_000,
};

export interface RequestOptions {
  /**
   * Retry through a cold start. Omitting it means one attempt and no more,
   * which is the default precisely so that tolerance is something a caller
   * asks for knowingly rather than something every request quietly inherits.
   */
  coldStart?: ColdStartPolicy;
  /** Called before each wait, so the UI can say what it is waiting for. */
  onRetry?: (attempt: number, delayMs: number) => void;
  /** Abandon the whole sequence — an unmounting component, a context switch. */
  signal?: AbortSignal;
}

/** Thrown when the caller abandoned the request; never shown as a failure. */
const CANCELLED_CODE = "request_cancelled";

/**
 * Whether this failure is consistent with an instance that is still waking.
 *
 * A transport fault or an unanswered attempt is the ordinary signature. So is
 * a gateway status: while the instance spins up it is the host's router, not
 * the application, that answers, and it answers 502/503/504. Anything else
 * means something *did* handle the request, and repeating it would be asking a
 * working server the same question twice.
 */
function isColdStartSignal(error: ApiError): boolean {
  if (error.code === "network_error") return true;
  return (
    error.status === 408 ||
    error.status === 502 ||
    error.status === 503 ||
    error.status === 504
  );
}

/**
 * Whether the last completed request got an answer from *something*.
 *
 * Any HTTP response counts, a refusal included: a 403 proves the backend is
 * awake just as well as a 200 does. Callers read this to decide whether a wake
 * probe is worth running at all, so an already-warm backend costs nothing.
 */
let backendReachable = false;

export function isBackendReachable(): boolean {
  return backendReachable;
}

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
    backendReachable = false;
    throw new ApiError(NETWORK_ERROR_MESSAGE, {
      code: "network_error",
      status: 0,
      details: { cause: cause instanceof Error ? cause.message : String(cause) },
    });
  }
  backendReachable = true;

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

/** One attempt, abandoned if it goes unanswered for longer than `timeoutMs`. */
async function attempt<T>(
  path: string,
  timeoutMs: number,
  external: AbortSignal | undefined,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const forward = () => controller.abort();
  external?.addEventListener("abort", forward);
  try {
    return await request<T>(path, { signal: controller.signal });
  } finally {
    clearTimeout(timer);
    external?.removeEventListener("abort", forward);
  }
}

function cancelled(): ApiError {
  return new ApiError("Request cancelled.", { code: CANCELLED_CODE, status: 0 });
}

function wait(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        reject(cancelled());
      },
      { once: true },
    );
  });
}

/**
 * A read-only GET, optionally retried while the instance wakes.
 *
 * Two properties matter more than the delays themselves. The loop cannot run
 * longer than the policy allows — retries are capped by the number of delays
 * *and* by the elapsed budget, whichever runs out first, so there is no input
 * that turns this into a poll. And what it finally throws is the last real
 * failure, so a genuinely dead backend still reports itself as unreachable
 * rather than as perpetually "still waking".
 */
async function get<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const policy = options.coldStart;
  if (!policy) return request<T>(path, { signal: options.signal });

  const startedAt = Date.now();

  for (let index = 0; ; index += 1) {
    if (options.signal?.aborted) throw cancelled();

    try {
      return await attempt<T>(path, policy.attemptTimeoutMs, options.signal);
    } catch (error) {
      if (options.signal?.aborted) throw cancelled();

      const apiError = error instanceof ApiError ? error : asUnknownFailure(error);
      const delayMs = policy.retryDelaysMs[index];
      if (delayMs === undefined || !isColdStartSignal(apiError)) throw apiError;
      if (Date.now() - startedAt + delayMs >= policy.budgetMs) throw apiError;

      options.onRetry?.(index + 1, delayMs);
      await wait(delayMs, options.signal);
    }
  }
}

function asUnknownFailure(error: unknown): ApiError {
  return new ApiError(error instanceof Error ? error.message : String(error), {
    code: "client_error",
    status: 0,
  });
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
export function getHealth(options: RequestOptions = {}): Promise<HealthResponse> {
  return get<HealthResponse>("/health", options);
}

/**
 * Wait for the backend to answer, and report whether it did.
 *
 * `GET /health` is the request to spend a cold start on: it exists already,
 * answers 200 even when the dataset is missing, and changes nothing. This
 * resolves rather than throws, because "is anybody there" is a question with
 * two ordinary answers.
 *
 * A backend that answers with an *error* still counts as awake. The probe asks
 * whether the round trip completes, not whether the deployment is healthy;
 * conflating the two would send the UI into another ninety-second wait over a
 * fault that waiting cannot fix.
 */
export async function wakeBackend(options: RequestOptions = {}): Promise<boolean> {
  try {
    await getHealth({ coldStart: COLD_START_POLICY, ...options });
    return true;
  } catch {
    return backendReachable;
  }
}

/**
 * The demo identities this deployment accepts.
 *
 * The context selector is populated from this rather than from a list baked
 * into the UI: the server owns the identity directory, and a frontend copy
 * would be a second source of truth for who exists and what they may see.
 */
export async function listPrincipals(
  options: RequestOptions = {},
): Promise<PrincipalView[]> {
  const body = await get<PrincipalsResponse>("/api/principals", options);
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
 *
 * Sent exactly once, whatever happens. A chat turn is persisted and may
 * prepare an action, so repeating it after a lost response would leave a
 * duplicate behind. A caller that expects a sleeping backend warms it with
 * `wakeBackend` *before* this rather than resending it afterwards.
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
 *
 * Never retried, under any condition. This is the one request that changes the
 * world, and a confirmation whose response was lost may well have executed —
 * so it is reported as failed and left for a person to decide about.
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
