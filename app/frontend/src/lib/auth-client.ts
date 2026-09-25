/**
 * The authentication and workspace half of the API client.
 *
 * Separate from `client.ts` for the same reason `auth-types.ts` is separate:
 * that file is the conversation contract, this one is who is having it. They
 * share the transport (`request` below mirrors it) and nothing else.
 *
 * Two properties this module holds so no component has to:
 *
 * - **The session travels as a cookie and only as a cookie.** Every call sets
 *   `credentials: "include"`. There is no token in a variable, in
 *   `localStorage`, or in a header — the cookie is `HttpOnly`, so this code
 *   could not read it even if it wanted to, and neither can an injected script.
 * - **A failure never returns as a success.** Any non-2xx becomes a thrown
 *   `ApiError` carrying the backend's structured code, so a component cannot
 *   render an error body as content.
 */

import { ApiError, apiBaseUrl } from "./client";
import type {
  AuditListing,
  CurrentUser,
  Invitation,
  LoginResult,
  OAuthProvider,
  VerificationStatus,
  MemberListing,
  Workspace,
  WorkspaceListing,
  WorkspaceRole,
} from "./auth-types";
import type { ApiErrorEnvelope } from "./types";

const NETWORK_ERROR_MESSAGE =
  "Could not reach the ASTRION API. Check that the backend is running.";

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(init.headers as Record<string, string> | undefined),
      },
      // The session is an HttpOnly cookie, so it only travels if the request
      // asks for it. The backend's Origin check and its CORS allow-list are
      // what keep this safe; a wildcard origin is refused at startup precisely
      // so that stays true.
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
    // A non-JSON body (a proxy error page, an empty 502) must not become a
    // silent `undefined` that a component then renders as blank.
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

const post = <T>(path: string, payload?: unknown) =>
  request<T>(path, {
    method: "POST",
    ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
  });

/* -- authentication -------------------------------------------------------- */

export function signIn(email: string, password: string): Promise<LoginResult> {
  return post<LoginResult>("/api/auth/login", { email, password });
}

/**
 * Enter the public demo, with no credential in the browser.
 *
 * Takes no arguments, and that is the design rather than a convenience: the
 * address and password live in the backend's configuration, so there is
 * nothing here to publish, nothing to inline into the bundle, and nothing a
 * reader of this code could sign in as somebody else with. The response is
 * shaped like `signIn`'s because it *is* a sign-in — the server verifies its
 * own credential through the ordinary login path and sets the same HttpOnly
 * cookie.
 *
 * It may take a few seconds on a sleeping deployment: the backend builds the
 * demo database, ingests the dataset and indexes the documents before it
 * answers. That wait is the whole feature, and the button says so.
 */
export function demoSignIn(): Promise<LoginResult> {
  return post<LoginResult>("/api/auth/demo-login");
}

/** Complete the second factor on a session that has passed a password only. */
export function submitMfaCode(code: string): Promise<{ status: string }> {
  return post("/api/auth/mfa/challenge", { code });
}

export interface RegistrationResult {
  status: string;
  message: string;
  email_sent: boolean;
  verification: VerificationStatus;
}

export function register(input: {
  email: string;
  password: string;
  displayName: string;
}): Promise<RegistrationResult> {
  return post("/api/auth/register", {
    email: input.email,
    password: input.password,
    display_name: input.displayName,
  });
}

/* -- email verification by code --------------------------------------------- */
/*
 * None of these takes an email address. The server acts on the verification
 * this browser holds as an HttpOnly cookie — issued at registration, or when a
 * correct password meets an unverified address — so there is nothing here that
 * could be pointed at somebody else's account.
 */

/** The verification carried by a thrown `ApiError`, if it carries one. */
export function verificationFrom(error: unknown): VerificationStatus | null {
  if (!(error instanceof ApiError)) return null;
  const candidate = (error.details as { verification?: unknown }).verification;
  return candidate && typeof candidate === "object"
    ? (candidate as VerificationStatus)
    : null;
}

export function fetchVerification(): Promise<{
  pending: boolean;
  verification: VerificationStatus | null;
}> {
  return request("/api/auth/verification");
}

export function submitVerificationCode(code: string): Promise<LoginResult> {
  return post<LoginResult>("/api/auth/verification/verify", { code });
}

export function resendVerificationCode(): Promise<{
  status: "code_sent" | "delivery_failed";
  verification: VerificationStatus;
}> {
  return post("/api/auth/verification/resend");
}

/** A Google/GitHub sign-in without a verified address: choose one to prove. */
export function chooseVerificationEmail(email: string): Promise<{
  status: "code_sent" | "delivery_failed";
  verification: VerificationStatus;
}> {
  return post("/api/auth/verification/email", { email });
}

export function cancelVerification(): Promise<{ status: string }> {
  return post("/api/auth/verification/cancel");
}

/* -- sign-in with Google / GitHub -------------------------------------------- */

/**
 * Where "Continue with Google/GitHub" sends the browser.
 *
 * A navigation, not a fetch: the provider's consent screen has to be a page,
 * and the state cookie that protects the callback is set on the way out. The
 * backend chooses where the browser returns to; nothing here can.
 */
