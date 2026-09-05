"use client";

import { useState } from "react";

import { Button } from "./ui/Button";
import { Callout } from "./ui/Callout";
import { TextField } from "./ui/Field";
import { demoAccess } from "@/lib/demo";

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
  onSignIn,
  onSubmitMfaCode,
  onRegistered,
  onDismissError,
}: {
  stage: "signed-out" | "mfa-required";
  busy: boolean;
  error: string | null;
  onSignIn(email: string, password: string): void;
  onSubmitMfaCode(code: string): void;
  onRegistered(message: string, verificationToken: string | undefined, email: string): void;
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
      onRegistered(result.message, result.verification_token, email);
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
  const demo = demoAccess();

  return (
    <div className={styles.panel}>
      <h1 className={styles.title}>
        {registering ? "Create your account" : "Sign in"}
      </h1>

      {demo && !registering ? (
        <DemoAccess
          email={demo.email}
          password={demo.password}
          busy={busy}
          onSignIn={onSignIn}
        />
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
 * Rendered only when the deployment published a demo address. It is an
 * ordinary sign-in: the button fills nothing the form could not be filled with
 * by hand and posts to the same endpoint, so the session that comes back went
 * through the same password check, the same rate limiter, the same lockout and
 * the same workspace scoping as anyone else's.
 *
 * Everything it says is a fact a visitor needs *before* they act, not
 * marketing:
 *
 * - the workspace is **shared**, so what they do is visible to the next
 *   person, and what they find may have been done by the last one;
 * - the records are **synthetic** — the supplied ParcelPilot pack, no real
 *   customer anywhere in it;
 * - actions are **real inside it**, because a demo that faked the
 *   confirmation gate would be demonstrating nothing.
 *
 * The password is shown because a published demo credential has to be
 * reachable to be usable. It never travels in a URL, is never logged, and is
 * only ever sent in the body of the sign-in request the visitor asked for.
 */
function DemoAccess({
  email,
  password,
  busy,
  onSignIn,
}: {
  email: string;
  password: string | null;
  busy: boolean;
  onSignIn(email: string, password: string): void;
}) {
  return (
    <Callout tone="info" title="Public demo" className={styles.demo}>
      <p>
        This deployment has a shared demo workspace holding the sample
        ParcelPilot dataset — synthetic accounts, orders, tickets and policy
        documents. There is no real customer data in it.
      </p>
      <p>
        Everyone shares one workspace, so actions you confirm and audit entries
        you create are visible to whoever visits next. Inside it, everything is
        real: the assistant runs, the confirmation gate holds, and your role
        decides what you may do.
      </p>
      <dl className={styles.demoAccount}>
        <div>
          <dt>Email</dt>
          <dd>{email}</dd>
        </div>
        {password ? (
          <div>
            <dt>Password</dt>
            <dd>{password}</dd>
          </div>
        ) : null}
      </dl>
      {password ? (
        <Button
          type="button"
          variant="secondary"
          disabled={busy}
          onClick={() => onSignIn(email, password)}
        >
          {busy ? "Working…" : "Sign in to the demo"}
        </Button>
      ) : (
        <p className={styles.demoNote}>
          Ask whoever runs this deployment for the demo password, or sign in
          with your own account below.
        </p>
      )}
    </Callout>
  );
}
