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
 * filters. `byIdentity[key]` is the whole of what that identity can reach,
 * so there is no code path on which one customer's transcript can be handed to
 * another — the isolation is structural rather than a rendering condition.
 * That mirrors the backend, where account scope is enforced in the data layer
 * rather than by asking the model nicely.
 *
 * The key is the demo persona where there is one, and `SESSION_THREAD_KEY`
 * where identity comes from the session cookie instead. Both modes therefore
 * share one shape, and neither can read the other's threads.
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

import {
  ApiError,
  COLD_START_POLICY,
  confirmAction,
  getHealth,
  isBackendReachable,
  listPrincipals,
  sendChat,
  wakeBackend,
} from "@/lib/client";
import type {
  ActionState,
  ChatResponse,
  ExecutedActionView,
  HealthResponse,
  PrincipalView,
} from "@/lib/types";

/** Remembers the demo identity between visits. A preference, not a credential. */
const IDENTITY_STORAGE_KEY = "astrion.identity";

/**
 * The bucket conversations live in when there is no demo persona to key them
 * by — that is, under real session authentication, where identity comes from
 * an `HttpOnly` cookie the browser cannot read.
 *
 * Deliberately not a user id. The store is keyed per identity so that one
 * demo persona's threads can never be handed to another; under session auth
 * there is exactly one identity per browser session, so one reserved bucket
 * preserves that structure without inventing an id the server never issued.
 * The sentinel cannot collide with a real one: the server's demo directory
 * issues dotted names like `support.agent`, and a colon is not among the
 * characters those ids use.
 */
const SESSION_THREAD_KEY = "session:cookie";

/** How much of the opening question becomes the conversation's label. */
const TITLE_MAX_LENGTH = 48;

/**
 * How long the opening request may run before the UI admits to waiting.
 *
 * Short enough that a cold start never looks like a frozen page, long enough
 * that a warm backend — which answers in a few tens of milliseconds — never
 * flashes a status the reader has no time to read.
 */
const SLOW_START_MS = 1_200;

/**
 * How far the UI has got in reaching the backend.
 *
 * The distinction that earns its keep is `waking` versus `unavailable`. The
 * deployment sleeps when idle and takes tens of seconds to return, so the
 * first request after a quiet period fails in exactly the way a dead server
 * does. Announcing "cannot reach the API" at that moment is simply wrong, and
 * it is wrong in the way that makes a working system look broken.
 *
 * - `starting`  — the first attempt is in flight and it is too early to say
 *                 anything; the normal case, and it shows nothing.
 * - `connecting`— that attempt is taking longer than a warm backend would.
 * - `waking`    — an attempt failed and the client is retrying while the
 *                 instance spins up.
 * - `ready`     — the backend has answered.
 * - `unavailable`— the bounded wake-up strategy ran out. Now it is a fault.
 */
export type ConnectionState =
  | "starting"
  | "connecting"
  | "waking"
  | "ready"
  | "unavailable";

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

/**
 * Where a conversation came from, when it did not come from the composer.
 *
 * Set when an operations signal is handed to the assistant. It is display
 * state and nothing else: the question sent to the backend names the signal,
 * and the agent fetches it through its own tool rather than being told what
 * the operations screen already believes. Keeping it on the thread is what
 * lets a reader move between the answer and the signal without losing either.
 */
export interface ConversationOrigin {
  signalId: string;
  title: string;
}

/** One conversation belonging to one identity. */
export interface ConversationThread {
  id: string;
  /** Taken from the opening question; null until one is asked. */
  title: string | null;
  turns: Turn[];
  /** The backend session bound to this thread, once it has issued one. */
  sessionId: string | null;
  origin: ConversationOrigin | null;
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
  return {
    id: nextThreadId(),
    title: null,
    turns: [],
    sessionId: null,
    origin: null,
  };
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
  /** How far the UI has got in reaching the backend. */
  connection: ConnectionState;
  /** What this deployment is running. Null until known, and null if unknown. */
  health: HealthResponse | null;

