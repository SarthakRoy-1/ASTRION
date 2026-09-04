"use client";

import { useState } from "react";

import { AppHeader } from "@/components/AppHeader";
import { Composer } from "@/components/Composer";
import { ConnectionNotice } from "@/components/ConnectionNotice";
import { Conversation } from "@/components/Conversation";
import { ErrorNotice } from "@/components/ErrorNotice";
import { MembersPanel } from "@/components/MembersPanel";
import { OperationsPanel } from "@/components/OperationsPanel";
import { SignInPanel } from "@/components/SignInPanel";
import { SystemStatus } from "@/components/SystemStatus";
import { WorkspaceOnboarding } from "@/components/WorkspaceOnboarding";
import { WorkspaceSwitcher } from "@/components/WorkspaceSwitcher";
import { useConversation } from "@/hooks/useConversation";
import { useWorkspaceSession } from "@/hooks/useWorkspaceSession";

import styles from "./page.module.css";

/**
 * The application shell, routed by session stage.
 *
 * Phase 1 added identity, so this page is no longer unconditionally the chat
 * screen. It routes on `session.stage`, which the server decides:
 *
 *     loading      -> nothing yet
 *     signed-out   -> sign in / create account
 *     mfa-required -> second factor
 *     onboarding   -> create your first workspace
 *     ready | demo -> the assistant
 *
 * `demo` is the original mock-identity mode, which the deployed demo still
 * runs on. It keeps the persona picker and skips authentication entirely,
 * because there are no user accounts in that mode. The distinction comes from
 * `/health`, never from a build flag — a deployment cannot accidentally show
 * the demo UI while running real authentication, or the reverse.
 *
 * The page still holds no business logic and makes no authorization decision.
 * It hides controls the active role does not grant, purely so people are not
 * offered buttons that would fail; the server re-checks every one of them.
 */
export default function ChatPage() {
  const conversation = useConversation();
  const {
    identity,
    principal,
    principals,
    principalsError,
    loadingPrincipals,
    connection,
    health,
    turns,
    sending,
    hasStarted,
    conversations,
    selectIdentity,
    selectConversation,
    send,
    respondToAction,
    reset,
  } = conversation;

  // The session is resolved from the health the conversation already fetched,
  // so a sleeping backend is probed once rather than twice.
  const session = useWorkspaceSession(health);
  const [showMembers, setShowMembers] = useState(false);
  const [showOperations, setShowOperations] = useState(false);

  // `loading` deliberately falls through to the shell below rather than
  // rendering a spinner: while the backend is still waking, what the user
  // needs to see is the connection notice explaining the wait, not a blank
  // screen and not a sign-in form that could not work yet.
  if (session.stage === "signed-out" || session.stage === "mfa-required") {
    return (
      <div className={styles.app}>
        <main className={styles.main}>
          <div className={styles.content}>
            <SignInPanel
              stage={session.stage}
              busy={session.busy}
              error={session.error}
              onSignIn={session.signIn}
              onSubmitMfaCode={session.submitMfaCode}
              onRegistered={() => session.clearError()}
              onDismissError={session.clearError}
            />
          </div>
        </main>
      </div>
    );
  }

  if (session.stage === "onboarding") {
    return (
      <div className={styles.app}>
        <main className={styles.main}>
          <div className={styles.content}>
            <WorkspaceOnboarding
              displayName={session.user?.display_name ?? "there"}
              busy={session.busy}
              error={session.error}
              onCreate={session.createWorkspace}
              onSignOut={session.signOut}
            />
          </div>
        </main>
      </div>
    );
  }

  // `loading` renders the shell too, and in that state the deployment's auth
  // mode is not yet known — so the demo affordances stay on screen only when
  // health has actually said `demo_header`.
  const isDemo = session.stage === "demo";
  const resolving = session.stage === "loading";

  // Either kind of identity will do, and the composer must not wait on both.
  //
  // Under real authentication the session *is* the identity, so `ready` there
  // means the session resolved into a workspace. In demo mode there is no
  // session, so it means a persona has been chosen. Written as an `or` rather
  // than branching on `isDemo` deliberately: `isDemo` is only known once
  // `/health` lands, and making the composer wait for a *status* probe it never
  // needed would leave the demo unusable for as long as that took.
  const ready =
    session.stage === "ready" || (Boolean(identity) && !loadingPrincipals);

  return (
    <div className={styles.app}>
      <AppHeader
        principals={isDemo || resolving ? principals : []}
        principal={isDemo || resolving ? principal : null}
        identity={isDemo || resolving ? identity : null}
        busy={sending}
        canReset={hasStarted}
        conversations={conversations}
        onSelectIdentity={selectIdentity}
        onSelectConversation={selectConversation}
        onReset={reset}
      />

      {!isDemo && session.activeWorkspace ? (
        <div className={styles.workspaceBar}>
          <div className={styles.content}>
            <WorkspaceSwitcher
              workspaces={session.workspaces}
              activeWorkspace={session.activeWorkspace}
              busy={session.busy}
              onSwitch={(id) => {
                setShowMembers(false);
                // A workspace switch changes which tenant's data the answers
                // came from, so the transcript on screen no longer belongs to
                // the workspace now selected.
                reset();
                void session.switchWorkspace(id);
              }}
              onManage={() => setShowMembers((open) => !open)}
              canManageMembers={session.can("members.read")}
            />
            <div className={styles.workspaceActions}>
              {/* Hidden when the role does not grant it — a rendering choice
                  only. The server re-checks `operations.read` regardless. */}
              {session.can("operations.read") ? (
                <button
                  className={styles.opsButton}
                  type="button"
                  onClick={() => {
                    setShowMembers(false);
                    setShowOperations((open) => !open);
                  }}
                >
                  Operations
                </button>
              ) : null}
              <button
                className={styles.signOut}
                type="button"
                onClick={() => void session.signOut()}
              >
                Sign out
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <main className={styles.main}>
        <div className={styles.content}>
          {/* Rendered as plain text rather than through `ErrorNotice`, which
              takes an `ApiError` and branches on its structured code. A session
              error is already a resolved, user-safe message; wrapping it in a
              synthetic error object would fake a code the backend never sent. */}
          {session.error && !isDemo ? (
            <p className={styles.sessionError} role="alert">
              {session.error}
            </p>
          ) : null}

          {principalsError ? (
            <div className={styles.startupError}>
              <ErrorNotice error={principalsError} />
            </div>
          ) : (
            <div className={styles.startupNotice}>
              <ConnectionNotice state={connection} />
            </div>
          )}

          {showOperations && !isDemo ? (
            <OperationsPanel
              onClose={() => setShowOperations(false)}
              onInvestigate={(question) => {
                // Hands the question to the existing agent rather than building
                // a second investigation path. The panel closes so the answer
                // is what the reader sees next.
                setShowOperations(false);
                send(question);
              }}
            />
          ) : null}

          {showMembers && session.activeWorkspace && session.user ? (
            <MembersPanel
              workspace={session.activeWorkspace}
              currentUserId={session.user.user_id}
              onClose={() => setShowMembers(false)}
              onChanged={() => void session.refresh()}
            />
          ) : null}

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
            Every answer cites its sources. Nothing changes without your
            confirmation.
          </p>
          <SystemStatus health={health} />
        </div>
      </div>
    </div>
  );
}
