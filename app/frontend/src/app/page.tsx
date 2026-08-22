"use client";

import { AppHeader } from "@/components/AppHeader";
import { Composer } from "@/components/Composer";
import { Conversation } from "@/components/Conversation";
import { ErrorNotice } from "@/components/ErrorNotice";
import { useConversation } from "@/hooks/useConversation";

import styles from "./page.module.css";

/**
 * The chat page — the whole product surface.
 *
 * Deliberately one screen: a header stating who you are and what you may see,
 * the transcript, and the composer. There is no landing page, no dashboard and
 * no navigation, because the assistant *is* the application and anything else
 * would be a detour on the way to asking it something.
 *
 * The page holds no business logic. It wires the conversation state machine to
 * the components and nothing more; every judgement it renders was made by the
 * backend.
 */
export default function ChatPage() {
  const conversation = useConversation();
  const {
    identity,
    principal,
    principals,
    principalsError,
    loadingPrincipals,
    turns,
    sending,
    hasStarted,
    selectIdentity,
    send,
    respondToAction,
    reset,
  } = conversation;

  // Without a resolved identity there is nobody to ask as. The backend refuses
  // anonymous requests outright, so the composer stays disabled rather than
  // letting the user compose something that cannot be sent.
  const ready = Boolean(identity) && !loadingPrincipals;

  return (
    <div className={styles.app}>
      <AppHeader
        principals={principals}
        principal={principal}
        identity={identity}
        busy={sending}
        canReset={hasStarted}
        onSelectIdentity={selectIdentity}
        onReset={reset}
      />

      <main className={styles.main}>
        <div className={styles.content}>
          {principalsError && (
            <div className={styles.startupError}>
              <ErrorNotice error={principalsError} />
            </div>
          )}

          <Conversation
            turns={turns}
            sending={sending}
            role={principal?.role ?? "customer"}
            onExample={send}
            onRespondToAction={respondToAction}
          />
        </div>
      </main>

      <div className={styles.composerBar}>
        <div className={styles.content}>
          <Composer disabled={!ready} sending={sending} onSend={send} />
          <p className={styles.disclaimer}>
            Answers cite their sources. Policy figures are calculated
            deterministically, and state-changing actions always require explicit
            confirmation.
          </p>
        </div>
      </div>
    </div>
  );
}
