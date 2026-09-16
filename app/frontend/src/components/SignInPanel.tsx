"use client";

import { useEffect, useState } from "react";

import { Button } from "./ui/Button";
import { Callout } from "./ui/Callout";
import { TextField } from "./ui/Field";

import styles from "./SignInPanel.module.css";

/**
 * The shortest password the backend will store.
 *
 * Mirrors `MIN_PASSWORD_LENGTH` in `app/backend/auth/passwords.py`, which is
 * the authority — this is a copy so the form can say so before a round trip,
 * and refuse locally rather than let someone submit a password the server is
 * certain to reject. If the backend's policy changes, this constant and the
 * hint below are the only two places the frontend has to follow it.
 */
const MIN_PASSWORD_LENGTH = 8;

/**
 * Sign in, register, and the second-factor challenge.
 *
 * What it holds to:
 *
 * - **It never says whether an account exists.** The backend answers
 *   registration and sign-in identically for known and unknown addresses, and
 *   this form renders the backend's message verbatim rather than interpreting
 *   it into something more "helpful" that would undo that.
 * - **It keeps no credential.** The password lives in component state until
 *   the request resolves and is never stored, logged, or put in a URL. The
 *   session that comes back is an `HttpOnly` cookie this code cannot read.
 * - **Autocomplete is spelled correctly** (`current-password` vs
 *   `new-password`), so password managers offer the right thing and people are
 *   not pushed toward reusing one they can remember.
 *
 * Registration hands its result *up* rather than swallowing it. That is the
 * fix for a dead end: the backend issues a verification link, refuses sign-in
 * until it is used, and — outside production, where there is no mail
 * transport — returns the token in the response so the flow can be completed.
 * Discarding it left a new user registered, unable to sign in, and told only
 * "Incorrect email address or password."
 */
export function SignInPanel({
  stage,
  busy,
  error,
  demoAvailable = false,
  onSignIn,
  onDemoSignIn,
  onSubmitMfaCode,
  onRegistered,
  onDismissError,
}: {
  stage: "signed-out" | "mfa-required";
  busy: boolean;
  error: string | null;
  /** From `/health`. The server decides whether there is a demo to offer. */
  demoAvailable?: boolean;
  onSignIn(email: string, password: string): void;
  onDemoSignIn?(): void;
  onSubmitMfaCode(code: string): void;
  onRegistered(message: string, verificationToken: string | undefined, email: string, emailSent?: boolean): void;
  onDismissError(): void;
}) {
  const [mode, setMode] = useState<"signin" | "register">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [code, setCode] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);

  /**
   * What is wrong with the password pair, if anything.
   *
   * Both messages are shown against the field they belong to rather than as a
   * banner, so a screen reader hears the problem while the offending control
   * has focus. The length message is worded exactly as the backend words it,
   * because the backend is what enforces it — a user who somehow gets past
   * this check should not be told two different things by two layers.
   */
  function validateRegistration(): boolean {
    const tooShort =
      password.length < MIN_PASSWORD_LENGTH
        ? `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`
        : null;
    // Compared exactly. Trimming or case-folding here would let someone
    // "confirm" a password they did not type, which is the whole point of the
    // second field.
    const mismatched =
      !tooShort && password !== confirmPassword ? "Passwords do not match." : null;

    setPasswordError(tooShort);
    setConfirmError(mismatched);
    return !tooShort && !mismatched;
  }

  async function submitRegistration(event: React.FormEvent) {
    event.preventDefault();
    onDismissError();
    if (!validateRegistration()) return;

    const { register } = await import("@/lib/auth-client");
    try {
      // The confirmation is deliberately absent: it is not a credential and
      // the API has no field for it. It exists so a mistyped password is
      // caught here rather than becoming an account nobody can sign in to.
      const result = await register({ email, password, displayName });
      // Neither copy of the password outlives the request that used it.
      setPassword("");
      setConfirmPassword("");
      onRegistered(result.message, result.verification_token, email, result.email_sent);
    } catch {
      // The hook surfaces the error; nothing useful to add here, and inventing
      // a message would risk contradicting the backend's careful wording.
    }
  }

  if (stage === "mfa-required") {
    return (
      <div className={styles.panel}>
        <h1 className={styles.title}>Two-factor authentication</h1>
        <p className={styles.lede}>
          Enter the six-digit code from your authenticator app. You are half
          signed in: the session exists but can do nothing until this is
          answered.
        </p>
        <form
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault();
            onSubmitMfaCode(code);
          }}
        >
          <TextField
            label="Authentication code"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            required
            autoFocus
          />
          {error ? (
            <Callout tone="fail" role="alert" title="Could not verify">
              {error}
            </Callout>
          ) : null}
          <Button
            type="submit"
            variant="primary"
            block
            disabled={busy}
            className={styles.submit}
          >
            {busy ? "Checking…" : "Verify"}
          </Button>
        </form>
      </div>
    );
  }

  const registering = mode === "register";

  return (
    <div className={styles.panel}>
      <h1 className={styles.title}>
        {registering ? "Create your account" : "Sign in"}
      </h1>

      {demoAvailable && !registering && onDemoSignIn ? (
        <DemoAccess busy={busy} onSignIn={onDemoSignIn} />
      ) : null}
      <p className={styles.lede}>
        {registering
          ? "You will name your first workspace next. A workspace holds one operation's accounts, orders, tickets and documents."
          : "Use the address your workspace was created with, or the one an invitation was sent to."}
      </p>

      <div className={styles.tabs} role="tablist" aria-label="Sign in or register">
        <button
          type="button"
          role="tab"
          aria-selected={!registering}
          className={!registering ? `${styles.tab} ${styles.tabActive}` : styles.tab}
          onClick={() => {
            setMode("signin");
            onDismissError();
            setPasswordError(null);
            setConfirmError(null);
          }}
        >
          Sign in
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={registering}
          className={registering ? `${styles.tab} ${styles.tabActive}` : styles.tab}
          onClick={() => {
            setMode("register");
            onDismissError();
            setPasswordError(null);
            setConfirmError(null);
          }}
        >
          Create account
        </button>
      </div>

      <form
        className={styles.form}
        onSubmit={
          registering
            ? submitRegistration
            : (event) => {
                event.preventDefault();
                onSignIn(email, password);
              }
        }
      >
        {registering ? (
          <TextField
            label="Your name"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            autoComplete="name"
            required
          />
        ) : null}

        <TextField
          label="Email"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          autoComplete="email"
          required
        />

        <TextField
          label="Password"
          type="password"
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
            // Clearing on edit rather than re-validating on every keystroke:
            // telling someone their password is too short while they are still
            // typing it is noise, not help.
            setPasswordError(null);
            setConfirmError(null);
          }}
          autoComplete={registering ? "new-password" : "current-password"}
          hint={
            registering ? `At least ${MIN_PASSWORD_LENGTH} characters.` : undefined
          }
          error={registering ? passwordError : null}
          required
        />

        {registering ? (
          <TextField
            label="Confirm password"
            type="password"
            value={confirmPassword}
            onChange={(event) => {
              setConfirmPassword(event.target.value);
              setConfirmError(null);
            }}
            autoComplete="new-password"
            error={confirmError}
            required
          />
        ) : null}

        {error ? (
          <Callout
            tone="fail"
            role="alert"
            title={registering ? "Could not create the account" : "Could not sign in"}
          >
            {error}
          </Callout>
        ) : null}

        <Button
          type="submit"
          variant="primary"
          block
          disabled={busy}
          className={styles.submit}
        >
          {busy ? "Working…" : registering ? "Create account" : "Sign in"}
        </Button>
      </form>

      <p className={styles.footnote}>
        Invited to an existing workspace? Open the invitation link you were
        sent, signed in as the address it was issued to.
      </p>
    </div>
  );
}

