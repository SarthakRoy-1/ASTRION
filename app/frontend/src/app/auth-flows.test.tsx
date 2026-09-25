import { readFileSync } from "node:fs";
import { join } from "node:path";

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import GetStartedPage from "./get-started/page";
import JoinPage from "./join/page";
import SignInPage from "./sign-in/page";
import { ProviderButtons } from "@/components/auth/ProviderButtons";
import type { VerificationStatus } from "@/lib/auth-types";
import health from "@/test/fixtures/health.json";
import { setTestRoute } from "@/test/next-navigation";
import { renderApp } from "@/test/render";

/**
 * Signing in with Google or GitHub, and proving an address with an emailed code.
 *
 * The backend here is a small stateful fake that answers the way
 * `app/backend/api/identity_routes.py` does. What is under test is the
 * frontend's half: which screen each answer leads to, what it lets a person
 * do there, and that it never needs — or shows — anything secret.
 */

const CODE = "482913";

function pending(overrides: Partial<VerificationStatus> = {}): VerificationStatus {
  return {
    purpose: "email_verification",
    email_hint: "s••••@example.com",
    needs_email: false,
    provider: null,
    code_expires_in_seconds: 600,
    code_ttl_seconds: 600,
    attempts_remaining: 5,
    resend_in_seconds: 0,
    sends_remaining: 4,
    expires_in_seconds: 3600,
    email_sent: true,
    ...overrides,
  };
}

interface Options {
  providers?: string[];
  /** The sign-in answers "verify your email" (a correct password, unproven address). */
  unverified?: boolean;
  /** `/me` reports a verification already in progress (a reload, or a provider return). */
  resumed?: VerificationStatus;
  verification?: Partial<VerificationStatus>;
  resend?: "ok" | "cooldown";
  /** Hold the verify request open until released. */
  holdVerify?: boolean;
}

function stubBackend(options: Options = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  let signedIn = false;
  let verification: VerificationStatus | null = options.resumed ?? null;
  let attempts = 5;
  let release: () => void = () => {};

  const json = (status: number, body: unknown) =>
    Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      statusText: "",
      json: async () => body,
    } as Response);
  const error = (status: number, code: string, message: string, details = {}) =>
    json(status, { error: { code, message, details } });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const body = init?.body ? JSON.parse(init.body as string) : null;
      calls.push({ url, method: init?.method ?? "GET", body });

      if (url.includes("/health")) {
        return json(200, {
          ...health,
          auth_mode: "session",
          demo_login_enabled: false,
          oauth_providers: options.providers ?? ["google", "github"],
        });
      }
      if (url.includes("/api/principals")) return json(200, { principals: [] });

      if (url.includes("/api/auth/me")) {
        if (signedIn) {
          return json(200, {
            user_id: "USR-1",
            display_name: "Sam",
            org_id: null,
            org_name: null,
            role: "member",
            permissions: [],
            account_scope: [],
            memberships: [],
            auth_mode: "session",
          });
        }
        return error(401, "unauthenticated", "Authentication is required.",
          verification ? { verification } : {});
      }
      if (url.includes("/api/workspaces")) {
        return json(200, { workspaces: [], active_workspace_id: null, needs_workspace: true });
      }

      if (url.includes("/api/auth/login")) {
        if (options.unverified) {
          verification = pending(options.verification);
          return error(401, "email_verification_required",
            "Verify your email address to finish signing in.", { verification });
        }
        signedIn = true;
        return json(200, { status: "authenticated", mfa_required: false, user_id: "USR-1", org_id: null });
      }

      if (url.includes("/api/auth/register")) {
        verification = pending(options.verification);
        return json(200, {
          status: "registration_received",
          message: "We've sent a verification code to s••••@example.com.",
          email_sent: true,
          verification,
        });
      }
      if (url.includes("/api/auth/verification/verify")) {
        if (options.holdVerify) await new Promise<void>((resolve) => (release = resolve));
        if (attempts <= 0) {
          return error(400, "otp_attempts_exceeded", "Too many incorrect attempts. Request a new code.");
        }
        if (body.code !== CODE) {
          attempts -= 1;
          return attempts <= 0
            ? error(400, "otp_attempts_exceeded", "Too many incorrect attempts. Request a new code.")
            : error(400, "otp_invalid", "That code is incorrect.", { attempts_remaining: attempts });
        }
        signedIn = true;
        verification = null;
        return json(200, { status: "authenticated", mfa_required: false, user_id: "USR-1", org_id: null });
      }
      if (url.includes("/api/auth/verification/resend")) {
        if (options.resend === "cooldown") {
          return error(429, "otp_resend_cooldown", "Please wait 42 seconds.", { retry_after_seconds: 42 });
        }
        attempts = 5;
        verification = pending({ resend_in_seconds: 60, sends_remaining: 3 });
        return json(200, { status: "code_sent", verification });
      }
      if (url.includes("/api/auth/verification/email")) {
        verification = pending({ purpose: "oauth_signup", provider: "github", email_hint: "o••••@example.com" });
        return json(200, { status: "code_sent", verification });
      }
      if (url.includes("/api/auth/verification/cancel")) {
        verification = null;
        return json(200, { status: "cancelled" });
      }
      throw new Error(`unexpected request: ${init?.method ?? "GET"} ${url}`);
    }),
  );

  return { calls, release: () => release() };
}

