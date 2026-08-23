/**
 * The API client's contract with the rest of the UI.
 *
 * The property that matters most: a failure must never come back looking like
 * a success. Every non-2xx response, and every transport fault, becomes a
 * thrown `ApiError` carrying the backend's own code — so no component can
 * accidentally render an error envelope as an answer.
 */

import { describe, expect, it, vi } from "vitest";

import {
  ApiError,
  confirmAction,
  getHealth,
  isBackendReachable,
  listPrincipals,
  sendChat,
  wakeBackend,
} from "./client";
import type { ColdStartPolicy } from "./client";
import { fixtures } from "@/test/helpers";

function stubResponse(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      ({
        ok: status >= 200 && status < 300,
        status,
        statusText: "",
        json: async () => body,
      }) as Response),
  );
}

describe("sendChat", () => {
  it("omits the session on the first message and sends it afterwards", async () => {
    stubResponse(200, fixtures.cancellation);

    await sendChat({ message: "hi", identity: "support.agent", sessionId: null });
    const first = JSON.parse(
      (vi.mocked(fetch).mock.calls[0]![1] as RequestInit).body as string,
    );
    expect(first).not.toHaveProperty("session_id");

    await sendChat({ message: "hi", identity: "support.agent", sessionId: "SES-1" });
    const second = JSON.parse(
      (vi.mocked(fetch).mock.calls[1]![1] as RequestInit).body as string,
    );
    expect(second.session_id).toBe("SES-1");
  });

  it("sends the identity as a header as well as in the body", async () => {
    stubResponse(200, fixtures.cancellation);

    await sendChat({ message: "hi", identity: "support.agent", sessionId: null });

    const init = vi.mocked(fetch).mock.calls[0]![1] as RequestInit;
    expect((init.headers as Record<string, string>)["X-ParcelPilot-User"]).toBe(
      "support.agent",
    );
  });
});

describe("error translation", () => {
  it("throws the backend's code rather than returning the envelope", async () => {
    stubResponse(401, fixtures.errorUnknownIdentity);

    await expect(
      sendChat({ message: "hi", identity: "nobody", sessionId: null }),
    ).rejects.toMatchObject({
      code: "unauthenticated",
      status: 401,
      message: fixtures.errorUnknownIdentity.error.message,
    });
  });

  it("classifies authorization, provider and action-conflict failures", async () => {
    const authorization = new ApiError("m", { code: "forbidden", status: 403 });
    const provider = new ApiError("m", { code: "provider_timeout", status: 504 });
    const conflict = new ApiError("m", { code: "action_not_pending", status: 409 });

    expect(authorization.isAuthorization).toBe(true);
    expect(provider.isProvider).toBe(true);
    expect(conflict.isActionConflict).toBe(true);
    expect(conflict.isAuthorization).toBe(false);
  });

  it("reports an unreachable backend distinctly from a server error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    // Reporting this as a 500 would send the reader looking in the wrong place.
    await expect(
      listPrincipals(),
    ).rejects.toMatchObject({ code: "network_error", status: 0 });
  });

  it("does not turn a non-JSON error body into a blank success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        ({
          ok: false,
          status: 502,
          statusText: "Bad Gateway",
          json: async () => {
            throw new SyntaxError("not json");
          },
        }) as unknown as Response),
    );

    await expect(
      sendChat({ message: "hi", identity: "support.agent", sessionId: null }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

describe("confirmAction", () => {
  it("posts the decision, session and reviewed fingerprint", async () => {
    stubResponse(200, fixtures.actionExecuted);

    await confirmAction({
      actionId: "ACT-abc",
      decision: "approve",
      identity: "support.manager",
      sessionId: "SES-1",
      fingerprint: "fp-1",
    });

    const [url, init] = vi.mocked(fetch).mock.calls[0]!;
    expect(String(url)).toContain("/api/actions/ACT-abc/confirm");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      decision: "approve",
      user_id: "support.manager",
      session_id: "SES-1",
      expected_fingerprint: "fp-1",
    });
  });

  it("escapes an action id rather than interpolating it raw", async () => {
    stubResponse(200, fixtures.actionExecuted);

    await confirmAction({
      actionId: "ACT/../evil",
      decision: "reject",
      identity: "support.manager",
      sessionId: "SES-1",
      fingerprint: "fp-1",
    });

    expect(String(vi.mocked(fetch).mock.calls[0]![0])).toContain("ACT%2F..%2Fevil");
  });
});

/**
 * Surviving a cold start.
 *
 * The deployed backend spins down when idle, and the request that wakes it can
 * take the better part of a minute. Two properties are being pinned here, and
 * they pull in opposite directions: a read-only request must be patient enough
 * to outlast the spin-up, and *nothing* may be patient enough to turn a dead
 * backend into an endless wait or a repeated write.
 *
 * The real policy waits up to about ninety seconds, which is not a thing to
 * spend in a test suite. These use the same code path with the delays turned
 * down, so what is verified is the shape of the strategy — when it retries,
 * when it refuses to, and where it stops — rather than the specific numbers.
 */

