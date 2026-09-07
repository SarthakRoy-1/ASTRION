"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { useSession } from "@/app/providers";
import { acceptInvitation } from "@/lib/auth-client";
import { ApiError } from "@/lib/client";

import styles from "./join.module.css";

/**
 * Where an invitation link lands.
 *
 * `POST /api/invitations/accept` has existed since workspaces did, and the
 * client wrapper for it has too — but nothing in the interface ever called it,
 * so every invitation the members screen issued was unacceptable. The
 * onboarding screen told invitees to "open the invitation link you were sent",
 * and there was no screen at the other end of one.
 *
 * The token travels in the request body, never in a path segment or a query
 * the server sees — a token in a URL ends up in access logs and `Referer`
 * headers. It does arrive here in the address bar, because that is what a link
 * is, and it is read once and posted onward rather than stored.
 *
 * The server decides everything that matters: whether the invitation exists,
 * whether it has expired or been revoked, and whether the signed-in address is
 * the one it was issued to. This screen renders that answer verbatim.
 */
export default function JoinPage() {
  return (
    <Suspense fallback={<Frame>Reading the invitation…</Frame>}>
      <Join />
    </Suspense>
  );
}

function Join() {
  const session = useSession();
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token");

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [joined, setJoined] = useState<string | null>(null);

  async function accept() {
    if (!token) return;
    setBusy(true);
    setError(null);
    try {
      const workspace = await acceptInvitation(token);
      setJoined(workspace.name);
      // Re-read identity and memberships: the caller now belongs to a
      // workspace they did not a moment ago, and the shell's navigation and
      // permissions are derived from that listing.
      await session.refresh();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <Frame>
        <Callout tone="fail" title="No invitation in this link">
          The link is missing its token. Ask whoever invited you to send it
          again — an invitation can be reissued, but it cannot be recovered.
        </Callout>
      </Frame>
    );
  }

  if (joined) {
    return (
      <div className={styles.panel}>
        <h1 className={styles.title}>You have joined {joined}</h1>
        <p className={styles.lede}>
          Your role in this workspace decides what you can do in it. You can see
          it, and what it grants, on the Workspace screen.
        </p>
        <div className={styles.actions}>
          <Button variant="primary" onClick={() => router.push("/")}>
            Go to Support
          </Button>
          <Button onClick={() => router.push("/workspace")}>
            See the workspace
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.panel}>
      <h1 className={styles.title}>Accept your invitation</h1>
      <p className={styles.lede}>
        You have been invited to an ASTRION workspace. Accepting adds you to
        it with the role the invitation was issued for.
      </p>

      <p className={styles.identity}>
        You are signed in as{" "}
        <span className={styles.name}>
          {session.user?.display_name ?? "someone"}
        </span>
        . An invitation can only be accepted by the address it was sent to — if
        that is not you, sign out and sign in as that address first.
      </p>

      {error ? (
        <div className={styles.notice}>
          <Callout tone="fail" role="alert" title="Could not accept">
            {error}
          </Callout>
        </div>
      ) : null}

      <div className={styles.actions}>
        <Button variant="primary" onClick={() => void accept()} disabled={busy}>
          {busy ? "Accepting…" : "Accept invitation"}
        </Button>
        <Button onClick={() => void session.signOut()} disabled={busy}>
          Sign out
        </Button>
      </div>
    </div>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.panel}>
      <h1 className={styles.title}>Invitation</h1>
      <div className={styles.lede}>{children}</div>
    </div>
  );
}
