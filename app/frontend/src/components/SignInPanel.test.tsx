import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SignInPanel } from "./SignInPanel";

/**
 * The registration form's own validation.
 *
 * Two things it is responsible for, and only two — everything else about a
 * password is the backend's:
 *
 * - **The confirmation is checked here and sent nowhere.** It is not a
 *   credential; it exists so a mistyped password becomes a corrected form
 *   rather than an account nobody can sign in to. The API has no field for it
 *   and must never receive one.
 * - **A password the server is certain to refuse is refused here first.** The
 *   length rule mirrors `MIN_PASSWORD_LENGTH` in the backend, so the two
 *   cannot tell the user different things.
 */

// Either side of the boundary, and one comfortably past it. The literals are
// spelled out rather than generated so a reader can count them.
const AT_MINIMUM = "eightchr"; // exactly 8
const TOO_SHORT = "sevench"; // 7
const COMFORTABLE = "nine-char"; // 9

function renderRegistration() {
  const user = userEvent.setup();
  const onRegistered = vi.fn();

  render(
    <SignInPanel
      stage="signed-out"
      busy={false}
      error={null}
      onSignIn={vi.fn()}
      onSubmitMfaCode={vi.fn()}
      onRegistered={onRegistered}
      onDismissError={vi.fn()}
    />,
  );

  return { user, onRegistered };
}

async function openRegistration(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("tab", { name: /create account/i }));
}

async function fill(
  user: ReturnType<typeof userEvent.setup>,
  password: string,
  confirmation: string,
) {
  await user.type(screen.getByLabelText(/your name/i), "Ada Lovelace");
  await user.type(screen.getByLabelText(/^email$/i), "ada@example.com");
  await user.type(screen.getByLabelText(/^password$/i), password);
  await user.type(screen.getByLabelText(/confirm password/i), confirmation);
  await user.click(screen.getByRole("button", { name: /create account/i }));
  // The submit handler resolves a dynamic import before it reaches the API,
  // and `user.click` does not await that. Letting the microtask queue drain
  // here is what stops one test's request landing inside the next one.
  await act(async () => {
    await Promise.resolve();
  });
}

/** The registration request body, or null if none was sent. */
function registerBody(): Record<string, unknown> | null {
  const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } })
    .mock.calls;
  const call = calls.find((c) => String(c[0]).includes("/api/auth/register"));
  if (!call) return null;
  const init = call[1] as RequestInit | undefined;
  return init?.body ? JSON.parse(init.body as string) : null;
}

function stubRegister() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      status: 200,
      statusText: "",
      json: async () => ({
        status: "registration_received",
        message: "If that address is available, an account was created.",
        verification_token: "tok-123",
      }),
    })) as unknown as typeof fetch,
  );
}

describe("the registration form", () => {
  it("asks for the password twice", async () => {
    const { user } = renderRegistration();
    await openRegistration(user);

    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
    // Both are password fields, and both tell a password manager this is a new
    // credential rather than one to fill from storage.
    expect(screen.getByLabelText(/confirm password/i)).toHaveAttribute(
      "type",
      "password",
    );
    expect(screen.getByLabelText(/confirm password/i)).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
  });

  it("states the minimum the backend actually enforces", async () => {
    const { user } = renderRegistration();
    await openRegistration(user);
    expect(screen.getByText("At least 8 characters.")).toBeInTheDocument();
  });

  it("does not ask a returning user to confirm anything", async () => {
    renderRegistration();
    // Sign-in is the default tab.
    expect(screen.queryByLabelText(/confirm password/i)).toBeNull();
    expect(screen.queryByText(/at least \d+ characters/i)).toBeNull();
  });

  it("accepts a password at exactly the minimum length", async () => {
    stubRegister();
    const { user, onRegistered } = renderRegistration();
    await openRegistration(user);
    await fill(user, AT_MINIMUM, AT_MINIMUM);

    await waitFor(() => expect(onRegistered).toHaveBeenCalledTimes(1));
    expect(registerBody()).toMatchObject({
      email: "ada@example.com",
      password: AT_MINIMUM,
    });
  });

  it("accepts a password comfortably past the minimum", async () => {
    stubRegister();
    const { user, onRegistered } = renderRegistration();
    await openRegistration(user);
    await fill(user, COMFORTABLE, COMFORTABLE);

    await waitFor(() => expect(onRegistered).toHaveBeenCalledTimes(1));
    expect(registerBody()).toMatchObject({ password: COMFORTABLE });
  });

  it("refuses a password below the minimum, without asking the server", async () => {
    stubRegister();
    const { user, onRegistered } = renderRegistration();
    await openRegistration(user);
    await fill(user, TOO_SHORT, TOO_SHORT);

    expect(
      screen.getByText("Password must be at least 8 characters."),
    ).toBeInTheDocument();
    expect(registerBody()).toBeNull();
    expect(onRegistered).not.toHaveBeenCalled();
  });

  it("refuses a confirmation that does not match", async () => {
    stubRegister();
    const { user, onRegistered } = renderRegistration();
    await openRegistration(user);
    await fill(user, AT_MINIMUM, `${AT_MINIMUM}x`);

    expect(screen.getByText("Passwords do not match.")).toBeInTheDocument();
    expect(registerBody()).toBeNull();
    expect(onRegistered).not.toHaveBeenCalled();
  });

  it("never sends the confirmation as a credential of its own", async () => {
    stubRegister();
    const { user } = renderRegistration();
    await openRegistration(user);
    await fill(user, AT_MINIMUM, AT_MINIMUM);
    await waitFor(() => expect(registerBody()).not.toBeNull());

    const body = registerBody()!;
    // The API has no field for it, and inventing one would make a check that
    // belongs to this form look like part of the credential.
    expect(Object.keys(body).sort()).toEqual(
      ["display_name", "email", "password"].sort(),
    );
  });

  it("wires each error to the field it belongs to", async () => {
    stubRegister();
    const { user } = renderRegistration();
    await openRegistration(user);
    await fill(user, AT_MINIMUM, "different");

    // So a screen reader hears the problem while the offending control has
    // focus, rather than as a banner somewhere above the form.
    const confirmation = screen.getByLabelText(/confirm password/i);
    expect(confirmation).toHaveAttribute("aria-invalid", "true");
    const describedBy = confirmation.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent(
      "Passwords do not match.",
    );
  });

  it("clears a validation error once the field is edited", async () => {
    stubRegister();
    const { user } = renderRegistration();
    await openRegistration(user);
    await fill(user, AT_MINIMUM, "different");
    expect(screen.getByText("Passwords do not match.")).toBeInTheDocument();

    // Repeating the complaint on every keystroke is noise, not help.
    await user.type(screen.getByLabelText(/confirm password/i), "x");
    expect(screen.queryByText("Passwords do not match.")).toBeNull();
  });
});


