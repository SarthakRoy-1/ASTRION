"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { ApiError } from "@/lib/client";
import { verifyEmail } from "@/lib/auth-client";

import styles from "./VerifyEmailPrompt.module.css";

type State = "issued" | "verifying" | "verified" | "failed";

/**
 * The step between registering and signing in.
 *
 * The backend refuses a password sign-in until the address is verified, and it
 * answers a failed sign-in with the same wording it uses for a wrong password
 * — deliberately, so the endpoint cannot be used to discover who has an
 * account. The consequence is that an unverified user sees "Incorrect email
 * address or password" and has no way to work out why. This screen is what
 * stops that being a dead end.
 *
 * **This application sends no email, in any environment.** There is no SMTP
 * client, no provider SDK and no mail setting anywhere in the codebase; the
 * backend's own module docstring says delivery is out of scope. What the
 * backend does instead is issue the token and return it in the response *only*
 * when the deployment is not production (`_may_disclose_link`), on the
 * reasoning that the link has to reach a developer somehow and returning it to
 * the caller is acceptable on a laptop and is credential disclosure anywhere
 * else.
 *
 * So this screen has two honest branches, and which one a reader gets is
 * decided by what the server actually returned rather than by a build flag in
 * the browser:
 *
 * - **A token came back.** The real link is shown, and it points at
 *   `/verify-email?token=…` — the same route an emailed link would land on, so
 *   the development path exercises production's own screen rather than a
 *   shortcut around it.
 * - **No token came back.** The address still needs confirming and nobody was
 *   emailed, and the copy says exactly that. Telling someone to check an inbox
 *   nothing was sent to is the single most misleading thing this screen could
 *   do, and it is what it used to say.
 */
export function VerifyEmailPrompt({
  email,
  message,
  verificationToken,
  onDone,
}: {
  email: string;
  /** The backend's own wording. Rendered verbatim. */
  message: string;
  /** Present only when the deployment is permitted to disclose it. */
  verificationToken?: string;
  onDone(): void;
}) {
  const [state, setState] = useState<State>("issued");
  const [error, setError] = useState<string | null>(null);

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

  return (
    <div className={styles.panel}>
      <h1 className={styles.title}>Confirm your address</h1>
      <p className={styles.lede}>{message}</p>

      <div className={styles.section}>
        {verificationToken ? (
          <>
            <Callout tone="info" title="This deployment sends no email">
              No message was sent to{" "}
              <span className={styles.address}>{email}</span> — there is no mail
              transport configured. Because this is not a production
              deployment, the backend returned the verification link here
              instead. It is valid for 24 hours and can be used once.
            </Callout>

            {/* A real anchor to the absolute URL, not a scripted shortcut: this
                is the link an email would contain, and following it exercises
                the same `/verify-email` route a production link would land on. */}
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
          <Callout tone="caution" title="No verification link was issued to you">
            Your address needs confirming before you can sign in, but this
            deployment has no mail delivery, so nothing was sent to{" "}
            <span className={styles.address}>{email}</span>. Whoever operates
            this deployment has to issue the link for you.
          </Callout>
        )}

        {error ? (
          <Callout tone="fail" role="alert" title="Could not verify">
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

/**
 * The link as an email would carry it.
 *
 * Built from the current origin because the backend has no mail transport and
 * therefore no configured frontend URL to put in one. On a server render there
 * is no origin to read, so the relative form is used and the browser resolves
 * it on hydration — both point at the same route.
 */
function verificationLink(token: string): string {
  const path = `/verify-email?token=${encodeURIComponent(token)}`;
  if (typeof window === "undefined") return path;
  return `${window.location.origin}${path}`;
}
