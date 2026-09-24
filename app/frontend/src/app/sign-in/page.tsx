import { SignedInRedirect } from "@/components/auth/SignedInRedirect";

/**
 * `/sign-in` — where the landing page's "Sign in" leads.
 *
 * The form itself is rendered by `AppFrame`, which shows the existing
 * `SignInPanel` on its "Sign in" tab to anyone not signed in. This page only
 * handles the case where there is already a session.
 */
export default function SignInPage() {
  return <SignedInRedirect />;
}