  turns: Turn[];
  sending: boolean;
  sessionId: string | null;
  hasStarted: boolean;
  /** The signal this conversation was started from, if it was. */
  origin: ConversationOrigin | null;
  /**
   * Whether a message would actually reach the backend.
   *
   * The composer needs this and cannot work it out: under session
   * authentication there is no persona to wait for, and under demo
   * authentication there is nothing else to send. Deriving it here rather than
   * in the page is what stops the two disagreeing — which is exactly what
   * happened before, leaving an enabled composer that silently dropped every
   * message a signed-in user typed.
   */
  canSend: boolean;

  /** Conversations belonging to the active identity, newest first. */
  conversations: ConversationSummary[];
  activeConversationId: string | null;

  selectIdentity: (userId: string) => void;
  selectConversation: (conversationId: string) => void;
  send: (message: string, origin?: ConversationOrigin) => Promise<void>;
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
  const [connection, setConnection] = useState<ConnectionState>("starting");

  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [identity, setIdentity] = useState<string | null>(null);
  const [byIdentity, setByIdentity] = useState<Record<string, IdentityThreads>>({});
  const [sending, setSending] = useState(false);

  // Read inside async callbacks so a rapid identity switch cannot make an
  // in-flight reply land in the wrong conversation.
  const identityRef = useRef<string | null>(null);
  identityRef.current = identity;

  // Which bucket of `byIdentity` the current caller's threads live in. Equal
  // to the demo persona when there is one, and to the reserved session key
  // when identity comes from the cookie instead.
  const threadKey = identity ?? SESSION_THREAD_KEY;
  const threadKeyRef = useRef<string>(threadKey);
  threadKeyRef.current = threadKey;

  // True once a wake-up has been attempted and failed. Without it, every
  // message typed at a backend that really is down would restart the whole
  // ninety-second wait instead of failing at once.
  const wakeExhausted = useRef(false);

  // A cold start is invisible for its first second or so, because the request
  // is simply outstanding. Say nothing for that long — then say something,
  // rather than leaving a blank page that reads as a hang.
  useEffect(() => {
    if (connection !== "starting") return;
    const timer = setTimeout(() => {
      setConnection((current) => (current === "starting" ? "connecting" : current));
    }, SLOW_START_MS);
    return () => clearTimeout(timer);
  }, [connection]);

