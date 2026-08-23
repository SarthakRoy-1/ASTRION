"use client";

/**
 * The conversation's state machine.
 *
 * It owns three things and nothing else: which identity is active, the
 * conversations belonging to each identity, and the turns rendered on screen.
 * Every decision with consequences — what the identity may see, whether a
 * figure is right, whether an action may run — belongs to the backend and is
 * only *displayed* from here.
 *
 * Conversations are stored per identity, never in one shared list that the UI
 * filters. `byIdentity[userId]` is the whole of what that identity can reach,
 * so there is no code path on which one customer's transcript can be handed to
 * another — the isolation is structural rather than a rendering condition.
 * That mirrors the backend, where account scope is enforced in the data layer
 * rather than by asking the model nicely.
 *
 * Three rules worth stating because they are easy to get wrong:
 *
 * - **Switching identity preserves both conversations.** A session binds
 *   prepared actions, and sessions never cross identities because each
 *   identity keeps its own threads. Returning to an identity resumes its own
 *   session, which the backend re-validates against that identity anyway.
 * - **A reply lands in the thread that sent it.** The identity and thread are
 *   captured when the request goes out, so switching context mid-flight cannot
 *   drop one customer's answer into another customer's transcript.
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

/** How much of the opening question becomes the conversation's label. */
const TITLE_MAX_LENGTH = 48;

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

/** One conversation belonging to one identity. */
export interface ConversationThread {
  id: string;
  /** Taken from the opening question; null until one is asked. */
  title: string | null;
  turns: Turn[];
  /** The backend session bound to this thread, once it has issued one. */
  sessionId: string | null;
}

/** Everything one identity can reach. Never merged across identities. */
interface IdentityThreads {
  threads: ConversationThread[];
  activeId: string;
}

/** A conversation as the history list needs it — no turns, no session. */
export interface ConversationSummary {
  id: string;
  title: string | null;
  messageCount: number;
  isActive: boolean;
}

let turnCounter = 0;
function nextTurnId(prefix: string): string {
  turnCounter += 1;
  return `${prefix}-${turnCounter}`;
}

let threadCounter = 0;
function nextThreadId(): string {
  threadCounter += 1;
  return `thread-${threadCounter}`;
}

function emptyThread(): ConversationThread {
  return { id: nextThreadId(), title: null, turns: [], sessionId: null };
}

function newIdentityThreads(): IdentityThreads {
  const thread = emptyThread();
  return { threads: [thread], activeId: thread.id };
}

/**
 * A short label for a conversation, taken from its opening question.
 *
 * Only the user's own words, only within their own context, and only the first
 * line — a title is a navigation aid, not a place to surface record detail.
 */
