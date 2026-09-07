"use client";

import { usePathname } from "next/navigation";
import { useState } from "react";

import { SignInPanel } from "@/components/SignInPanel";
import { WorkspaceOnboarding } from "@/components/WorkspaceOnboarding";
import { VerifyEmailPrompt } from "@/components/auth/VerifyEmailPrompt";
import { AppShell } from "@/components/shell/AppShell";
import { AuthLayout } from "@/components/shell/AuthLayout";
import { useSession } from "@/app/providers";

/**
 * Which of the product's three frames the current URL and session get.
 *
 * There are exactly three, and the choice is made in one place so no route can
 * accidentally render the signed-in chrome to a signed-out visitor:
 *
 *     AuthLayout + gate     nobody is signed in, or nobody has a workspace
 *     AuthLayout + children a screen reached before belonging to a workspace
 *     AppShell   + children the product
 *
 * The route decides before the session does. `/verify-email` and `/join` are
 * where an emailed link lands, and both are reached by someone who is not yet
 * inside a workspace — so they never wear the signed-in chrome, whatever the
 * session turns out to be.
 *
 * `loading` deliberately falls through to the shell rather than rendering a
 * spinner. While the backend is still waking, what the user needs to see is
 * the connection notice explaining the wait — not a blank screen, and not a
 * sign-in form that could not work yet. A backend we cannot reach is not a
 * signed-out user, and the previous behaviour of showing one is what made a
 * cold start look like a logout.
 */

/** Reachable without a session, because a link in an email lands here. */
const PUBLIC_ROUTES = ["/verify-email"];

/** Reachable while signed in but *before* belonging to any workspace. */
const PRE_WORKSPACE_ROUTES = ["/join"];

interface Registration {
  email: string;
  message: string;
  token?: string;
  emailSent: boolean;
}

export function AppFrame({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const pathname = usePathname() ?? "/";
  const [registration, setRegistration] = useState<Registration | null>(null);

  const isPublic = PUBLIC_ROUTES.includes(pathname);
  const isPreWorkspace = PRE_WORKSPACE_ROUTES.includes(pathname);

  // Decided before the session is: these screens never wear the signed-in
  // chrome, whatever stage resolves. Letting `loading` fall through to the
  // shell first put a navigation bar and a workspace switcher on an invitation
  // link for as long as the backend took to answer, then replaced the whole
  // tree once it did — a flash of the wrong product, and a remount that threw
  // away anything the reader had already started interacting with.
  if (isPublic) {
    return <AuthLayout>{children}</AuthLayout>;
  }

  if (
    isPreWorkspace &&
    session.stage !== "signed-out" &&
    session.stage !== "mfa-required"
  ) {
    return <AuthLayout>{children}</AuthLayout>;
  }

  if (session.stage === "signed-out" || session.stage === "mfa-required") {
    // Registration succeeded and the address is not yet confirmed. The panel
    // below closes that loop; without it the only route onward was a sign-in
    // the backend is required to refuse.
    if (registration && session.stage === "signed-out") {
      return (
        <AuthLayout>
          <VerifyEmailPrompt
            email={registration.email}
            message={registration.message}
            emailSent={registration.emailSent}
            verificationToken={registration.token}
            onDone={() => setRegistration(null)}
          />
        </AuthLayout>
      );
    }

    return (
      <AuthLayout>
        <SignInPanel
          stage={session.stage}
          busy={session.busy}
          error={session.error}
          onSignIn={session.signIn}
          onSubmitMfaCode={session.submitMfaCode}
          onRegistered={(message, token, email, emailSent) =>
            setRegistration({ message, token, email: email ?? "", emailSent: emailSent ?? false })
          }
          onDismissError={session.clearError}
        />
      </AuthLayout>
    );
  }

  if (session.stage === "onboarding") {
    return (
      <AuthLayout>
        <WorkspaceOnboarding
          displayName={session.user?.display_name ?? "there"}
          busy={session.busy}
          error={session.error}
          onCreate={session.createWorkspace}
          onSignOut={session.signOut}
        />
      </AuthLayout>
    );
  }

  return <AppShell>{children}</AppShell>;
}