  // The identity directory is a read-only GET, so it is safe to ask for again
  // — and it is what gates the whole UI, which makes it the right request to
  // carry the cold start. Failing it on the first attempt would report a
  // sleeping instance as an unreachable one.
  useEffect(() => {
    let cancelled = false;

    listPrincipals({
      coldStart: COLD_START_POLICY,
      onRetry: () => {
        if (!cancelled) setConnection("waking");
      },
    })
      .then((loaded) => {
        if (cancelled) return;
        setConnection("ready");
        setPrincipals(loaded);

        const remembered = readRememberedIdentity();
        const known = loaded.find((p) => p.user_id === remembered);
        // Fall back to the first identity the *server* offers rather than to a
        // name hard-coded here: the directory is the backend's.
        setIdentity(known?.user_id ?? loaded[0]?.user_id ?? null);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        // The bounded strategy is spent. Only now is this a fault worth
        // reporting, and it is reported with the backend's own error.
        setConnection("unavailable");
        wakeExhausted.current = true;
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

    getHealth({ coldStart: COLD_START_POLICY })
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

  // Give the active caller their first, empty conversation. Idempotent, so
  // returning to a demo persona never disturbs the conversations it already
  // has, and the session bucket is created once and reused.
  useEffect(() => {
    setByIdentity((current) =>
      current[threadKey] ? current : { ...current, [threadKey]: newIdentityThreads() },
    );
  }, [threadKey]);

  const active = byIdentity[threadKey];
  const activeThread = active?.threads.find((thread) => thread.id === active.activeId);

  const turns = useMemo(() => activeThread?.turns ?? [], [activeThread]);
  const sessionId = activeThread?.sessionId ?? null;
  const origin = activeThread?.origin ?? null;

  /**
   * Start a fresh conversation for the active identity.
   *
   * The previous one is kept and stays reachable from the history list. An
   * already-empty conversation is reused rather than stacking indistinguishable
   * "New conversation" entries.
   */
  const reset = useCallback(() => {
    const target = threadKeyRef.current;
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
    const target = threadKeyRef.current;

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
    async (message: string, origin?: ConversationOrigin) => {
      const text = message.trim();
      const activeIdentity = identityRef.current;
      const key = threadKeyRef.current;
      if (!text || sending) return;
      // Demo mode is the only mode that needs a persona: there, the identity
      // *is* the request's authority. Under session authentication the cookie
      // carries it and `user_id` is ignored by the server, so waiting for a
      // persona that will never arrive is what silently swallowed every
      // message a signed-in user typed.
      if (health?.auth_mode === "demo_header" && !activeIdentity) return;

      // Bind this request to the conversation it was sent from. Switching
      // context while it is in flight must not land the answer elsewhere.
      const state = byIdentity[key];
      const threadId = state?.activeId;
      const thread = state?.threads.find((t) => t.id === threadId);
      if (!threadId) return;
      const threadSession = thread?.sessionId ?? null;

      updateThread(setByIdentity, key, threadId, (current) => ({
        ...current,
        title: current.title ?? deriveTitle(text),
        // Only ever set, never cleared by a later message: a thread that began
        // as an investigation stays traceable to it however long it runs.
        origin: current.origin ?? origin ?? null,
        turns: [...current.turns, { kind: "user", id: nextTurnId("user"), text }],
      }));
      setSending(true);

      try {
        // The instance may have gone back to sleep since the page loaded. Wake
        // it with the read-only probe *first*, then send the message once.
        // The alternative — send, fail, resend — risks a second persisted turn
        // and a second prepared action for one question the user asked once.
        if (!isBackendReachable() && !wakeExhausted.current) {
          setConnection("waking");
          const awake = await wakeBackend({
            onRetry: () => setConnection("waking"),
          });
          wakeExhausted.current = !awake;
          setConnection(awake ? "ready" : "unavailable");
        }

        const response = await sendChat({
          message: text,
          identity: activeIdentity,
          sessionId: threadSession,
        });
        // The backend answered, so whatever the last wake attempt concluded is
        // out of date: the next message may probe again.
        wakeExhausted.current = false;
        setConnection("ready");
        // The backend issues the session on the first request and expects it
        // back on every later one; it is what binds a prepared action to this
        // conversation.
        updateThread(setByIdentity, key, threadId, (current) => ({
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
        const apiError = asApiError(error);
        // Not retried, deliberately: this message may already have been
        // recorded and may already have prepared an action. The failure is
        // shown as a turn, and re-asking is the user's decision to make.
        if (apiError.code === "network_error") setConnection("unavailable");
        updateThread(setByIdentity, key, threadId, (current) => ({
          ...current,
          turns: [
            ...current.turns,
            { kind: "error", id: nextTurnId("error"), error: apiError },
          ],
        }));
      } finally {
        setSending(false);
      }
    },
    [byIdentity, health, sending],
  );

  const respondToAction = useCallback(
    async (turnId: string, decision: "approve" | "reject") => {
      const activeIdentity = identityRef.current;
      const key = threadKeyRef.current;

      const state = byIdentity[key];
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

      updateAction(setByIdentity, key, threadId, turnId, (action) => ({
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
        updateAction(setByIdentity, key, threadId, turnId, () => ({
          state: result.action_status,
          submitting: null,
          executed: result.action,
          error: null,
        }));
      } catch (error: unknown) {
        const apiError = asApiError(error);
        updateAction(setByIdentity, key, threadId, turnId, (action) => ({
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

  // Either kind of identity will do, and the composer must not wait on both.
  //
  // Written as an `or` rather than as a branch on the auth mode deliberately:
  // the mode is only known once `/health` lands, and a demo whose status probe
  // failed — which is caught and ignored, because a status line is never worth
  // an error — must stay usable. So a chosen persona alone is enough, and so
  // is a deployment that has said it authenticates by session.
  const canSend =
    (Boolean(identity) && !loadingPrincipals) ||
    (health !== null && health.auth_mode !== "demo_header");

  return {
    identity,
    principals,
    principal,
    principalsError,
    loadingPrincipals,
    connection,
    health,
    turns,
    sending,
    sessionId,
    hasStarted: turns.length > 0,
    origin,
    canSend,
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