async function openSignIn(options: Options = {}) {
  const user = userEvent.setup();
  setTestRoute("/sign-in");
  const stub = stubBackend(options);
  renderApp(<SignInPage />);
  return { user, stub };
}

async function signInUnverified(options: Options = {}) {
  const { user, stub } = await openSignIn({ unverified: true, ...options });
  await user.type(await screen.findByLabelText(/^work email address$/i), "sam@example.com");
  await user.type(screen.getByLabelText(/^password$/i), "correct-horse-battery");
  await user.click(screen.getByRole("button", { name: /^sign in/i }));
  await screen.findByRole("heading", { name: /verify your email/i });
  return { user, stub };
}

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

// --- the sign-in page ---------------------------------------------------------

describe("the sign-in page's ways in", () => {
  it("offers Google and GitHub, then an 'or', then email and password", async () => {
    await openSignIn();
    const google = await screen.findByRole("button", { name: /continue with google/i });
    const github = screen.getByRole("button", { name: /continue with github/i });
    const separator = screen.getByRole("separator", { name: /or/i });
    const email = screen.getByLabelText(/^work email address$/i);
    const password = screen.getByLabelText(/^password$/i);

    // In reading order: providers, the divider, then the form.
    const order = [google, github, separator, email, password];
    for (let i = 1; i < order.length; i += 1) {
      expect(
        order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
    expect(google).toBeEnabled();
    expect(github).toBeEnabled();
    expect(screen.getByRole("link", { name: /create one/i })).toHaveAttribute("href", "/get-started");
  });

  it("shows a provider the server cannot honour as unavailable, not as broken", async () => {
    await openSignIn({ providers: ["github"] });
    const google = await screen.findByRole("button", { name: /continue with google/i });
    expect(google).toBeDisabled();
    expect(google).toHaveAccessibleDescription(/google sign-in isn.t set up/i);
    expect(screen.getByRole("button", { name: /continue with github/i })).toBeEnabled();
  });

  it("says why a provider sign-in came back, and clears it from the address bar", async () => {
    window.history.replaceState(null, "", "/sign-in?auth_error=oauth_cancelled");
    await openSignIn();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/sign-in was cancelled/i);
    expect(window.location.search).toBe("");
  });

  it("never renders text taken from the URL", async () => {
    window.history.replaceState(null, "", "/sign-in?auth_error=%3Cb%3Eyou%20have%20been%20hacked%3C%2Fb%3E");
    await openSignIn();
    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent(/hacked/i);
    expect(alert).toHaveTextContent(/didn.t complete/i);
  });
});

describe("the provider buttons", () => {
  it("go to the backend's start URL, once", async () => {
    const navigate = vi.fn();
    const user = userEvent.setup();
    render(<ProviderButtons available={["google", "github"]} navigate={navigate} />);

    await user.click(screen.getByRole("button", { name: /continue with github/i }));
    expect(navigate).toHaveBeenCalledWith(expect.stringMatching(/\/api\/auth\/oauth\/github\/start$/));

    // Connecting: labelled as such, and nothing can start a second flow.
    const busy = screen.getByRole("button", { name: /connecting to github/i });
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(busy).toBeDisabled();
    expect(screen.getByRole("button", { name: /continue with google/i })).toBeDisabled();
    fireEvent.click(busy);
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("are reachable and pressable from the keyboard", async () => {
    const navigate = vi.fn();
    const user = userEvent.setup();
    render(<ProviderButtons available={["google", "github"]} navigate={navigate} />);
    await user.tab();
    expect(screen.getByRole("button", { name: /continue with google/i })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(navigate).toHaveBeenCalledWith(expect.stringMatching(/\/api\/auth\/oauth\/google\/start$/));
  });

  it("carry no secret: the page never holds a client secret or token", () => {
    const source = readFileSync(
      join(process.cwd(), "src", "components", "auth", "ProviderButtons.tsx"),
      "utf8",
    );
    expect(source).not.toMatch(/client_secret|access_token|clientSecret/);
  });
});

// --- verifying an address --------------------------------------------------------

describe("verifying an email address with a code", () => {
  it("follows a correct password on an unproven address to the code screen", async () => {
    await signInUnverified();
    expect(screen.getByText("s••••@example.com")).toBeInTheDocument();
    // Not an error, and not the sign-in form.
    expect(screen.queryByText(/could not sign in/i)).toBeNull();
    expect(screen.queryByLabelText(/^password$/i)).toBeNull();

    const input = screen.getByLabelText(/^verification code$/i);
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("autocomplete", "one-time-code");
    expect(input).toHaveAttribute("inputmode", "numeric");
    expect(input).toHaveAccessibleDescription(/6-digit code.*expires in 10:00/i);
  });

  it("accepts a pasted code, spaces and all, and continues to onboarding", async () => {
    const { stub } = await signInUnverified();
    const input = screen.getByLabelText(/^verification code$/i);
    fireEvent.change(input, { target: { value: " 482 913 " } });
    expect(input).toHaveValue(CODE);

    expect(
      await screen.findByRole("heading", { name: /create your first workspace/i }),
    ).toBeInTheDocument();
    const verify = stub.calls.find((c) => c.url.includes("/verification/verify"));
    expect(verify?.body).toEqual({ code: CODE });
  });

  it("takes digits typed one at a time and ignores anything else", async () => {
    const { user } = await signInUnverified();
    const input = screen.getByLabelText(/^verification code$/i);
    await user.type(input, "48a2-9");
    expect(input).toHaveValue("4829");
    expect(screen.getByRole("button", { name: /^verify$/i })).toBeDisabled();
  });

  it("says a wrong code is wrong, and how many tries are left", async () => {
    const { user } = await signInUnverified();
    await user.type(screen.getByLabelText(/^verification code$/i), "111111");
    expect(await screen.findByText(/that code is incorrect\. 4 attempts left\./i)).toBeInTheDocument();
    const input = screen.getByLabelText(/^verification code$/i);
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveFocus();
  });

  it("stops accepting codes after too many wrong ones", async () => {
    const { user } = await signInUnverified();
    for (let i = 0; i < 5; i += 1) {
      await user.type(screen.getByLabelText(/^verification code$/i), "111111");
      await waitFor(() => expect(screen.getByLabelText(/^verification code$/i)).toHaveValue(""));
    }
    expect(await screen.findByText(/too many attempts/i)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/^verification code$/i), CODE);
    expect(screen.getByRole("button", { name: /^verify$/i })).toBeDisabled();
  });

  it("shows that it is working while the code is checked", async () => {
    const { user, stub } = await signInUnverified({ holdVerify: true });
    await user.type(screen.getByLabelText(/^verification code$/i), CODE);
    const button = await screen.findByRole("button", { name: /verifying/i });
    expect(button).toBeDisabled();
    expect(screen.getByLabelText(/^verification code$/i)).toBeDisabled();
    stub.release();
    await screen.findByRole("heading", { name: /create your first workspace/i });
  });

  it("marks an expired code and asks for a new one", async () => {
    await signInUnverified({ verification: { code_expires_in_seconds: 0 } });
    expect(screen.getByText(/code expired/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^verify$/i })).toBeDisabled();
  });

  it("says so when the email could not be sent", async () => {
    await signInUnverified({ verification: { email_sent: false } });
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn.t deliver the code/i);
  });

  it("counts down before a code can be resent", async () => {
    await signInUnverified({ verification: { resend_in_seconds: 60 } });
    expect(screen.getByRole("button", { name: /resend code in 1:00/i })).toBeDisabled();
  });

  it("resends a code and says the old one is dead", async () => {
    const { user, stub } = await signInUnverified();
    await user.click(screen.getByRole("button", { name: /^resend code$/i }));
    expect(await screen.findByText(/new code sent/i)).toBeInTheDocument();
    expect(screen.getByText(/previous code no longer works/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /resend code in/i })).toBeDisabled();
    expect(stub.calls.filter((c) => c.url.includes("/verification/resend"))).toHaveLength(1);
  });

  it("honours the server's cooldown when it refuses a resend", async () => {
    const { user } = await signInUnverified({ resend: "cooldown" });
    await user.click(screen.getByRole("button", { name: /^resend code$/i }));
    expect(await screen.findByRole("button", { name: /resend code in 0:4[12]/i })).toBeDisabled();
  });

  it("can be left, back to the sign-in form", async () => {
    const { user, stub } = await signInUnverified();
    await user.click(screen.getByRole("button", { name: /back to sign in/i }));
    expect(await screen.findByLabelText(/^work email address$/i)).toBeInTheDocument();
    expect(stub.calls.some((c) => c.url.includes("/verification/cancel"))).toBe(true);
  });

  it("returns to the code screen after a reload", async () => {
    await openSignIn({ resumed: pending() });
    expect(await screen.findByRole("heading", { name: /verify your email/i })).toBeInTheDocument();
    // Still the public page around it.
    expect(screen.getByRole("heading", { level: 1, name: /welcome back to astrion/i })).toBeInTheDocument();
  });

  it("never sends an email address to the verify endpoint", async () => {
    const { user, stub } = await signInUnverified();
    await user.type(screen.getByLabelText(/^verification code$/i), CODE);
    await screen.findByRole("heading", { name: /create your first workspace/i });
    const verify = stub.calls.find((c) => c.url.includes("/verification/verify"));
    expect(Object.keys(verify?.body as object)).toEqual(["code"]);
  });
});

