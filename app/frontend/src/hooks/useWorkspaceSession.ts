"use client";

/**
 * Who is signed in, which workspace they are acting in, and how to change both.
 *
 * One hook owns the whole answer, because the three questions the app needs —
 * *is anyone signed in*, *do they have a workspace*, *which one is active* —
 * are answered by the same two requests and would drift if split.
 *
 * The state machine has four resting places, and the page routes on `stage`:
 *
 *     loading      still asking the server
 *     demo         this deployment has no real auth (AUTH_MODE=demo_header)
 *     signed-out   nobody is signed in
 *     mfa-required a password or provider sign-in, awaiting the second factor
 *     verify-email proving an address with an emailed code, before any session
 *     onboarding   signed in, belongs to no workspace
 *     ready        signed in, in a workspace
 *
 * `demo` exists because the deployed demo runs on the mock identity header and
 * has no users at all. Rather than showing a sign-in form that cannot work, the
 * app keeps its original persona picker in that mode. The distinction comes
 * from the server (`/health` reports `auth_mode`), never from a build flag.
 *
 * **Nothing here decides what the user may do.** `permissions` is carried so
 * the UI can hide controls that would fail; the server re-checks every one of
 * them at the point of use.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  activateWorkspace as apiActivate,
  cancelVerification as apiCancelVerification,
  createWorkspace as apiCreate,
  demoSignIn as apiDemoSignIn,
  fetchSessionState,
  listWorkspaces,
  signIn as apiSignIn,
  signOut as apiSignOut,
  submitMfaCode as apiSubmitMfa,
  verificationFrom,
} from "@/lib/auth-client";
import { ApiError } from "@/lib/client";
import { writeSessionHint } from "@/lib/session-hint";
import type {
  AuthMode,
  CurrentUser,
  LoginResult,
  OAuthProvider,
  VerificationStatus,
  Workspace,
} from "@/lib/auth-types";
import type { HealthResponse } from "@/lib/types";

export type SessionStage =
  | "loading"
  | "demo"
  | "signed-out"
  | "mfa-required"
  | "verify-email"
  | "onboarding"
  | "ready";

export interface WorkspaceSession {
  stage: SessionStage;
  user: CurrentUser | null;
  workspaces: Workspace[];
  activeWorkspace: Workspace | null;
  authMode: AuthMode | null;
  /**
   * Whether this deployment offers one-click demo access.
   *
   * Answered by the server on `/health`, never by a build flag — a frontend
   * that decided this for itself could offer a button no backend would honour,
   * which is exactly the failure the credential used to be published to avoid.
   */
  demoAvailable: boolean;
  /** Which "Continue with …" providers the server has credentials for. */
  oauthProviders: OAuthProvider[];
  /** The address being proven, while `stage` is `verify-email`. */
  verification: VerificationStatus | null;
  error: string | null;
  busy: boolean;

  signIn(email: string, password: string): Promise<void>;
  /** Enter the public demo. Sends no credential; the server holds its own. */
  signInToDemo(): Promise<void>;
  submitMfaCode(code: string): Promise<void>;
  /** Registration succeeded: the address is next. */
  beginVerification(verification: VerificationStatus): void;
  /** A code was accepted and the server started a session. */
  completeVerification(result: LoginResult): Promise<void>;
  /** Leave the code screen and return to signing in. */
  abandonVerification(): Promise<void>;
  signOut(): Promise<void>;
  createWorkspace(name: string): Promise<void>;
  switchWorkspace(workspaceId: string): Promise<void>;
  refresh(): Promise<void>;
  clearError(): void;
  /** True when the caller's role in the active workspace grants `permission`. */
  can(permission: string): boolean;
}

/**
 * What a visitor is told when the *first* load fails.
 *
 * `data_unavailable` is the one code this cannot render verbatim. It means the
 * backend is awake but its data is not there yet — a cold start on a
 * deployment whose disk is rebuilt — and the honest thing to say about that is
 * "wait a moment", not whatever the API said. This is not a general policy of
 * rewriting backend messages: every other code is rendered as the server wrote
 * it, because the server worded it carefully.
 */
const INITIALISING_MESSAGE =
  "The ASTRION environment is still starting up. Please retry in a moment.";

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    return error.code === "data_unavailable" ? INITIALISING_MESSAGE : error.message;
  }
  return error instanceof Error ? error.message : String(error);
}

/**
 * @param health `GET /health`, or `null` while it is still being fetched.
 *
 * Taken as an argument rather than fetched here on purpose. The app already
 * probes health once, with cold-start tolerance, and a *second* probe would
 * double the requests a sleeping backend has to answer before anything renders
 * — and would race the first one to a different conclusion. One probe, one
 * answer, passed down.
 */
