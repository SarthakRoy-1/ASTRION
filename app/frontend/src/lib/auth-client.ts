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
  MemberListing,
  Workspace,
  WorkspaceListing,
  WorkspaceRole,
} from "./auth-types";
import type { ApiErrorEnvelope } from "./types";

const NETWORK_ERROR_MESSAGE =
  "Could not reach the ParcelPilot API. Check that the backend is running.";

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

/** Complete the second factor on a session that has passed a password only. */
export function submitMfaCode(code: string): Promise<{ status: string }> {
  return post("/api/auth/mfa/challenge", { code });
}

export function register(input: {
  email: string;
  password: string;
  displayName: string;
}): Promise<{ status: string; message: string; verification_token?: string }> {
  return post("/api/auth/register", {
    email: input.email,
    password: input.password,
    display_name: input.displayName,
  });
}

export function verifyEmail(token: string): Promise<{ status: string }> {
  return post("/api/auth/verify-email", { token });
}

export function signOut(): Promise<{ status: string }> {
  return post("/api/auth/logout");
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