describe("registering with an email address", () => {
  it("goes from the form to the code, and from the code to onboarding", async () => {
    const user = userEvent.setup();
    setTestRoute("/get-started");
    const stub = stubBackend();
    renderApp(<GetStartedPage />);

    await screen.findByLabelText(/your name/i);
    await user.type(screen.getByLabelText(/your name/i), "Sam");
    await user.type(screen.getByLabelText(/^work email address$/i), "sam@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "correct-horse-battery");
    await user.type(screen.getByLabelText(/confirm password/i), "correct-horse-battery");
    await user.click(screen.getByRole("button", { name: /^create account$/i }));

    // The code screen wears the same scene: the page's headline is the h1.
    expect(await screen.findByRole("heading", { level: 2, name: /verify your email/i })).toBeInTheDocument();
    expect(screen.getByText("s••••@example.com")).toBeInTheDocument();
    // The response carried no code, and the page shows none.
    expect(document.body.textContent).not.toContain(CODE);

    await user.type(screen.getByLabelText(/^verification code$/i), CODE);
    expect(
      await screen.findByRole("heading", { name: /create your first workspace/i }),
    ).toBeInTheDocument();
    expect(stub.calls.some((c) => c.url.includes("/api/auth/register"))).toBe(true);
  });
});

/** The visible surface of the sign-in card that `/sign-in` and `/get-started` share. */
function authSurface() {
  const google = screen.getByRole("button", { name: /continue with google/i });
  const github = screen.getByRole("button", { name: /continue with github/i });
  const separator = screen.getByRole("separator", { name: /or/i });
  return {
    google,
    github,
    separator,
    email: screen.getByLabelText(/^work email address$/i),
    password: screen.getByLabelText(/^password$/i),
    footer: screen.getByRole("list", { name: /site information/i }),
    headline: screen.getByRole("heading", { level: 1 }).textContent,
  };
}

describe("/get-started", () => {
  it("offers the same provider surface as /sign-in", async () => {
    setTestRoute("/sign-in");
    stubBackend({ providers: ["google", "github"] });
    const signIn = renderApp(<SignInPage />);
    await screen.findByRole("button", { name: /continue with google/i });
    const onSignIn = authSurface();
    const signInFooter = onSignIn.footer.textContent;
    signIn.unmount();

    setTestRoute("/get-started");
    stubBackend({ providers: ["google", "github"] });
    renderApp(<GetStartedPage />);
    await screen.findByLabelText(/your name/i);
    const onGetStarted = authSurface();

    expect(onGetStarted.google).toBeEnabled();
    expect(onGetStarted.github).toBeEnabled();
    expect(onGetStarted.separator).toBeInTheDocument();
    expect(onGetStarted.email).toBeInTheDocument();
    // Same scene — footer, skip link, header — with only the hero's wording
    // following the route.
    expect(onSignIn.headline).toBe("Welcome Back to Astrion");
    expect(onGetStarted.headline).toBe("Welcome to Astrion");
    expect(screen.getByRole("navigation", { name: /primary|main|site/i })).toBeInTheDocument();
    // Not the old split-screen registration page.
    expect(screen.queryByText(/grounded operations intelligence/i)).toBeNull();
    expect(screen.queryByText(/build what.s next/i)).toBeNull();
    expect(onGetStarted.footer.textContent).toBe(signInFooter);
    expect(screen.getByRole("link", { name: /skip to main content/i })).toBeInTheDocument();
  });

  it("reads providers, the divider, then the account form, in that order", async () => {
    setTestRoute("/get-started");
    stubBackend({ providers: ["google", "github"] });
    renderApp(<GetStartedPage />);
    await screen.findByLabelText(/your name/i);
    const { google, github, separator, email, password } = authSurface();

    const order = [google, github, separator, screen.getByLabelText(/your name/i), email, password];
    for (let i = 1; i < order.length; i += 1) {
      expect(
        order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
    // Account creation, not the sign-in form, and no tabs to switch it.
    expect(screen.getByRole("heading", { level: 2, name: /create your account/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).toBeNull();
    const switchLine = screen.getByText(/already have an account/i);
    expect(within(switchLine).getByRole("link", { name: /^sign in$/i })).toHaveAttribute("href", "/sign-in");
  });

  it("shows an unconfigured provider as unavailable, as /sign-in does", async () => {
    setTestRoute("/get-started");
    stubBackend({ providers: ["github"] });
    renderApp(<GetStartedPage />);
    const google = await screen.findByRole("button", { name: /continue with google/i });
    expect(google).toBeDisabled();
    expect(google).toHaveAccessibleDescription(/google sign-in isn.t set up/i);
    expect(screen.getByRole("button", { name: /continue with github/i })).toBeEnabled();
  });

  it("says why a provider sign-in came back, and clears it from the address bar", async () => {
    window.history.replaceState(null, "", "/get-started?auth_error=oauth_cancelled");
    setTestRoute("/get-started");
    stubBackend();
    renderApp(<GetStartedPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/sign-in was cancelled/i);
    expect(window.location.search).toBe("");
  });

  it("keeps the registration validation", async () => {
    const user = userEvent.setup();
    setTestRoute("/get-started");
    const stub = stubBackend();
    renderApp(<GetStartedPage />);

    await user.type(await screen.findByLabelText(/your name/i), "Sam");
    await user.type(screen.getByLabelText(/^work email address$/i), "sam@example.com");
    await user.type(screen.getByLabelText(/^password$/i), "short");
    await user.type(screen.getByLabelText(/confirm password/i), "short");
    await user.click(screen.getByRole("button", { name: /^create account$/i }));

    expect(await screen.findByText("Password must be at least 8 characters.")).toBeInTheDocument();
    expect(stub.calls.some((c) => c.url.includes("/api/auth/register"))).toBe(false);
  });
});

// --- the invitation page --------------------------------------------------------
//
// `/join` is reachable only once signed in. `verify-email` is a stage in which
// someone is NOT signed in — they hold a verification, not a session — so it
// must not be mistaken for one.

describe("an invitation link", () => {
  it("shows the code screen, not the invitation, to someone still verifying an address", async () => {
    setTestRoute("/join?token=inv-abc");
    stubBackend({ resumed: pending() });
    renderApp(<JoinPage />);

    expect(await screen.findByRole("heading", { name: /verify your email/i })).toBeInTheDocument();
    // The invitation page claims a session and offers to use it; neither is true yet.
    expect(screen.queryByRole("heading", { name: /accept your invitation/i })).toBeNull();
    expect(screen.queryByText(/you are signed in as/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /accept invitation/i })).toBeNull();
  });

  it("returns to the invitation once the address is verified", async () => {
    const user = userEvent.setup();
    setTestRoute("/join?token=inv-abc");
    stubBackend({ resumed: pending() });
    renderApp(<JoinPage />);

    await user.type(await screen.findByLabelText(/^verification code$/i), CODE);
    expect(
      await screen.findByRole("heading", { name: /accept your invitation/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/you are signed in as/i)).toBeInTheDocument();
  });

  it("still shows the sign-in page to someone who is simply signed out", async () => {
    setTestRoute("/join?token=inv-abc");
    stubBackend();
    renderApp(<JoinPage />);

    expect(await screen.findByLabelText(/^work email address$/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /accept your invitation/i })).toBeNull();
  });
});

describe("a GitHub sign-in with no verified address", () => {
  it("asks for an address, then for its code", async () => {
    const user = userEvent.setup();
    await openSignIn({
      resumed: pending({
        purpose: "oauth_signup",
        provider: "github",
        needs_email: true,
        email_hint: null,
        code_expires_in_seconds: null,
      }),
    });
    expect(await screen.findByRole("heading", { name: /add your email/i })).toBeInTheDocument();
    expect(screen.getByText(/github didn.t share a verified email/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/^work email address$/i), "octo@example.com");
    await user.click(screen.getByRole("button", { name: /send code/i }));

    expect(await screen.findByRole("heading", { name: /verify your email/i })).toBeInTheDocument();
    expect(screen.getByText("o••••@example.com")).toBeInTheDocument();
    // Offered a way to correct a mistyped address.
    expect(screen.getByRole("button", { name: /use a different email/i })).toBeInTheDocument();
  });
});

// --- the headline --------------------------------------------------------------

describe("the sign-in headline", () => {
  const css = readFileSync(
    join(process.cwd(), "src", "components", "auth", "SignInScene.module.css"),
    "utf8",
  );
  const rule = (selector: string) => {
    const match = css.match(new RegExp(`\\n${selector.replace(".", "\\.")} \\{([^}]*)\\}`));
    return match?.[1] ?? "";
  };

  it("sits in its own layer above the glass card", () => {
    const headline = rule(".headline");
    const card = rule(".card");
    const z = (block: string) => Number(block.match(/z-index:\s*(\d+)/)?.[1] ?? 0);
    expect(z(headline)).toBeGreaterThan(z(card));
    // In the flow beside the card, not positioned underneath it.
    expect(headline).not.toMatch(/position:\s*absolute/);
  });

  it("is outside the card, so no backdrop filter can reach it", async () => {
    await openSignIn();
    const heading = await screen.findByRole("heading", { level: 1, name: /welcome back to astrion/i });
    const form = screen.getByRole("heading", { level: 2, name: /^sign in$/i });
    const card = form.closest("div[class*='card']");
    expect(card).not.toBeNull();
    expect(within(card as HTMLElement).queryByRole("heading", { level: 1 })).toBeNull();
    expect(card?.contains(heading)).toBe(false);
  });
});
