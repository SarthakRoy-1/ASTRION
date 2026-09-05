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
import type {
  CurrentUser,
  MemberListing,
  Workspace,
  WorkspaceListing,
} from "@/lib/auth-types";
import type { SignalReport } from "@/lib/operations-types";

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
 * A signed-in deployment, as `/health`, `/api/auth/me` and `/api/workspaces`
 * describe it.
 *
 * Demo mode is the default everywhere else because that is what the recorded
 * fixtures were captured under. This is how a test asks for the *other* mode —
 * the one a real deployment runs, where identity comes from a cookie,
 * `/api/principals` is empty, and what a caller may do comes from their
 * membership rather than from a persona.
 */
export interface SessionFixture {
  user?: Partial<CurrentUser>;
  workspace?: Partial<Workspace>;
  /** Overrides the workspace's own permission list. */
  permissions?: string[];
  members?: Partial<MemberListing>;
  signals?: SignalReport | Reply;
}

/** The names the server really issues — see `app/backend/auth/permissions.py`. */
const DEFAULT_PERMISSIONS = [
  "run_agent",
  "read_records",
  "read_documents",
  "propose_action",
  "execute_action",
  "operations.read",
  "members.read",
  "members.invite",
];

export function sessionUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    user_id: "USR-test",
    display_name: "Ada Support",
    org_id: "ORG-test",
    org_name: "Northstar Logistics",
    role: "support_agent",
    permissions: DEFAULT_PERMISSIONS,
    account_scope: ["ACCT-001", "ACCT-002"],
    memberships: [],
    auth_mode: "session",
    ...overrides,
  };
}

export function sessionWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    workspace_id: "ORG-test",
    name: "Northstar Logistics",
    slug: "northstar-logistics",
    created_at_utc: "2026-08-01T00:00:00Z",
    role: "operations",
    permissions: DEFAULT_PERMISSIONS,
    ...overrides,
  };
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
    /** Serve a real session instead of the demo identity header. */
    session?: SessionFixture;
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

  const session = options.session;
  const workspace = session
    ? sessionWorkspace({
        ...session.workspace,
        ...(session.permissions ? { permissions: session.permissions } : {}),
      })
    : null;
  const user = session ? sessionUser(session.user) : null;
  const workspaceListing: WorkspaceListing | null = workspace
    ? {
        workspaces: [workspace],
        active_workspace_id: workspace.workspace_id,
        needs_workspace: false,
      }
    : null;
  const memberListing: MemberListing | null = workspace
    ? {
        workspace_id: workspace.workspace_id,
        members: [
          {
            user_id: user!.user_id,
            email: "ada@northstar.example",
            display_name: user!.display_name,
            role: workspace.role ?? "operations",
            status: "active",
          },
        ],
        owner_count: 1,
        ...session?.members,
      }
    : null;

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

      if (url.includes("/api/principals")) {
        // A real deployment's directory of demo personas is empty: identity
        // there comes from the session cookie, not from a picker.
        return respond(session ? { body: { principals: [] } } : principalsReply);
      }
      if (url.includes("/health")) {
        return respond({
          body: session
            ? { ...fixtures.health, auth_mode: "session" }
            : fixtures.health,
        });
      }
      if (session) {
        if (url.includes("/api/auth/me")) return respond({ body: user });
        if (url.includes("/api/operations/signals")) {
          const reply = session.signals;
          return respond(
            reply && "signals" in reply
              ? { body: reply }
              : ((reply as Reply | undefined) ?? {
                  body: {
                    signals: [],
                    count: 0,
                    highest_severity: null,
                    reference_time: null,
                    scope_account_ids: [],
                  },
                }),
          );
        }
        if (url.includes("/members")) return respond({ body: memberListing });
        if (url.includes("/invitations")) {
          return respond({ body: { invitations: [] } });
        }
        if (url.includes("/api/workspaces")) {
          return respond({ body: workspaceListing });
        }
      }
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
