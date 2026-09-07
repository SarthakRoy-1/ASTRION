"use client";

import Link from "next/link";

import { Composer } from "@/components/Composer";
import { Conversation } from "@/components/Conversation";
import { ConversationHistory } from "@/components/ConversationHistory";
import { ContextRail } from "@/components/support/ContextRail";
import { Button } from "@/components/ui/Button";
import { useChat, useSession } from "@/app/providers";
import { mayChangeState } from "@/lib/types";

import styles from "./page.module.css";

/**
 * Support: "what is happening with this customer, and what should I do?"
 *
 * The page holds no business logic and makes no authorization decision. It
 * hides controls the active role does not grant, purely so people are not
 * offered buttons that would fail; the server re-checks every one of them.
 *
 * Whether this caller may confirm an action comes from two different places
 * depending on how the deployment authenticates, and both are the server's
 * answer rather than this page's:
 *
 * - **Session auth** — the `execute_action` permission on the caller's
 *   membership of the active workspace.
 * - **Demo auth** — the persona's role, from the server's own directory.
 *
 * Deriving it from the persona alone was wrong under session authentication,
 * where there is no persona: every signed-in operations user was told they
 * could not approve state changes, whatever their workspace granted them.
 */
export default function SupportPage() {
  const session = useSession();
  const chat = useChat();

  const demo = session.stage === "demo";
  const canConfirmActions = demo
    ? mayChangeState(chat.principal?.role ?? "customer")
    : session.can("execute_action");
  // Narrower than `canConfirmActions`, and answered the same two ways: the
  // SOP asks for a second, higher signature on a credit above its threshold.
  // Rendering only — the confirmation endpoint re-derives the threshold from
  // the policy engine under whoever is confirming.
  const canApproveHighValue = demo
    ? (chat.principal?.role ?? "customer") === "support_manager"
    : session.can("approve_high_value_action");

  return (
    <main id="main" className={styles.page}>
      <h1 className="visually-hidden">Support</h1>

      <div className={styles.grid}>
        <div className={styles.stream}>
          <div className={styles.toolbar}>
            <ConversationHistory
              conversations={chat.conversations}
              contextName={demo ? (chat.principal?.display_name ?? null) : null}
              disabled={chat.sending}
              onSelect={chat.selectConversation}
            />
            <span className={styles.toolbarSpacer} />
            <Button
              size="sm"
              onClick={chat.reset}
              disabled={chat.sending || !chat.hasStarted}
            >
              New conversation
            </Button>
          </div>

          {chat.origin ? (
            <p className={styles.origin}>
              <span className={styles.originLabel}>Investigating</span>
              <span className={styles.originTitle}>{chat.origin.title}</span>
              <Link
                className={styles.originLink}
                href={{
                  pathname: "/operations",
                  query: { signal: chat.origin.signalId },
                }}
              >
                Back to the signal
              </Link>
            </p>
          ) : null}

          <Conversation
            turns={chat.turns}
            sending={chat.sending}
            canConfirmActions={canConfirmActions}
            canApproveHighValue={canApproveHighValue}
            onExample={chat.send}
            onRespondToAction={chat.respondToAction}
          />
        </div>

        <aside className={styles.rail} aria-label="Context">
          <ContextRail
            demo={demo}
            user={session.user}
            workspace={session.activeWorkspace}
            principal={chat.principal}
            principals={chat.principals}
            identity={chat.identity}
            busy={chat.sending}
            health={chat.health}
            onSelectIdentity={chat.selectIdentity}
          />
        </aside>
      </div>

      <div className={styles.composerBar}>
        <div className={styles.composerInner}>
          <Composer
            disabled={!chat.canSend}
            sending={chat.sending}
            unavailableReason={composerReason(chat.connection)}
            onSend={chat.send}
          />
          <p className={styles.disclaimer}>
            Every answer cites its sources. Nothing changes without your
            confirmation.
          </p>
        </div>
      </div>
    </main>
  );
}

/**
 * Why the composer is not accepting a message yet.
 *
 * Only ever shown while it is actually disabled. The wording tracks the
 * connection state the client already computes, so a cold start reads as a
 * wait and a spent wake-up strategy reads as a fault — the distinction the
 * whole cold-start treatment exists to preserve.
 */
function composerReason(connection: string): string | null {
  if (connection === "unavailable") {
    return "The ASTRION API is not answering. Nothing can be sent until it does.";
  }
  return "Connecting to the ASTRION API…";
}