/**
 * The public-demo panel.
 *
 * A hosted deployment running real authentication has no way in for a
 * visitor, and this is the way in: an ordinary sign-in with a published
 * account. What the panel owes the visitor is the truth before they act —
 * that the workspace is shared, the records synthetic, and the actions real
 * inside it — and what it owes the product is that it stays an ordinary
 * sign-in, with no channel for the credential except the request the visitor
 * asked for.
 */
describe("the public demo panel", () => {
  const DEMO_EMAIL = "support@demo.parcelpilot.example";
  const DEMO_PASSWORD = "published-demo-password";

  function renderPanel() {
    const user = userEvent.setup();
    const onSignIn = vi.fn();
    render(
      <SignInPanel
        stage="signed-out"
        busy={false}
        error={null}
        onSignIn={onSignIn}
        onSubmitMfaCode={vi.fn()}
        onRegistered={vi.fn()}
        onDismissError={vi.fn()}
      />,
    );
    return { user, onSignIn };
  }

  function publish(email?: string, password?: string) {
    vi.stubEnv("NEXT_PUBLIC_DEMO_EMAIL", email ?? "");
    vi.stubEnv("NEXT_PUBLIC_DEMO_PASSWORD", password ?? "");
  }

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("is absent when the deployment published no demo account", () => {
    // A self-hosted copy must not advertise credentials it does not have.
    publish();
    renderPanel();

    expect(screen.queryByText("Public demo")).toBeNull();
    expect(screen.queryByRole("button", { name: /sign in to the demo/i })).toBeNull();
  });

  it("says the workspace is shared before the visitor acts in it", () => {
    publish(DEMO_EMAIL, DEMO_PASSWORD);
    renderPanel();

    expect(screen.getByText("Public demo")).toBeInTheDocument();
    expect(screen.getByText(/everyone shares one workspace/i)).toBeInTheDocument();
    expect(
      screen.getByText(/visible to whoever visits next/i),
    ).toBeInTheDocument();
  });

  it("says the records are synthetic and the actions are not", () => {
    publish(DEMO_EMAIL, DEMO_PASSWORD);
    renderPanel();

    expect(screen.getByText(/synthetic accounts, orders, tickets/i)).toBeInTheDocument();
    expect(screen.getByText(/no real customer data/i)).toBeInTheDocument();
    // A demo that faked the confirmation gate would demonstrate nothing.
    expect(screen.getByText(/the confirmation gate holds/i)).toBeInTheDocument();
  });

  it("names the account being used", () => {
    publish(DEMO_EMAIL, DEMO_PASSWORD);
    renderPanel();

    expect(screen.getByText(DEMO_EMAIL)).toBeInTheDocument();
  });

  it("signs in through the ordinary path, with the published credentials", async () => {
    publish(DEMO_EMAIL, DEMO_PASSWORD);
    const { user, onSignIn } = renderPanel();

    await user.click(screen.getByRole("button", { name: /sign in to the demo/i }));

    // The same callback the typed form uses. No second endpoint, no minted
    // session, no role chosen by the browser.
    expect(onSignIn).toHaveBeenCalledTimes(1);
    expect(onSignIn).toHaveBeenCalledWith(DEMO_EMAIL, DEMO_PASSWORD);
  });

  it("puts the credential in no URL and no navigable attribute", () => {
    publish(DEMO_EMAIL, DEMO_PASSWORD);
    renderPanel();

    // The password is displayed, because a published demo credential has to be
    // readable to be usable. What it must never be is *carried* — a query
    // string, a link target or a form action would put it into history, the
    // Referer header and every access log along the way.
    for (const element of Array.from(document.querySelectorAll("*"))) {
      for (const attribute of Array.from(element.attributes)) {
        expect(attribute.value).not.toContain(DEMO_PASSWORD);
      }
    }
    expect(document.querySelectorAll("form[action]")).toHaveLength(0);
  });

  it("offers instructions rather than a button when no password was published", () => {
    publish(DEMO_EMAIL);
    renderPanel();

    expect(screen.getByText(DEMO_EMAIL)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign in to the demo/i })).toBeNull();
    expect(
      screen.getByText(/ask whoever runs this deployment/i),
    ).toBeInTheDocument();
  });

  it("stays out of the way of someone creating a real account", async () => {
    publish(DEMO_EMAIL, DEMO_PASSWORD);
    const { user } = renderPanel();
    await user.click(screen.getByRole("tab", { name: /create account/i }));

    expect(screen.queryByText("Public demo")).toBeNull();
  });
});
