"use client";

import { usePathname, useRouter } from "next/navigation";
import { useState, useSyncExternalStore } from "react";

import { ConnectionNotice } from "@/components/ConnectionNotice";
import { ErrorNotice } from "@/components/ErrorNotice";
import { SignInPanel } from "@/components/SignInPanel";
import { WorkspaceOnboarding } from "@/components/WorkspaceOnboarding";
import { VerifyEmailPrompt } from "@/components/auth/VerifyEmailPrompt";
import { AppShell } from "@/components/shell/AppShell";
import { AuthLayout } from "@/components/shell/AuthLayout";
import { LandingPage } from "@/components/landing/LandingPage";
import { useChat, useSession } from "@/app/providers";
import { PUBLIC_DEMO_SIGN_IN_ENABLED } from "@/lib/features";
import { readSessionHint, subscribeSessionHint } from "@/lib/session-hint";

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
 * One more, in front of those three: **the public landing page at `/`.** It is
 * shown to a signed-out visitor, and — because it needs nothing from the API —
 * also while the session is still `loading` for a browser with no record of a
 * recent sign-in (`lib/session-hint.ts`). A returning user keeps the behaviour
 * below; nobody signed in ever sees it.
 *
 * `loading` otherwise falls through to the shell rather than rendering a
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

/** Where the landing page's "Get Started" leads: the form opens on registration. */
const REGISTER_ROUTE = "/get-started";

/** The two ways in from the landing page. */
const AUTH_ROUTES = ["/sign-in", REGISTER_ROUTE];

/** `false` while rendering on the server, where no browser storage exists. */
const serverSessionHint = () => false;

interface Registration {
  email: string;
  message: string;
  token?: string;
  emailSent: boolean;
}

export function AppFrame({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const chat = useChat();
  const router = useRouter();
  const pathname = usePathname() ?? "/";
  const [registration, setRegistration] = useState<Registration | null>(null);
  const recentlySignedIn = useSyncExternalStore(
    subscribeSessionHint,
    readSessionHint,
    serverSessionHint,
  );

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

  // The public front door. Signed-out visitors to `/` see the landing page
  // rather than a sign-in form; so does a first-time visitor while the API is
  // still being reached, since nothing on the page depends on it.
  if (
    pathname === "/" &&
    (session.stage === "signed-out" ||
      (session.stage === "loading" && !recentlySignedIn))
  ) {
    return <LandingPage />;
  }

  // Someone who has just pressed "Sign in" or "Get Started" is waiting for a
  // form, not for the product: while the API is still being reached they see
  // the connection notice in the sign-in frame, never the application chrome.
  if (AUTH_ROUTES.includes(pathname) && session.stage === "loading") {
    return (
      <AuthLayout>
        {chat.principalsError ? (
          <ErrorNotice error={chat.principalsError} />
        ) : (
          <ConnectionNotice state={chat.connection} />
        )}
      </AuthLayout>
    );
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
            onDone={() => {
              setRegistration(null);
              // "Continue to sign in" means the sign-in form, not a second
              // registration form on the page they registered from.
              if (pathname === REGISTER_ROUTE) router.replace("/sign-in");
            }}
          />
        </AuthLayout>
      );
    }

    return (
      <AuthLayout>
        <SignInPanel
          // Keyed so moving between `/sign-in` and `/get-started` opens the
          // tab the link promised rather than whichever was open before.
          key={pathname === REGISTER_ROUTE ? "register" : "signin"}
          stage={session.stage}
          busy={session.busy}
          error={session.error}
          initialMode={pathname === REGISTER_ROUTE ? "register" : "signin"}
          // The public demo is not offered for now. Everything behind it —
          // the endpoint, the seeded workspace, `signInToDemo` and the panel
          // itself — is intact; `PUBLIC_DEMO_SIGN_IN_ENABLED` restores it.
          demoAvailable={PUBLIC_DEMO_SIGN_IN_ENABLED && session.demoAvailable}
          onSignIn={session.signIn}
          onDemoSignIn={PUBLIC_DEMO_SIGN_IN_ENABLED ? session.signInToDemo : undefined}
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
