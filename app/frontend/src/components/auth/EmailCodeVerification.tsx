"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { TextField } from "@/components/ui/Field";
import {
  chooseVerificationEmail,
  resendVerificationCode,
  submitVerificationCode,
} from "@/lib/auth-client";
import type { LoginResult, VerificationStatus } from "@/lib/auth-types";
import { ApiError } from "@/lib/client";

import panel from "../SignInPanel.module.css";
import styles from "./EmailCodeVerification.module.css";

const CODE_LENGTH = 6;
const PROVIDER_LABELS = { google: "Google", github: "GitHub" } as const;

type Problem =
  | { kind: "invalid"; attemptsRemaining: number }
  | { kind: "expired" }
  | { kind: "attempts" }
  | { kind: "limit"; message: string }
  | { kind: "gone" }
  | { kind: "other"; message: string };

type Busy = "verifying" | "sending" | null;

/** `125` -> `2:05`. */
function clock(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function digitsOnly(value: string): string {
  return value.replace(/\D/g, "").slice(0, CODE_LENGTH);
}

/**
 * "Verify your email": the one-time code step.
 *
 * Reached three ways, all ending here: registering, signing in with the right
 * password to an address never proven, and a GitHub/Google sign-in whose
 * provider supplied no verified address (which first asks for one).
 *
 * What it holds to:
 *
 * - **It never sees the code except as typed.** The server emails it and
 *   never returns it; this component only forwards what the person enters.
 * - **It never names the full address.** The server sends `s••••@example.com`
 *   and that is all there is to show.
 * - **One real input.** The six boxes are drawn *behind* a single
 *   `<input autocomplete="one-time-code">`, so paste, autofill from the
 *   keyboard's suggestion bar, a screen reader and the arrow keys all meet an
 *   ordinary text field rather than six fragments of one.
 * - **The server decides.** Countdowns start from what the server says and
 *   are a courtesy; resend limits, expiry and attempts are enforced there.
 */
export function EmailCodeVerification({
  verification,
  appearance = "card",
  onVerified,
  onCancel,
}: {
  verification: VerificationStatus;
  appearance?: "card" | "glass";
  onVerified(result: LoginResult): void | Promise<void>;
  onCancel(): void | Promise<void>;
}) {
  const glass = appearance === "glass";
  const [status, setStatus] = useState(verification);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState<Busy>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [resent, setResent] = useState(false);
  const [changingEmail, setChangingEmail] = useState(verification.needs_email);
  const [email, setEmail] = useState("");
  const [focused, setFocused] = useState(false);

  // Deadlines as absolute times, so a slow tick never stretches them.
  const [now, setNow] = useState(() => Date.now());
  const [codeExpiresAt, setCodeExpiresAt] = useState<number | null>(null);
  const [resendAt, setResendAt] = useState(0);

  const inputRef = useRef<HTMLInputElement>(null);
  const lastSubmitted = useRef<string | null>(null);
  const codeId = useId();
  const hintId = useId();

  const adopt = useCallback((next: VerificationStatus) => {
    const at = Date.now();
    setStatus(next);
    setNow(at);
    setCodeExpiresAt(
      next.code_expires_in_seconds == null ? null : at + next.code_expires_in_seconds * 1000,
    );
    setResendAt(at + next.resend_in_seconds * 1000);
  }, []);

  useEffect(() => adopt(verification), [adopt, verification]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const codeSecondsLeft = codeExpiresAt == null ? null : (codeExpiresAt - now) / 1000;
  const resendSecondsLeft = Math.max(0, (resendAt - now) / 1000);
  const expired =
    problem?.kind === "expired" ||
    status.code_expires_in_seconds == null ||
    (codeSecondsLeft !== null && codeSecondsLeft <= 0);
  const exhausted = problem?.kind === "attempts";
  const gone = problem?.kind === "gone";
  const deliveryFailed = status.email_sent === false;
  const canVerify =
    !busy && !expired && !exhausted && !gone && code.length === CODE_LENGTH;
  const canResend =
    !busy && !gone && resendSecondsLeft <= 0 && status.sends_remaining > 0;

  const failWith = useCallback((cause: unknown) => {
    if (!(cause instanceof ApiError)) {
      setProblem({ kind: "other", message: String(cause) });
      return;
    }
    const retry = Number((cause.details as { retry_after_seconds?: number }).retry_after_seconds ?? 0);
    switch (cause.code) {
      case "otp_invalid":
        setProblem({
          kind: "invalid",
          attemptsRemaining: Number(
            (cause.details as { attempts_remaining?: number }).attempts_remaining ?? 0,
          ),
        });
        return;
      case "otp_expired":
        setProblem({ kind: "expired" });
        return;
      case "otp_attempts_exceeded":
        setProblem({ kind: "attempts" });
        return;
      case "verification_expired":
        setProblem({ kind: "gone" });
        return;
      case "otp_resend_cooldown":
        setResendAt(Date.now() + retry * 1000);
        setProblem(null);
        return;
      case "otp_send_limit":
        setResendAt(Date.now() + retry * 1000);
        setProblem({ kind: "limit", message: cause.message });
        return;
      default:
        setProblem({ kind: "other", message: cause.message });
    }
  }, []);

  const verify = useCallback(
    async (value: string) => {
      if (busy || value.length !== CODE_LENGTH) return;
      lastSubmitted.current = value;
      setBusy("verifying");
      setProblem(null);
      try {
        const result = await submitVerificationCode(value);
        await onVerified(result);
      } catch (cause) {
        failWith(cause);
        setCode("");
        lastSubmitted.current = null;
        inputRef.current?.focus();
      } finally {
        setBusy(null);
      }
    },
    [busy, failWith, onVerified],
  );

  // A complete code — typed, pasted or autofilled — is submitted at once, but
  // only once: a refused code is cleared rather than re-sent.
  useEffect(() => {
    if (code.length === CODE_LENGTH && lastSubmitted.current !== code && canVerify) {
      void verify(code);
    }
  }, [canVerify, code, verify]);

  async function resend() {
    setBusy("sending");
    setProblem(null);
    setResent(false);
    try {
      const result = await resendVerificationCode();
      adopt(result.verification);
      setResent(result.status === "code_sent");
      setCode("");
      lastSubmitted.current = null;
      inputRef.current?.focus();
    } catch (cause) {
      failWith(cause);
    } finally {
      setBusy(null);
    }
  }

  async function submitEmail(event: React.FormEvent) {
    event.preventDefault();
    setBusy("sending");
    setProblem(null);
    try {
      const result = await chooseVerificationEmail(email);
      adopt(result.verification);
      setChangingEmail(false);
      setCode("");
      lastSubmitted.current = null;
    } catch (cause) {
      failWith(cause);
    } finally {
      setBusy(null);
    }
  }

  const Title = glass ? "h2" : "h1";
  const provider = status.provider ? PROVIDER_LABELS[status.provider] : null;
  const panelClass = glass ? panel.glass : panel.panel;

  if (gone) {
    return (
      <div className={panelClass}>
        <Title className={panel.title}>Verification expired</Title>
        <Callout tone="caution" role="alert" title="Start again" className={styles.block}>
          This verification is no longer active. Sign in again and a new code
          will be sent.
        </Callout>
        <div className={styles.actions}>
          <Button variant="primary" block className={panel.submit} onClick={() => void onCancel()}>
            Back to sign in
          </Button>
        </div>
      </div>
    );
  }

  if (changingEmail) {
    return (
      <div className={panelClass}>
        <Title className={panel.title}>Add your email</Title>
        <p className={panel.lede}>
          {provider
            ? `${provider} didn’t share a verified email address with us. `
            : ""}
          Enter the address you want to use with Astrion and we’ll send a code
          to confirm it’s yours.
        </p>
        <form className={panel.form} onSubmit={submitEmail}>
          <TextField
            label="Work email address"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="email"
            required
            autoFocus
          />
          {problem ? <ProblemCallout problem={problem} /> : null}
          <Button
            type="submit"
            variant="primary"
            block
            disabled={busy !== null}
            className={panel.submit}
          >
            {busy === "sending" ? "Sending code…" : "Send code"}
          </Button>
        </form>
        <p className={styles.footer}>
          <button type="button" className={styles.link} onClick={() => void onCancel()}>
            Use a different sign-in method
          </button>
        </p>
      </div>
    );
  }

  return (
    <div className={panelClass}>
      <Title className={panel.title}>Verify your email</Title>
      <p className={panel.lede}>
        We’ve sent a verification code to{" "}
        <span className={styles.address}>{status.email_hint}</span>.
      </p>

      {deliveryFailed ? (
        <Callout tone="fail" role="alert" title="The email didn’t send" className={styles.block}>
          We couldn’t deliver the code just now. Wait a moment, then send a new
          one.
        </Callout>
      ) : null}
      {resent && !deliveryFailed ? (
        <Callout tone="ok" role="status" title="New code sent" className={styles.block}>
          Your previous code no longer works.
        </Callout>
      ) : null}

      <form
        className={panel.form}
        onSubmit={(event) => {
          event.preventDefault();
          void verify(code);
        }}
        noValidate
      >
        <div className={styles.field}>
          <label htmlFor={codeId} className={styles.label}>
            Verification code
          </label>
          <div
            className={styles.slots}
            data-invalid={problem?.kind === "invalid" || undefined}
            data-disabled={expired || exhausted || undefined}
          >
            {Array.from({ length: CODE_LENGTH }, (_, index) => {
              const active =
                focused &&
                (index === code.length ||
                  (code.length === CODE_LENGTH && index === CODE_LENGTH - 1));
              return (
                <span
                  key={index}
                  className={styles.slot}
                  data-active={active || undefined}
                  data-filled={index < code.length || undefined}
                  aria-hidden="true"
                >
                  {code[index] ?? ""}
                </span>
              );
            })}
            <input
              ref={inputRef}
              id={codeId}
              className={styles.input}
              value={code}
              onChange={(event) => {
                setCode(digitsOnly(event.target.value));
                if (problem?.kind === "invalid" || problem?.kind === "other") setProblem(null);
              }}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]*"
              maxLength={CODE_LENGTH + 2}
              aria-describedby={hintId}
              aria-invalid={problem?.kind === "invalid" || undefined}
              disabled={busy === "verifying"}
              autoFocus
              spellCheck={false}
            />
          </div>
          <p id={hintId} className={styles.hint}>
            {problem?.kind === "invalid" ? (
              <span className={styles.error} role="alert">
                That code is incorrect.{" "}
                {problem.attemptsRemaining === 1
                  ? "1 attempt left."
                  : `${problem.attemptsRemaining} attempts left.`}
              </span>
            ) : expired ? (
              "This code has expired."
            ) : codeSecondsLeft !== null ? (
              `Enter the 6-digit code. It expires in ${clock(codeSecondsLeft)}.`
            ) : (
              "Enter the 6-digit code."
            )}
          </p>
        </div>

        {expired && !exhausted ? (
          <Callout tone="caution" role="alert" title="Code expired">
            Send a new code to continue.
          </Callout>
        ) : null}
        {problem && problem.kind !== "invalid" && problem.kind !== "expired" ? (
          <ProblemCallout problem={problem} />
        ) : null}

        <Button
          type="submit"
          variant="primary"
          block
          disabled={!canVerify}
          className={panel.submit}
        >
          {busy === "verifying" ? "Verifying…" : "Verify"}
        </Button>
      </form>

      <div className={styles.resend}>
        <span>Didn’t get it?</span>{" "}
        {status.sends_remaining <= 0 && resendSecondsLeft <= 0 ? (
          <span className={styles.muted}>No more codes can be sent right now.</span>
        ) : (
          <button
            type="button"
            className={styles.link}
            onClick={() => void resend()}
            disabled={!canResend}
            aria-live="polite"
          >
            {busy === "sending"
              ? "Sending…"
              : resendSecondsLeft > 0
                ? `Resend code in ${clock(resendSecondsLeft)}`
                : "Resend code"}
          </button>
        )}
      </div>

      <p className={styles.footer}>
        Check your spam folder too. The code works once.{" "}
        {status.purpose === "oauth_signup" ? (
          <button type="button" className={styles.link} onClick={() => setChangingEmail(true)}>
            Use a different email
          </button>
        ) : (
          <button type="button" className={styles.link} onClick={() => void onCancel()}>
            Back to sign in
          </button>
        )}
      </p>
    </div>
  );
}

function ProblemCallout({ problem }: { problem: Problem }) {
  switch (problem.kind) {
    case "attempts":
      return (
        <Callout tone="fail" role="alert" title="Too many attempts">
          That code can’t be used any more. Send a new code to try again.
        </Callout>
      );
    case "limit":
      return (
        <Callout tone="caution" role="alert" title="Please wait">
          {problem.message}
        </Callout>
      );
    case "other":
      return (
        <Callout tone="fail" role="alert" title="Something went wrong">
          {problem.message}
        </Callout>
      );
    default:
      return null;
  }
}
