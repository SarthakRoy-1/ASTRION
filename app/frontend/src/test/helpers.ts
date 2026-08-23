/**
 * Test doubles for the backend.
 *
 * The fixtures these helpers serve are *recorded* from the real API by
 * `scripts/export_ui_fixtures.py`, and `tests/test_frontend_contract.py` fails
 * the backend suite if any of them drifts. So a UI test that passes here is
 * rendering a payload the backend genuinely produces — not one written from
 * memory that happens to satisfy the component.
 *
 * No test reaches the network: `vitest.setup.ts` replaces `fetch` with a stub
 * that throws, and every test installs its own routes over it.
 */

import { vi } from "vitest";

import actionExecuted from "./fixtures/action-executed.json";
import chatCancellation from "./fixtures/chat-cancellation.json";
import chatCrossAccountDenied from "./fixtures/chat-cross-account-denied.json";
import chatKnownIssue from "./fixtures/chat-known-issue.json";
import chatPendingAction from "./fixtures/chat-pending-action.json";
import chatServiceCredit from "./fixtures/chat-service-credit.json";
import chatServiceCreditProvisional from "./fixtures/chat-service-credit-provisional.json";
import chatSlaBreach from "./fixtures/chat-sla-breach.json";
import chatSupersededPolicy from "./fixtures/chat-superseded-policy.json";
import chatUncertain from "./fixtures/chat-uncertain.json";
import errorActionNotPending from "./fixtures/error-action-not-pending.json";
import errorUnknownIdentity from "./fixtures/error-unknown-identity.json";
import health from "./fixtures/health.json";
import principals from "./fixtures/principals.json";
import type {
  ActionConfirmationResponse,
  ChatResponse,
  HealthResponse,
  PrincipalsResponse,
} from "@/lib/types";

export const fixtures = {
  principals: principals as PrincipalsResponse,
  health: health as HealthResponse,
  cancellation: chatCancellation as ChatResponse,
  serviceCredit: chatServiceCredit as ChatResponse,
  serviceCreditProvisional: chatServiceCreditProvisional as ChatResponse,
  slaBreach: chatSlaBreach as ChatResponse,
  knownIssue: chatKnownIssue as ChatResponse,
  supersededPolicy: chatSupersededPolicy as ChatResponse,
  uncertain: chatUncertain as ChatResponse,
  crossAccountDenied: chatCrossAccountDenied as ChatResponse,
  pendingAction: chatPendingAction as ChatResponse,
  actionExecuted: actionExecuted as ActionConfirmationResponse,
  errorActionNotPending: errorUnknownIdentityShape(errorActionNotPending),
  errorUnknownIdentity: errorUnknownIdentityShape(errorUnknownIdentity),
};

function errorUnknownIdentityShape(body: unknown) {
  return body as { error: { code: string; message: string } };
}

/** A queued reply: either a JSON body with a status, or a thrown network fault. */
export interface Reply {
  status?: number;
  body?: unknown;
  networkError?: boolean;
}

export interface ApiStub {
  /** Every request the UI made, in order. */
  calls: { url: string; method: string; body: unknown; identity: string | null }[];
  /** Requests refused because the backend was modelled as still asleep. */
  refused: number;
  /** Queue the next reply for POST /api/chat. */
  onChat: (reply: Reply) => void;
  /** Queue the next reply for a confirmation POST. */
  onConfirm: (reply: Reply) => void;
}

/**
 * Install a `fetch` that serves the recorded fixtures.
 *
 * Chat and confirmation replies are queues, so a test can script a sequence —
 * a success then a conflict, for instance — without re-stubbing between
 * assertions. An exhausted queue repeats its last entry, which keeps a test
 * that sends one more message than it scripted from failing for an unrelated
 * reason.
 *
 * `sleeping` models the deployment's own failure mode rather than a synthetic
 * one: a spun-down instance refuses the first requests outright, whatever they
 * ask for, and serves normally once it is up. Counting the refusals lets a
 * test assert that the UI waited rather than gave up — and that it did not
 * send a state-changing request twice while waiting.
 */
export function stubApi(
  options: {
    principals?: Reply;
    chat?: Reply[];
    confirm?: Reply[];
    sleeping?: number;
  } = {},
): ApiStub {
  const calls: ApiStub["calls"] = [];
  let asleep = options.sleeping ?? 0;
  let refused = 0;
  const chatQueue: Reply[] = [...(options.chat ?? [{ body: fixtures.cancellation }])];
  const confirmQueue: Reply[] = [
    ...(options.confirm ?? [{ body: fixtures.actionExecuted }]),
  ];
  const principalsReply = options.principals ?? { body: fixtures.principals };

  function take(queue: Reply[]): Reply {
    return queue.length > 1 ? queue.shift()! : (queue[0] ?? { body: null });
  }

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const method = init?.method ?? "GET";
      const headers = (init?.headers ?? {}) as Record<string, string>;

      calls.push({
        url,
        method,
        body: init?.body ? JSON.parse(init.body as string) : null,
        identity: headers["X-ParcelPilot-User"] ?? null,
      });

      if (asleep > 0) {
        asleep -= 1;
        refused += 1;
        return respond({ networkError: true });
      }

      if (url.includes("/api/principals")) return respond(principalsReply);
      if (url.includes("/health")) return respond({ body: fixtures.health });
      if (url.includes("/confirm")) return respond(take(confirmQueue));
      if (url.includes("/api/chat")) return respond(take(chatQueue));

      throw new Error(`unexpected request: ${method} ${url}`);
    }),
  );

  const stub: ApiStub = {
    calls,
    get refused() {
      return refused;
    },
    onChat: (reply) => chatQueue.push(reply),
    onConfirm: (reply) => confirmQueue.push(reply),
  };
  return stub;
}

function respond(reply: Reply): Promise<Response> {
  if (reply.networkError) {
    return Promise.reject(new TypeError("Failed to fetch"));
  }
  const status = reply.status ?? 200;
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    json: async () => reply.body,
  } as Response);
}

/** Chat requests only, for asserting what the UI actually sent. */
export function chatCalls(stub: ApiStub) {
  return stub.calls.filter((call) => call.url.includes("/api/chat"));
}

export function confirmCalls(stub: ApiStub) {
  return stub.calls.filter((call) => call.url.includes("/confirm"));
}