/** The production policy, with the waiting taken out. */
const FAST: ColdStartPolicy = {
  attemptTimeoutMs: 50,
  retryDelaysMs: [1, 2, 4],
  budgetMs: 10_000,
};

function stubSequence(replies: Array<{ status: number; body?: unknown } | "offline">) {
  let index = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      const reply = replies[Math.min(index, replies.length - 1)]!;
      index += 1;
      if (reply === "offline") throw new TypeError("Failed to fetch");
      return {
        ok: reply.status >= 200 && reply.status < 300,
        status: reply.status,
        statusText: "",
        json: async () => reply.body ?? null,
      } as Response;
    }),
  );
}

describe("cold starts", () => {
  it("keeps trying a read-only request while the instance wakes", async () => {
    stubSequence(["offline", "offline", { status: 200, body: fixtures.principals }]);

    await expect(listPrincipals({ coldStart: FAST })).resolves.toHaveLength(
      fixtures.principals.principals!.length,
    );
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(3);
  });

  it("retries a gateway status but not an application error", async () => {
    // While the instance spins up it is the host router that answers, not the
    // application. A 500 means something *did* handle the request.
    stubSequence([{ status: 503 }, { status: 200, body: fixtures.principals }]);
    await expect(listPrincipals({ coldStart: FAST })).resolves.toBeDefined();
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2);

    stubSequence([{ status: 500 }, { status: 200, body: fixtures.principals }]);
    await expect(listPrincipals({ coldStart: FAST })).rejects.toBeInstanceOf(ApiError);
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });

  it("gives up after a bounded number of attempts", async () => {
    stubSequence(["offline"]);

    await expect(listPrincipals({ coldStart: FAST })).rejects.toMatchObject({
      code: "network_error",
    });
    // One attempt, then one per configured delay. Never more, whatever happens.
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(FAST.retryDelaysMs.length + 1);
  });

  it("stops retrying once the wall-clock budget is spent", async () => {
    stubSequence(["offline"]);
    const policy: ColdStartPolicy = {
      ...FAST,
      retryDelaysMs: [40, 40, 40, 40, 40],
      budgetMs: 60,
    };

    await expect(listPrincipals({ coldStart: policy })).rejects.toBeInstanceOf(ApiError);
    // The delays alone would allow six attempts; the budget cuts it to two.
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2);
  });

  it("abandons an attempt that is never answered", async () => {
    // A held-open connection is the other face of a cold start, and it must not
    // be allowed to hang the page indefinitely.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: RequestInfo | URL, init?: RequestInit) =>
          new Promise((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("Aborted", "AbortError")),
            );
          }),
      ),
    );

    await expect(
      listPrincipals({ coldStart: { ...FAST, retryDelaysMs: [] } }),
    ).rejects.toMatchObject({ code: "network_error" });
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });

  it("reports whether the backend answered, without throwing", async () => {
    stubSequence(["offline"]);
    await expect(wakeBackend({ coldStart: { ...FAST, retryDelaysMs: [1] } })).resolves.toBe(
      false,
    );
    expect(isBackendReachable()).toBe(false);

    stubSequence([{ status: 200, body: fixtures.health }]);
    await expect(wakeBackend({ coldStart: FAST })).resolves.toBe(true);
    expect(isBackendReachable()).toBe(true);
  });

  it("counts a backend that answers with an error as awake", async () => {
    // Waiting cannot fix a broken deployment, and treating it as a cold start
    // would spend another ninety seconds discovering that.
    stubSequence([{ status: 500 }]);

    await expect(wakeBackend({ coldStart: FAST })).resolves.toBe(true);
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });

  it("does not retry a single request by default", async () => {
    stubSequence(["offline"]);

    await expect(getHealth()).rejects.toBeInstanceOf(ApiError);
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });
});

describe("requests that are never repeated", () => {
  it("sends a chat message exactly once even when the transport fails", async () => {
    stubSequence(["offline"]);

    await expect(
      sendChat({ message: "hi", identity: "support.agent", sessionId: null }),
    ).rejects.toMatchObject({ code: "network_error" });
    // A second attempt could persist a second turn and prepare a second action.
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });

  it("sends a confirmation exactly once even when the transport fails", async () => {
    stubSequence(["offline"]);

    await expect(
      confirmAction({
        actionId: "ACT-abc",
        decision: "approve",
        identity: "support.manager",
        sessionId: "SES-1",
        fingerprint: "fp-1",
      }),
    ).rejects.toMatchObject({ code: "network_error" });
    // The one request that changes the world. A lost response may still have
    // executed, so repeating it is never the client's call.
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
  });
});