export function oauthStartUrl(provider: OAuthProvider): string {
  return `${apiBaseUrl()}/api/auth/oauth/${provider}/start`;
}

/** Legacy verification links (issued before codes replaced them). */
export function verifyEmail(token: string): Promise<{ status: string }> {
  return post("/api/auth/verify-email", { token });
}

export interface ResendState {
  can_resend: boolean;
  seconds_until_allowed: number;
  sends_used: number;
  in_cooldown: boolean;
}

export function resendVerification(email: string): Promise<{
  status: string;
  message: string;
  email_sent: boolean;
  verification_token?: string;
  resend_state: ResendState;
}> {
  return post("/api/auth/resend-verification", { email });
}

export function signOut(): Promise<{ status: string }> {
  return post("/api/auth/logout");
}

/**
 * Where this browser stands: signed in, half signed in (a second factor is
 * outstanding), part-way through verifying an address, or signed out.
 *
 * One request — `/api/auth/me` — answers all four, because its refusal says
 * which kind of "not signed in" this is.
 */
export type SessionState =
  | { kind: "user"; user: CurrentUser }
  | { kind: "mfa" }
  | { kind: "verification"; verification: VerificationStatus }
  | { kind: "signed-out" };

export async function fetchSessionState(): Promise<SessionState> {
  try {
    return { kind: "user", user: await request<CurrentUser>("/api/auth/me") };
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      if ((error.details as { mfa_required?: boolean }).mfa_required) {
        return { kind: "mfa" };
      }
      const verification = verificationFrom(error);
      return verification
        ? { kind: "verification", verification }
        : { kind: "signed-out" };
    }
    throw error;
  }
}

/** The signed-in user, or `null` when nobody is signed in.
 *
 *  A 401 is an ordinary answer to "who am I", not a failure, so it resolves
 *  rather than throwing — otherwise every first page load would look broken. */
export async function fetchCurrentUser(): Promise<CurrentUser | null> {
  try {
    return await request<CurrentUser>("/api/auth/me");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

/* -- workspaces ------------------------------------------------------------ */

/**
 * The audit trail for the caller's own workspace.
 *
 * There is deliberately no workspace parameter. The endpoint derives the scope
 * from the session and offers nothing that could widen it, so this signature
 * has nothing to widen it *with* — the absence is the control, not an
 * oversight.
 */
export function fetchAuditLog(limit = 100): Promise<AuditListing> {
  return request<AuditListing>(`/api/auth/audit?limit=${encodeURIComponent(limit)}`);
}

export function listWorkspaces(): Promise<WorkspaceListing> {
  return request<WorkspaceListing>("/api/workspaces");
}

export function createWorkspace(name: string): Promise<Workspace> {
  return post<Workspace>("/api/workspaces", { name });
}

export function renameWorkspace(
  workspaceId: string,
  name: string,
): Promise<Workspace> {
  return request<Workspace>(`/api/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

/** Make this the workspace the agent runs against.
 *
 *  The choice is written to the server-side session, not held here: a client
 *  that could name its own tenant per request would be choosing its own
 *  authorization. */
export function activateWorkspace(workspaceId: string): Promise<Workspace> {
  return post<Workspace>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/activate`,
  );
}

/* -- members --------------------------------------------------------------- */

export function listMembers(workspaceId: string): Promise<MemberListing> {
  return request<MemberListing>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/members`,
  );
}

export function changeMemberRole(
  workspaceId: string,
  userId: string,
  role: WorkspaceRole,
): Promise<{ status: string }> {
  return request(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`,
    { method: "PATCH", body: JSON.stringify({ role }) },
  );
}

export function removeMember(
  workspaceId: string,
  userId: string,
): Promise<{ status: string; left: boolean }> {
  return request(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
  );
}

export function transferOwnership(
  workspaceId: string,
  userId: string,
): Promise<{ status: string }> {
  return post(`/api/workspaces/${encodeURIComponent(workspaceId)}/ownership`, {
    user_id: userId,
  });
}

/* -- invitations ----------------------------------------------------------- */

export function listInvitations(
  workspaceId: string,
): Promise<{ invitations: Invitation[] }> {
  return request(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/invitations`,
  );
}

export function inviteMember(
  workspaceId: string,
  email: string,
  role: WorkspaceRole,
): Promise<Invitation> {
  return post<Invitation>(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/invitations`,
    { email, role },
  );
}

export function revokeInvitation(
  workspaceId: string,
  invitationId: string,
): Promise<{ status: string }> {
  return request(
    `/api/workspaces/${encodeURIComponent(workspaceId)}/invitations/${encodeURIComponent(invitationId)}`,
    { method: "DELETE" },
  );
}

/** Redeem an invitation.
 *
 *  The token goes in the body, never in the path or a query string: a token in
 *  a URL is written to browser history, sent in `Referer` to whatever the next
 *  page loads from, and recorded in every access log in between. */
export function acceptInvitation(token: string): Promise<Workspace> {
  return post<Workspace>("/api/invitations/accept", { token });
}