/**
 * The way in, for a visitor who has no account and no reason to make one.
 *
 * One button, and nothing to copy. The address and the password live in the
 * backend's configuration and are never sent here, so there is no credential
 * in this file, none in the compiled bundle, and none for a reader of the page
 * source to find. `POST /api/auth/demo-login` takes no body at all — the
 * server signs *itself* in through the ordinary login path and returns the
 * same HttpOnly cookie any other sign-in would.
 *
 * What replaced it is worth naming, because the old panel was not careless.
 * It published the credential because a published demo has to be reachable to
 * be usable, and it was honest about that. What it could not fix is that a
 * visitor still had to read a password off a page and hand it back — and that
 * a `NEXT_PUBLIC_*` value is baked into the bundle at build time, so the demo
 * broke silently whenever the deployment's password and the frontend's build
 * disagreed. Moving the credential behind the endpoint removes both.
 *
 * Everything it says is a fact a visitor needs *before* they act, not
 * marketing:
 *
 * - the workspace is **shared**, so what they do is visible to the next
 *   person, and what they find may have been done by the last one;
 * - the records are **synthetic** — the supplied assessment pack, no real
 *   customer anywhere in it;
 * - actions are **real inside it**, because a demo that faked the
 *   confirmation gate would be demonstrating nothing.
 *
 * The button owns its own pending label. A sleeping deployment builds the
 * database, ingests the dataset and indexes the documents before it answers,
 * and "Preparing demo…" is the difference between a wait and a dead button.
 */
function DemoAccess({
  busy,
  onSignIn,
}: {
  busy: boolean;
  onSignIn(): void;
}) {
  // Whether *this* button started the work the panel is busy with. Without it,
  // typing into the form below would put the demo button into its pending
  // state too, which would read as though the form had triggered the demo.
  const [requested, setRequested] = useState(false);
  useEffect(() => {
    if (!busy) setRequested(false);
  }, [busy]);
  const preparing = requested && busy;

  return (
    <Callout tone="info" title="Public demo" className={styles.demo}>
      <p>
        This deployment has a shared demo workspace holding the sample
        ASTRION dataset — synthetic accounts, orders, tickets and policy
        documents. There is no real customer data in it.
      </p>
      <p>
        Everyone shares one workspace, so actions you confirm and audit entries
        you create are visible to whoever visits next. Inside it, everything is
        real: the assistant runs, the confirmation gate holds, and your role
        decides what you may do.
      </p>
      <Button
        type="button"
        variant="primary"
        block
        disabled={busy}
        className={styles.demoButton}
        onClick={() => {
          setRequested(true);
          onSignIn();
        }}
      >
        {preparing ? "Preparing demo…" : "Sign in to the demo"}
      </Button>
      <p className={styles.demoNote}>
        No account needed. The first visit after a quiet period takes a few
        seconds while the demo data is prepared.
      </p>
    </Callout>
  );
}
