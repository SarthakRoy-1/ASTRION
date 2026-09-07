"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { ApiError } from "@/lib/client";
import { resendVerification, verifyEmail, type ResendState } from "@/lib/auth-client";

import styles from "./VerifyEmailPrompt.module.css";

type State = "issued" | "verifying" | "verified" | "failed";

/**
 * The step between registering and signing in.
 *
 * Three cases the frontend handles honestly, decided by what the server says:
 *
 * 1. **Email was sent** (`emailSent === true`): tell the user to check their
 *    inbox. Show a resend button that counts down until the next send is
 *    allowed, with the send count so they know how many are left.
 *
 * 2. **Token came back, email was not sent** (non-production, no mail
 *    provider): show the link. This path exists so developers can test the
 *    verification flow without configuring Resend.
 *
 * 3. **Neither** (impossible to reach in a correctly configured production
 *    deployment): fall-through notice that says who to contact.
 *
 * Rate-limiting is server-side (30 s / 3 sends / 24 h cooldown). The
 * countdown timer is derived from the server's `seconds_until_allowed` and
 * runs client-side only as a UX aid — the server enforces independently.
 */
export function VerifyEmailPrompt({
  email,
  message,
  emailSent,
  verificationToken,
  initialResendState,
  onDone,
}: {
  email: string;
  message: string;
  emailSent: boolean;
  verificationToken?: string;
  initialResendState?: ResendState;
  onDone(): void;
}) {
  const [state, setState] = useState<State>("issued");
  const [error, setError] = useState<string | null>(null);

  const [resendState, setResendState] = useState<ResendState>(
    initialResendState ?? {
      can_resend: false,
      seconds_until_allowed: emailSent ? 30 : 0,
      sends_used: emailSent ? 1 : 0,
      in_cooldown: false,
    },
  );
  const [countdown, setCountdown] = useState(
    initialResendState?.seconds_until_allowed ??
      (emailSent ? 30 : 0),
  );
  const [resending, setResending] = useState(false);
  const [resendSuccess, setResendSuccess] = useState(false);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const startCountdown = useCallback((seconds: number) => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    setCountdown(seconds);
    if (seconds <= 0) return;
    intervalRef.current = setInterval(() => {
      setCountdown((prev) => {
        if (prev <= 1) {
          clearInterval(intervalRef.current!);
          setResendState((s) => ({ ...s, can_resend: true, seconds_until_allowed: 0 }));
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
  }, []);

  useEffect(() => {
    startCountdown(
      initialResendState?.seconds_until_allowed ?? (emailSent ? 30 : 0),
    );
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
    // Run once on mount only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function verify() {
    if (!verificationToken) return;
    setState("verifying");
    setError(null);
    try {
      await verifyEmail(verificationToken);
      setState("verified");
    } catch (cause) {
      setState("failed");
      setError(cause instanceof ApiError ? cause.message : String(cause));
    }
  }

  async function handleResend() {
    setResending(true);
    setError(null);
    setResendSuccess(false);
    try {
      const result = await resendVerification(email);
      setResendState(result.resend_state);
      startCountdown(result.resend_state.seconds_until_allowed);
      setResendSuccess(result.email_sent);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setResending(false);
    }
  }

  if (state === "verified") {
    return (
      <div className={styles.panel}>
        <h1 className={styles.title}>Address verified</h1>
        <p className={styles.lede}>
          <span className={styles.address}>{email}</span> is confirmed. Sign in
          to create your first workspace.
        </p>
        <div className={styles.section}>
          <Button variant="primary" block onClick={onDone}>
            Continue to sign in
          </Button>
        </div>
      </div>
    );
  }

  const canResend =
    !resending &&
    !resendState.in_cooldown &&
    resendState.can_resend &&
    countdown <= 0;

  const sendsRemaining = Math.max(0, 3 - resendState.sends_used);

  return (
    <div className={styles.panel}>
      <h1 className={styles.title}>Confirm your address</h1>
      <p className={styles.lede}>{message}</p>

      <div className={styles.section}>
        {emailSent ? (
          <>
            <Callout tone="info" title="Verification email sent">
              A verification link has been sent to{" "}
              <span className={styles.address}>{email}</span>. Click it to
              confirm your address, then sign in.
            </Callout>

            {resendSuccess && (
              <Callout tone="ok" title="New link sent">
                A fresh verification link has been sent to{" "}
                <span className={styles.address}>{email}</span>.
              </Callout>
            )}

            {resendState.in_cooldown ? (
              <p className={styles.note}>
                Maximum sends reached. Please wait 24 hours before requesting
                another link.
              </p>
            ) : (
              <div className={styles.actions}>
                <Button
                  variant="secondary"
                  onClick={() => void handleResend()}
                  disabled={!canResend}
                >
                  {resending
                    ? "Sending…"
                    : countdown > 0
                      ? `Resend link (${countdown}s)`
                      : sendsRemaining > 0
                        ? `Resend link (${sendsRemaining} left)`
                        : "Resend link"}
                </Button>
              </div>
            )}
          </>
        ) : verificationToken ? (
          <>
            <Callout tone="info" title="Email delivery not configured">
              No message was sent to{" "}
              <span className={styles.address}>{email}</span>. Because this is
              not a production deployment, the verification link is returned
              here instead. It is valid for 24 hours and can be used once.
            </Callout>

            <a className={styles.link} href={verificationLink(verificationToken)}>
              {verificationLink(verificationToken)}
            </a>

            <Button
              variant="primary"
              block
              onClick={() => void verify()}
              disabled={state === "verifying"}
            >
              {state === "verifying"
                ? "Verifying…"
                : "Verify this address and continue"}
            </Button>
          </>
        ) : (
          <Callout tone="caution" title="No verification link was issued">
            Your address needs confirming before you can sign in, but email
            delivery is not configured for this deployment, so nothing was sent
            to <span className={styles.address}>{email}</span>. Contact whoever
            operates this deployment to get a verification link.
          </Callout>
        )}

        {error ? (
          <Callout tone="fail" role="alert" title={state === "failed" ? "Could not verify" : "Could not send"}>
            {error}
          </Callout>
        ) : null}

        <div className={styles.actions}>
          <Button variant="secondary" onClick={onDone}>
            Back to sign in
          </Button>
        </div>
      </div>
    </div>
  );
}

function verificationLink(token: string): string {
  const path = `/verify-email?token=${encodeURIComponent(token)}`;
  if (typeof window === "undefined") return path;
  return `${window.location.origin}${path}`;
}