function deriveTitle(message: string): string {
  const firstLine = message.trim().split("\n")[0]?.trim() ?? "";
  if (firstLine.length <= TITLE_MAX_LENGTH) return firstLine;
  return `${firstLine.slice(0, TITLE_MAX_LENGTH).trimEnd()}…`;
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

  /** Conversations belonging to the active identity, newest first. */
  conversations: ConversationSummary[];
  activeConversationId: string | null;

  selectIdentity: (userId: string) => void;
  selectConversation: (conversationId: string) => void;
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
  const [byIdentity, setByIdentity] = useState<Record<string, IdentityThreads>>({});
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

  // Give an identity its first, empty conversation the moment it becomes
  // active. Idempotent, so returning to an identity never disturbs the
  // conversations it already has.
  useEffect(() => {
    if (!identity) return;
    setByIdentity((current) =>
      current[identity] ? current : { ...current, [identity]: newIdentityThreads() },
    );
  }, [identity]);

  const active = identity ? byIdentity[identity] : undefined;
  const activeThread = active?.threads.find((thread) => thread.id === active.activeId);

  const turns = useMemo(() => activeThread?.turns ?? [], [activeThread]);
  const sessionId = activeThread?.sessionId ?? null;

  /**
   * Start a fresh conversation for the active identity.
   *
   * The previous one is kept and stays reachable from the history list. An
   * already-empty conversation is reused rather than stacking indistinguishable
   * "New conversation" entries.
   */
  const reset = useCallback(() => {
    const target = identityRef.current;
    if (!target) return;

    setSending(false);
    setByIdentity((current) => {
      const state = current[target] ?? newIdentityThreads();
      const existing = state.threads.find((thread) => thread.id === state.activeId);
      if (existing && existing.turns.length === 0) {
        return { ...current, [target]: { ...state, activeId: existing.id } };
      }
      const fresh = emptyThread();
      return {
        ...current,
        [target]: { threads: [fresh, ...state.threads], activeId: fresh.id },
      };
    });
  }, []);

  /**
   * Switch identity without disturbing any conversation.
   *
   * Each identity keeps its own threads, so the one being left is preserved
   * exactly as it stands and the one being entered resumes where it was.
   */
  const selectIdentity = useCallback((userId: string) => {
    setIdentity((current) => {
      if (current === userId) return current;
      setSending(false);
      rememberIdentity(userId);
      return userId;
    });
  }, []);

  const selectConversation = useCallback((conversationId: string) => {
    const target = identityRef.current;
    if (!target) return;

    setByIdentity((current) => {
      const state = current[target];
      // Only ever selectable within the active identity's own list.
      if (!state || !state.threads.some((thread) => thread.id === conversationId)) {
        return current;
      }
      return { ...current, [target]: { ...state, activeId: conversationId } };
    });
  }, []);

  const send = useCallback(
    async (message: string) => {
      const text = message.trim();
      const activeIdentity = identityRef.current;
      if (!text || !activeIdentity || sending) return;

      // Bind this request to the conversation it was sent from. Switching
      // context while it is in flight must not land the answer elsewhere.
      const state = byIdentity[activeIdentity];
      const threadId = state?.activeId;
      const thread = state?.threads.find((t) => t.id === threadId);
      if (!threadId) return;
      const threadSession = thread?.sessionId ?? null;

      updateThread(setByIdentity, activeIdentity, threadId, (current) => ({
        ...current,
        title: current.title ?? deriveTitle(text),
        turns: [...current.turns, { kind: "user", id: nextTurnId("user"), text }],
      }));
      setSending(true);

      try {
        const response = await sendChat({
          message: text,
          identity: activeIdentity,
          sessionId: threadSession,
        });
        // The backend issues the session on the first request and expects it
        // back on every later one; it is what binds a prepared action to this
        // conversation.
        updateThread(setByIdentity, activeIdentity, threadId, (current) => ({
          ...current,
          sessionId: response.session_id,
          turns: [
            ...current.turns,
            {
              kind: "agent",
              id: nextTurnId("agent"),
              response,
              action: initialActionProgress(response),
            },
          ],
        }));
      } catch (error: unknown) {
        updateThread(setByIdentity, activeIdentity, threadId, (current) => ({
          ...current,
          turns: [
            ...current.turns,
            { kind: "error", id: nextTurnId("error"), error: asApiError(error) },
          ],
        }));
      } finally {
        setSending(false);
      }
    },
    [byIdentity, sending],
  );

  const respondToAction = useCallback(
    async (turnId: string, decision: "approve" | "reject") => {
      const activeIdentity = identityRef.current;
      if (!activeIdentity) return;

      const state = byIdentity[activeIdentity];
      const threadId = state?.activeId;
      const thread = state?.threads.find((t) => t.id === threadId);
      if (!thread || !threadId) return;

      const turn = thread.turns.find((t) => t.id === turnId);
      if (!turn || turn.kind !== "agent") return;

      const proposed = turn.response.proposed_action;
      // Guard here as well as on the button: a keyboard repeat or a double
      // submit must not produce two requests for one approval.
      if (!proposed || turn.action.submitting || turn.action.state !== "pending_confirmation") {
        return;
      }
      const boundSession = thread.sessionId;
      if (!boundSession) return;

      updateAction(setByIdentity, activeIdentity, threadId, turnId, (action) => ({
        ...action,
        submitting: decision,
        error: null,
      }));

      try {
        const result = await confirmAction({
          actionId: proposed.action_id,
          decision,
          identity: activeIdentity,
          sessionId: boundSession,
          fingerprint: proposed.parameter_fingerprint,
        });
        updateAction(setByIdentity, activeIdentity, threadId, turnId, () => ({
          state: result.action_status,
          submitting: null,
          executed: result.action,
          error: null,
        }));
      } catch (error: unknown) {
        const apiError = asApiError(error);
        updateAction(setByIdentity, activeIdentity, threadId, turnId, (action) => ({
          ...action,
          submitting: null,
          // A conflict means the backend already moved this action on. Leaving
          // it displayed as pending would invite a click that cannot work.
          state: apiError.isActionConflict ? "failed" : action.state,
          error: apiError,
        }));
      }
    },
    [byIdentity],
  );

  const principal = useMemo(
    () => principals.find((p) => p.user_id === identity) ?? null,
    [principals, identity],
  );

  // Only ever the active identity's own conversations. There is deliberately
  // no shape here that could carry another identity's threads to the UI.
  const conversations = useMemo<ConversationSummary[]>(() => {
    if (!active) return [];
    return active.threads.map((thread) => ({
      id: thread.id,
      title: thread.title,
      messageCount: thread.turns.filter((turn) => turn.kind === "user").length,
      isActive: thread.id === active.activeId,
    }));
  }, [active]);

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
    conversations,
    activeConversationId: active?.activeId ?? null,
    selectIdentity,
    selectConversation,
    send,
    respondToAction,
    reset,
  };
}

function initialActionProgress(response: ChatResponse): ActionProgress {
  return {
    state: response.action_status ?? "none",
    submitting: null,
    executed: null,
    error: null,
  };
}

type ThreadStore = Record<string, IdentityThreads>;

/** Update one thread belonging to one identity, leaving every other alone. */
function updateThread(
  setStore: React.Dispatch<React.SetStateAction<ThreadStore>>,
  identity: string,
  threadId: string,
  update: (thread: ConversationThread) => ConversationThread,
): void {
  setStore((current) => {
    const state = current[identity];
    if (!state) return current;
    return {
      ...current,
      [identity]: {
        ...state,
        threads: state.threads.map((thread) =>
          thread.id === threadId ? update(thread) : thread,
        ),
      },
    };
  });
}

function updateAction(
  setStore: React.Dispatch<React.SetStateAction<ThreadStore>>,
  identity: string,
  threadId: string,
  turnId: string,
  update: (action: ActionProgress) => ActionProgress,
): void {
  updateThread(setStore, identity, threadId, (thread) => ({
    ...thread,
    turns: thread.turns.map((turn) =>
      turn.kind === "agent" && turn.id === turnId
        ? { ...turn, action: update(turn.action) }
        : turn,
    ),
  }));
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
