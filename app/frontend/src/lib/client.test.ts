/**
 * The API client's contract with the rest of the UI.
 *
 * The property that matters most: a failure must never come back looking like
 * a success. Every non-2xx response, and every transport fault, becomes a
 * thrown `ApiError` carrying the backend's own code — so no component can
 * accidentally render an error envelope as an answer.
 */

import { describe, expect, it, vi } from "vitest";

import { ApiError, confirmAction, listPrincipals, sendChat } from "./client";
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
