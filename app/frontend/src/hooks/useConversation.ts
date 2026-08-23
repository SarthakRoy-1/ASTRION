"use client";

/**
 * The conversation's state machine.
 *
 * It owns three things and nothing else: which identity is active, the
 * session the backend issued, and the turns rendered on screen. Every decision
 * with consequences — what the identity may see, whether a figure is right,
 * whether an action may run — belongs to the backend and is only *displayed*
 * from here.
 *
 * Two rules worth stating because they are easy to get wrong:
 *
 * - **Switching identity starts a new conversation.** A session binds prepared
 *   actions; carrying one across an identity change would leave a proposal
 *   made under one scope sitting in a conversation running under another.
 *   Resetting is both safer and the honest thing to show in a demo.
 * - **A confirmation in flight blocks another.** The backend makes execution
 *   single-use regardless, but a UI that lets a second click through and then
 *   reports `action_not_pending` has told the user they did something wrong
 *   when they did not.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, confirmAction, getHealth, listPrincipals, sendChat } from "@/lib/client";
import type {
  ActionState,
  ChatResponse,
  ExecutedActionView,
  HealthResponse,
  PrincipalView,
} from "@/lib/types";

/** Remembers the demo identity between visits. A preference, not a credential. */
const IDENTITY_STORAGE_KEY = "parcelpilot.identity";

export interface ActionProgress {
  /** The action's state as last reported by the backend. */
  state: ActionState;
  /** Which decision is currently in flight, if any. */
  submitting: "approve" | "reject" | null;
  /** The audit record, once the action reached a terminal state. */
  executed: ExecutedActionView | null;
  /** A confirmation that failed — shown on the card, not as a chat turn. */
  error: ApiError | null;
}

export type Turn =
  | { kind: "user"; id: string; text: string }
  | { kind: "agent"; id: string; response: ChatResponse; action: ActionProgress }
  | { kind: "error"; id: string; error: ApiError };

let turnCounter = 0;
function nextTurnId(prefix: string): string {
  turnCounter += 1;
  return `${prefix}-${turnCounter}`;
}

function initialActionProgress(response: ChatResponse): ActionProgress {
  return {
    state: response.action_status ?? "none",
    submitting: null,
    executed: null,
    error: null,
  };
}

export interface Conversation {
  identity: string | null;
  principals: PrincipalView[];
  principal: PrincipalView | null;
  principalsError: ApiError | null;
  loadingPrincipals: boolean;
  /** What this deployment is running. Null until known, and null if unknown. */
  health: HealthResponse | null;

  turns: Turn[];
  sending: boolean;
  sessionId: string | null;
  hasStarted: boolean;

  selectIdentity: (userId: string) => void;
  send: (message: string) => Promise<void>;
  respondToAction: (
    turnId: string,
    decision: "approve" | "reject",
  ) => Promise<void>;
  reset: () => void;
}

