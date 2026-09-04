"use client";

import { useState } from "react";

import styles from "./SignInPanel.module.css";

/**
 * Sign in, register, and the second-factor challenge.
 *
 * Functional rather than finished — the product redesign is a later phase.
 * What it does hold to:
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
  onRegistered(message: string, verificationToken?: string): void;
  onDismissError(): void;
}) {
  const [mode, setMode] = useState<"signin" | "register">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  async function submitRegistration(event: React.FormEvent) {
    event.preventDefault();
    onDismissError();
    const { register } = await import("@/lib/auth-client");
    try {
      const result = await register({ email, password, displayName });
      setNotice(result.message);
      onRegistered(result.message, result.verification_token);
      setMode("signin");
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
          Enter the six-digit code from your authenticator app.
        </p>
        <form
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault();
            onSubmitMfaCode(code);
          }}
        >
          <label className={styles.label} htmlFor="mfa-code">
            Authentication code
          </label>
          <input
            id="mfa-code"
            className={styles.input}
            value={code}
            onChange={(event) => setCode(event.target.value)}
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            required
            autoFocus
          />
          {error ? <p className={styles.error}>{error}</p> : null}
          <button className={styles.primary} type="submit" disabled={busy}>
            {busy ? "Checking…" : "Verify"}
          </button>
        </form>
      </div>
    );
  }

  const registering = mode === "register";

  return (
    <div className={styles.panel}>
      <h1 className={styles.title}>ParcelPilot</h1>
      <p className={styles.lede}>
        Deterministic AI for logistics operations. AI investigates,
        deterministic rules decide, humans stay in control.
      </p>

      <div className={styles.tabs} role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={!registering}
          className={!registering ? styles.tabActive : styles.tab}
          onClick={() => {
            setMode("signin");
            onDismissError();
          }}
        >
          Sign in
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={registering}
          className={registering ? styles.tabActive : styles.tab}
          onClick={() => {
            setMode("register");
            onDismissError();
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
          <>
            <label className={styles.label} htmlFor="name">
              Your name
            </label>
            <input
              id="name"
              className={styles.input}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              autoComplete="name"
              required
            />
          </>
        ) : null}

        <label className={styles.label} htmlFor="email">
          Email
        </label>
        <input
          id="email"
          className={styles.input}
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          autoComplete="email"
          required
        />

        <label className={styles.label} htmlFor="password">
          Password
        </label>
        <input
          id="password"
          className={styles.input}
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete={registering ? "new-password" : "current-password"}
          required
        />
        {registering ? (
          <p className={styles.hint}>At least 12 characters.</p>
        ) : null}

        {error ? <p className={styles.error}>{error}</p> : null}
        {notice ? <p className={styles.notice}>{notice}</p> : null}

        <button className={styles.primary} type="submit" disabled={busy}>
          {busy ? "Working…" : registering ? "Create account" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
