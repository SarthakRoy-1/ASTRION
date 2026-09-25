"use client";

import { usePathname, useRouter } from "next/navigation";
import { useSyncExternalStore } from "react";

import { ConnectionNotice } from "@/components/ConnectionNotice";
import { ErrorNotice } from "@/components/ErrorNotice";
import { SignInPanel } from "@/components/SignInPanel";
import { WorkspaceOnboarding } from "@/components/WorkspaceOnboarding";
import { GET_STARTED_HEADLINE, SignInScene, SignInWaiting } from "@/components/auth/SignInScene";
import { EmailCodeVerification } from "@/components/auth/EmailCodeVerification";
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
 * The sign-in and registration forms — `/sign-in`, `/get-started`, and wherever
 * a signed-out visitor lands other than `/` — wear the public site instead of
 * `AuthLayout`: `SignInScene`, the landing page's header over the brand
 * background. The two routes are one experience (provider buttons, divider,
 * email form, the code screen, the footer); `/get-started` differs only in
 * opening the form on account creation.
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

export function AppFrame({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const chat = useChat();
  const router = useRouter();
  const pathname = usePathname() ?? "/";
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

  // `verify-email` is not signed in: that visitor holds a verification, not a
  // session, so like `signed-out` they are sent on to the screen below.
  if (
    isPreWorkspace &&
    session.stage !== "signed-out" &&
    session.stage !== "mfa-required" &&
    session.stage !== "verify-email"
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

  // One scene for both routes; the route decides only the hero's wording.
  const scene = (content: React.ReactNode) => (
    <SignInScene headline={pathname === REGISTER_ROUTE ? GET_STARTED_HEADLINE : undefined}>
      {content}
    </SignInScene>
  );

  // Someone who has just pressed "Sign in" or "Get Started" is waiting for a
  // form, not for the product: while the API is still being reached they see
  // the connection notice in the page they asked for, never the application
  // chrome.
  if (AUTH_ROUTES.includes(pathname) && session.stage === "loading") {
    const notice = chat.principalsError ? (
      <ErrorNotice error={chat.principalsError} />
    ) : (
      <ConnectionNotice state={chat.connection} />
    );
    return scene(
      <SignInWaiting title={pathname === REGISTER_ROUTE ? "Create your account" : "Sign in"}>
        {notice}
      </SignInWaiting>,
    );
  }

  // Proving an address with an emailed code — after registering, after the
  // right password for an address never proven, or after a Google/GitHub
  // sign-in that brought no verified address. Wherever the visitor is, this
  // is the screen: nothing else can proceed until it is answered or left.
  if (session.stage === "verify-email" && session.verification) {
    const screen = (
      <EmailCodeVerification
        verification={session.verification}
        appearance="glass"
        onVerified={session.completeVerification}
        onCancel={async () => {
          await session.abandonVerification();
          // "Back to sign in" means the sign-in form, wherever this began.
          if (pathname !== "/sign-in") router.replace("/sign-in");
        }}
      />
    );
    return scene(screen);
  }

  if (session.stage === "signed-out" || session.stage === "mfa-required") {
    const registering = pathname === REGISTER_ROUTE;
    const panel = (
      <SignInPanel
        // Keyed so moving between `/sign-in` and `/get-started` opens the
        // tab the link promised rather than whichever was open before.
        key={registering ? "register" : "signin"}
        stage={session.stage}
        busy={session.busy}
        error={session.error}
        initialMode={registering ? "register" : "signin"}
        appearance="glass"
        // The public demo is not offered for now. Everything behind it —
        // the endpoint, the seeded workspace, `signInToDemo` and the panel
        // itself — is intact; `PUBLIC_DEMO_SIGN_IN_ENABLED` restores it.
        demoAvailable={PUBLIC_DEMO_SIGN_IN_ENABLED && session.demoAvailable}
        onSignIn={session.signIn}
        onDemoSignIn={PUBLIC_DEMO_SIGN_IN_ENABLED ? session.signInToDemo : undefined}
        onSubmitMfaCode={session.submitMfaCode}
        oauthProviders={session.oauthProviders}
        onRegistered={session.beginVerification}
        onDismissError={session.clearError}
      />
    );

    return scene(panel);
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