export function useConversation(): Conversation {
  const [principals, setPrincipals] = useState<PrincipalView[]>([]);
  const [principalsError, setPrincipalsError] = useState<ApiError | null>(null);
  const [loadingPrincipals, setLoadingPrincipals] = useState(true);

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [identity, setIdentity] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  // Read inside async callbacks so a rapid identity switch cannot make an
  // in-flight reply land in the wrong conversation.
  const identityRef = useRef<string | null>(null);
  identityRef.current = identity;

  useEffect(() => {
    let cancelled = false;

    listPrincipals()
      .then((loaded) => {
        if (cancelled) return;
        setPrincipals(loaded);

        const remembered = readRememberedIdentity();
        const known = loaded.find((p) => p.user_id === remembered);
        // Fall back to the first identity the *server* offers rather than to a
        // name hard-coded here: the directory is the backend's.
        setIdentity(known?.user_id ?? loaded[0]?.user_id ?? null);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setPrincipalsError(asApiError(error));
      })
      .finally(() => {
        if (!cancelled) setLoadingPrincipals(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  // Configuration, not conversation: which provider is answering, how much of
  // the source pack is indexed, and which snapshot the dates are judged
  // against. A failure here is deliberately silent — it tells the user nothing
  // they can act on, and the chat reports its own faults perfectly well.
  useEffect(() => {
    let cancelled = false;

    getHealth()
      .then((loaded) => {
        if (!cancelled) setHealth(loaded);
      })
      .catch(() => {
        /* Status is a nicety. Never surface it as a failure. */
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const reset = useCallback(() => {
    setTurns([]);
    setSessionId(null);
    setSending(false);
  }, []);

  const selectIdentity = useCallback(
    (userId: string) => {
      setIdentity((current) => {
        if (current === userId) return current;
        reset();
        rememberIdentity(userId);
        return userId;
      });
    },
    [reset],
  );

  const send = useCallback(
    async (message: string) => {
      const text = message.trim();
      const activeIdentity = identityRef.current;
      if (!text || !activeIdentity || sending) return;

      setTurns((current) => [
        ...current,
        { kind: "user", id: nextTurnId("user"), text },
      ]);
      setSending(true);

      try {
        const response = await sendChat({
          message: text,
          identity: activeIdentity,
          sessionId,
        });
        // The backend issues the session on the first request and expects it
        // back on every later one; it is what binds a prepared action to this
        // conversation.
        setSessionId(response.session_id);
        setTurns((current) => [
          ...current,
          {
            kind: "agent",
            id: nextTurnId("agent"),
            response,
            action: initialActionProgress(response),
          },
        ]);
      } catch (error: unknown) {
        setTurns((current) => [
          ...current,
          { kind: "error", id: nextTurnId("error"), error: asApiError(error) },
        ]);
      } finally {
        setSending(false);
      }
    },
    [sending, sessionId],
  );

  const respondToAction = useCallback(
    async (turnId: string, decision: "approve" | "reject") => {
      const turn = turns.find((t) => t.id === turnId);
      const activeIdentity = identityRef.current;
      if (!turn || turn.kind !== "agent" || !activeIdentity) return;

      const proposed = turn.response.proposed_action;
      // Guard here as well as on the button: a keyboard repeat or a double
      // submit must not produce two requests for one approval.
      if (!proposed || turn.action.submitting || turn.action.state !== "pending_confirmation") {
        return;
      }
      if (!sessionId) return;

      updateAction(setTurns, turnId, (action) => ({
        ...action,
        submitting: decision,
        error: null,
      }));

      try {
        const result = await confirmAction({
          actionId: proposed.action_id,
          decision,
          identity: activeIdentity,
          sessionId,
          fingerprint: proposed.parameter_fingerprint,
        });
        updateAction(setTurns, turnId, () => ({
          state: result.action_status,
          submitting: null,
          executed: result.action,
          error: null,
        }));
      } catch (error: unknown) {
        const apiError = asApiError(error);
        updateAction(setTurns, turnId, (action) => ({
          ...action,
          submitting: null,
          // A conflict means the backend already moved this action on. Leaving
          // it displayed as pending would invite a click that cannot work.
          state: apiError.isActionConflict ? "failed" : action.state,
          error: apiError,
        }));
      }
    },
    [sessionId, turns],
  );

  const principal = useMemo(
    () => principals.find((p) => p.user_id === identity) ?? null,
    [principals, identity],
  );

  return {
    identity,
    principals,
    principal,
    principalsError,
    loadingPrincipals,
    health,
    turns,
    sending,
    sessionId,
    hasStarted: turns.length > 0,
    selectIdentity,
    send,
    respondToAction,
    reset,
  };
}

function updateAction(
  setTurns: React.Dispatch<React.SetStateAction<Turn[]>>,
  turnId: string,
  update: (action: ActionProgress) => ActionProgress,
): void {
  setTurns((current) =>
    current.map((turn) =>
      turn.kind === "agent" && turn.id === turnId
        ? { ...turn, action: update(turn.action) }
        : turn,
    ),
  );
}

/**
 * Normalise anything thrown into an `ApiError`.
 *
 * A bug in the UI must still surface as a visible failure rather than an
 * unhandled rejection that leaves the interface stuck on "Investigating…".
 */
function asApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError(
    error instanceof Error ? error.message : "Something went wrong.",
    { code: "client_error", status: 0 },
  );
}

function readRememberedIdentity(): string | null {
  try {
    return window.localStorage.getItem(IDENTITY_STORAGE_KEY);
  } catch {
    return null;
  }
}

function rememberIdentity(userId: string): void {
  try {
    window.localStorage.setItem(IDENTITY_STORAGE_KEY, userId);
  } catch {
    // Private browsing or a blocked store. Losing a UI preference is not worth
    // failing the interaction over.
  }
}
