import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act, useState } from "react";
import { describe, expect, it, vi } from "vitest";

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
 * One button, and the visitor types nothing. The credential that used to be
 * printed here lives in the backend's configuration now, so what these tests
 * are really about is what is *absent*: no address to copy, no password on the
 * page, no `NEXT_PUBLIC_*` value baked into the bundle, and no argument on the
 * call that could name a different account.
 *
 * What the panel still owes the visitor is unchanged — the truth before they
 * act, that the workspace is shared, the records synthetic, and the actions
 * real inside it.
 */
describe("the public demo panel", () => {
  function renderPanel(
    props: {
      demoAvailable?: boolean;
      busy?: boolean;
      onDemoSignIn?: () => void;
    } = {},
  ) {
    const user = userEvent.setup();
    const onSignIn = vi.fn();
    const onDemoSignIn = props.onDemoSignIn ?? vi.fn();
    const view = render(
      <SignInPanel
        stage="signed-out"
        busy={props.busy ?? false}
        error={null}
        demoAvailable={props.demoAvailable ?? true}
        onSignIn={onSignIn}
        onDemoSignIn={onDemoSignIn}
        onSubmitMfaCode={vi.fn()}
        onRegistered={vi.fn()}
        onDismissError={vi.fn()}
      />,
    );
    return { user, onSignIn, onDemoSignIn, view };
  }

  const demoButton = () =>
    screen.getByRole("button", { name: /sign in to the demo|preparing demo/i });

  it("is absent when the deployment offers no demo", () => {
    // The server answers this on `/health`; a self-hosted copy must not
    // advertise a way in that its backend would refuse.
    renderPanel({ demoAvailable: false });

    expect(screen.queryByText("Public demo")).toBeNull();
    expect(
      screen.queryByRole("button", { name: /sign in to the demo/i }),
    ).toBeNull();
  });

  it("says the workspace is shared before the visitor acts in it", () => {
    renderPanel();

    expect(screen.getByText("Public demo")).toBeInTheDocument();
    expect(screen.getByText(/everyone shares one workspace/i)).toBeInTheDocument();
    expect(screen.getByText(/visible to whoever visits next/i)).toBeInTheDocument();
  });

  it("says the records are synthetic and the actions are not", () => {
    renderPanel();

    expect(screen.getByText(/synthetic accounts, orders, tickets/i)).toBeInTheDocument();
    expect(screen.getByText(/no real customer data/i)).toBeInTheDocument();
    // A demo that faked the confirmation gate would demonstrate nothing.
    expect(screen.getByText(/the confirmation gate holds/i)).toBeInTheDocument();
  });

  it("asks the visitor for nothing at all", async () => {
    const { user, onDemoSignIn, onSignIn } = renderPanel();

    await user.click(demoButton());

    // No arguments: there is no address and no password in this component to
    // pass, which is what keeps them out of the bundle.
    expect(onDemoSignIn).toHaveBeenCalledTimes(1);
    expect(onDemoSignIn).toHaveBeenCalledWith();
    // And it is not the typed form's callback wearing a different hat.
    expect(onSignIn).not.toHaveBeenCalled();
  });

  it("shows no credential of its own", () => {
    renderPanel();

    // Scoped to the demo panel: the ordinary form below it still has a password
    // field, and should. What must be empty is the demo path — not "the
    // password is not displayed", but that there is nothing here that could be
    // one, and nothing to type.
    const panel = screen.getByText("Public demo").parentElement!;
    expect(panel).toContainElement(demoButton());
    expect(panel.querySelectorAll("input")).toHaveLength(0);
    expect(panel.textContent).not.toMatch(/@/);
    expect(panel.textContent).not.toMatch(/password/i);
  });

  it("puts nothing in a URL or a navigable attribute", () => {
    renderPanel();

    // A query string, a link target or a form action would put whatever it
    // carried into history, the Referer header and every access log on the way.
    // There is nothing to carry now, and this is what keeps it that way.
    expect(document.querySelectorAll("form[action]")).toHaveLength(0);
    for (const anchor of Array.from(document.querySelectorAll("a[href]"))) {
      expect(anchor.getAttribute("href")).not.toMatch(/demo/i);
    }
  });

  it("says it is preparing while the backend builds the environment", async () => {
    // A sleeping deployment ingests a dataset and indexes six documents before
    // it answers. Without this the button looks dead for those seconds.
    function Harness() {
      const [busy, setBusy] = useState(false);
      return (
        <SignInPanel
          stage="signed-out"
          busy={busy}
          error={null}
          demoAvailable
          onSignIn={vi.fn()}
          onDemoSignIn={() => setBusy(true)}
          onSubmitMfaCode={vi.fn()}
          onRegistered={vi.fn()}
          onDismissError={vi.fn()}
        />
      );
    }

    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.getByRole("button", { name: /sign in to the demo/i })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: /sign in to the demo/i }));

    const button = screen.getByRole("button", { name: /preparing demo/i });
    expect(button).toBeDisabled();
  });

  it("does not claim to be preparing when something else is busy", () => {
    // `busy` is shared with the form below. A demo button that went pending
    // because somebody pressed Sign in would be reporting the wrong thing.
    renderPanel({ busy: true });

    expect(
      screen.getByRole("button", { name: /sign in to the demo/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /preparing demo/i })).toBeNull();
  });

  it("stays out of the way of someone creating a real account", async () => {
    const { user } = renderPanel();
    await user.click(screen.getByRole("tab", { name: /create account/i }));

    expect(screen.queryByText("Public demo")).toBeNull();
  });

  it("leaves the ordinary sign-in form in place", () => {
    // Real accounts still exist, and the demo is an addition rather than a
    // replacement for them.
    renderPanel();

    expect(screen.getByLabelText(/^email$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password$/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^sign in$/i })).toBeInTheDocument();
  });
});