export function useWorkspaceSession(
  health: HealthResponse | null,
): WorkspaceSession {
  const [stage, setStage] = useState<SessionStage>("loading");
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [authMode, setAuthMode] = useState<AuthMode | null>(null);
  const [verification, setVerification] = useState<VerificationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Guards against a resolved request writing state after unmount, which React
  // reports as an update-on-unmounted-component warning and which would also
  // let a stale response overwrite a newer one.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /** Re-read identity and workspaces, and decide which stage that puts us in. */
  const load = useCallback(async (mode: AuthMode) => {
    if (mode === "demo_header") {
      if (alive.current) setStage("demo");
      return;
    }

    const state = await fetchSessionState();
    if (!alive.current) return;

    if (state.kind !== "user") {
      setUser(null);
      setWorkspaces([]);
      setActiveId(null);
      // The refusal from `/me` says which kind of "not signed in" this is, so
      // a reload in the middle of a second factor or a code lands back on
      // that screen rather than on a sign-in form that would start over.
      if (state.kind === "verification") {
        setVerification(state.verification);
        setStage("verify-email");
      } else {
        setVerification(null);
        setStage(state.kind === "mfa" ? "mfa-required" : "signed-out");
      }
      return;
    }

    setVerification(null);
    const current = state.user;
    setUser(current);
    const listing = await listWorkspaces();
    if (!alive.current) return;

    setWorkspaces(listing.workspaces);
    setActiveId(listing.active_workspace_id);
    setStage(listing.needs_workspace ? "onboarding" : "ready");
  }, []);

  // Health decides how identity works here, so nothing can be resolved until
  // it arrives. Until then the stage stays `loading`, which the page renders as
  // the ordinary app shell — so a backend that is still waking shows the
  // connection notice rather than a sign-in form that could not work anyway.
  // A backend we cannot reach is not a signed-out user.
  useEffect(() => {
    if (health === null) return;

    let cancelled = false;
    (async () => {
      try {
        const mode = ((health.auth_mode as AuthMode | undefined) ?? "session");
        if (cancelled || !alive.current) return;
        setAuthMode(mode);
        await load(mode);
      } catch (cause) {
        if (cancelled || !alive.current) return;
        setError(messageFor(cause));
        setStage("signed-out");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [health, load]);

  const run = useCallback(
    async (work: () => Promise<void>) => {
      setBusy(true);
      setError(null);
      try {
        await work();
      } catch (cause) {
        if (alive.current) setError(messageFor(cause));
      } finally {
        if (alive.current) setBusy(false);
      }
    },
    [],
  );

  const signInToDemo = useCallback(
    () =>
      run(async () => {
        // No arguments, and none to forget: the credential is the server's.
        // A demo sign-in that came back needing a second factor would mean the
        // demo account had enrolled one, which `load` handles the same way it
        // handles anyone else's.
        const result = await apiDemoSignIn();
        if (!alive.current) return;
        if (result.mfa_required) {
          setStage("mfa-required");
          return;
        }
        await load("session");
      }),
    [load, run],
  );

  const signIn = useCallback(
    (email: string, password: string) =>
      run(async () => {
        let result: LoginResult;
        try {
          result = await apiSignIn(email, password);
        } catch (cause) {
          // The right password for an address never proven: the server has
          // sent a code. That is the next screen, not an error.
          const pending = verificationFrom(cause);
          if (pending === null) throw cause;
          if (!alive.current) return;
          setVerification(pending);
          setStage("verify-email");
          return;
        }
        if (!alive.current) return;
        if (result.mfa_required) {
          setStage("mfa-required");
          return;
        }
        await load("session");
      }),
    [load, run],
  );

  const beginVerification = useCallback((pending: VerificationStatus) => {
    setError(null);
    setVerification(pending);
    setStage("verify-email");
  }, []);

  const completeVerification = useCallback(
    (result: LoginResult) =>
      run(async () => {
        setVerification(null);
        if (result.mfa_required) {
          setStage("mfa-required");
          return;
        }
        await load("session");
      }),
    [load, run],
  );

  const abandonVerification = useCallback(
    () =>
      run(async () => {
        try {
          await apiCancelVerification();
        } finally {
          if (alive.current) {
            setVerification(null);
            setStage("signed-out");
          }
        }
      }),
    [run],
  );

  const submitMfaCode = useCallback(
    (code: string) =>
      run(async () => {
        await apiSubmitMfa(code);
        await load("session");
      }),
    [load, run],
  );

  const signOut = useCallback(
    () =>
      run(async () => {
        await apiSignOut();
        if (!alive.current) return;
        setUser(null);
        setWorkspaces([]);
        setActiveId(null);
        setStage("signed-out");
      }),
    [run],
  );

  const createWorkspace = useCallback(
    (name: string) =>
      run(async () => {
        await apiCreate(name);
        await load("session");
      }),
    [load, run],
  );

  const switchWorkspace = useCallback(
    (workspaceId: string) =>
      run(async () => {
        await apiActivate(workspaceId);
        // Re-read rather than assuming: the server decides what the new active
        // workspace grants, and the role may differ from the previous one.
        await load("session");
      }),
    [load, run],
  );

  const refresh = useCallback(
    () => run(() => load(authMode ?? "session")),
    [authMode, load, run],
  );

  // Remember, as a presentation hint only, whether this browser is signed in —
  // see `lib/session-hint.ts`. Written when the server has answered and never
  // while it is still loading, so a slow backend cannot erase it.
  useEffect(() => {
    if (stage === "ready" || stage === "onboarding" || stage === "demo") {
      writeSessionHint(true);
    } else if (stage === "signed-out") {
      writeSessionHint(false);
    }
  }, [stage]);

  const activeWorkspace = useMemo(
    () => workspaces.find((w) => w.workspace_id === activeId) ?? null,
    [workspaces, activeId],
  );

  const can = useCallback(
    (permission: string) =>
      Boolean(activeWorkspace?.permissions?.includes(permission)),
    [activeWorkspace],
  );

  return {
    stage,
    user,
    workspaces,
    activeWorkspace,
    authMode,
    demoAvailable: health?.demo_login_enabled ?? false,
    oauthProviders: (health?.oauth_providers ?? []).filter(
      (p): p is OAuthProvider => p === "google" || p === "github",
    ),
    verification,
    error,
    busy,
    signIn,
    signInToDemo,
    submitMfaCode,
    beginVerification,
    completeVerification,
    abandonVerification,
    signOut,
    createWorkspace,
    switchWorkspace,
    refresh,
    clearError: () => setError(null),
    can,
  };
}
