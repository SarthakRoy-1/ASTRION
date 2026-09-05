"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { verifyEmail } from "@/lib/auth-client";
import { ApiError } from "@/lib/client";

import styles from "../join/join.module.css";

/**
 * Where a verification link lands.
 *
 * The backend refuses a password sign-in until an address is verified, and it
 * answers the refusal with the same wording it uses for a wrong password so
 * the endpoint cannot be used to discover who has an account. That is correct,
 * and it is also why this screen has to exist: without somewhere for the link
 * to go, an unverified user only ever sees "Incorrect email address or
 * password".
 *
 * Reachable while signed out, because that is exactly who opens it. It
 * verifies on arrival rather than asking for a click: the user already
 * expressed intent by opening the link, and the token is single-use, so a
 * confirmation step would only add a way to lose it.
 */
export default function VerifyEmailPage() {
  return (
    <Suspense fallback={<Frame title="Verifying">Checking the link…</Frame>}>
      <Verify />
    </Suspense>
  );
}

type State = "verifying" | "verified" | "failed" | "missing";

function Verify() {
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token");

  const [state, setState] = useState<State>(token ? "verifying" : "missing");
  const [error, setError] = useState<string | null>(null);

  /**
   * Which token has already been sent, so it is never sent twice.
   *
   * The token is single-use: the backend consumes it under a
   * `consumed_at_utc IS NULL` guard, so a second redemption is correctly
   * refused. React's StrictMode runs an effect twice in development, which
   * meant this page redeemed the token, succeeded, redeemed it again, and
   * rendered the second call's refusal over the first call's success — telling
   * someone whose account had just been verified that their link was invalid.
   *
   * A ref rather than state because it must be set *before* the request goes
   * out, and a state update scheduled during render would not be visible to
   * the second invocation in time.
   */
  const attempted = useRef<string | null>(null);

  const run = useCallback(async (value: string) => {
    setState("verifying");
    setError(null);
    try {
      await verifyEmail(value);
      setState("verified");
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
      setState("failed");
    }
  }, []);

  useEffect(() => {
    if (!token || attempted.current === token) return;
    attempted.current = token;
    void run(token);
  }, [token, run]);

  if (state === "missing") {
    return (
      <Frame title="Nothing to verify">
        <Callout tone="fail" title="This link has no token">
          Open the verification link exactly as it was issued, or register again
          to have a new one sent.
        </Callout>
      </Frame>
    );
  }

  if (state === "verified") {
    return (
      <Frame title="Address verified">
        Your email address is confirmed. Sign in to continue.
        <div className={styles.actions}>
          <Button variant="primary" onClick={() => router.push("/")}>
            Continue to sign in
          </Button>
        </div>
      </Frame>
    );
  }

  if (state === "failed") {
    return (
      <Frame title="Could not verify">
        <Callout tone="fail" role="alert" title="That link did not work">
          {error}
        </Callout>
        {/* A link that was never valid and one that has already been used are
            answered identically by the backend, on purpose — the difference
            would say whether an address is registered. So the reader is told
            both possibilities rather than being left to assume the worse one. */}
        <p className={styles.note}>
          If you already opened this link, your address is confirmed and the
          link cannot be used a second time. Try signing in.
        </p>
        <div className={styles.actions}>
          <Button variant="primary" onClick={() => router.push("/")}>
            Go to sign in
          </Button>
        </div>
      </Frame>
    );
  }

  return <Frame title="Verifying">Checking the link…</Frame>;
}

function Frame({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className={styles.panel}>
      <h1 className={styles.title}>{title}</h1>
      <div className={styles.lede}>{children}</div>
    </div>
  );
}
