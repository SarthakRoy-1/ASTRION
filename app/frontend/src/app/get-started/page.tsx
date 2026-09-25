import { SignedInRedirect } from "@/components/auth/SignedInRedirect";

/**
 * `/get-started` — where the landing page's "Get Started" leads.
 *
 * The same experience as `/sign-in`: `AppFrame` shows `SignInPanel` in
 * `SignInScene`, opened on account creation, with the same Google/GitHub
 * buttons and divider; then the email code screen, then workspace creation.
 * This page only handles the case where there is already a session.
 */
export default function GetStartedPage() {
  return <SignedInRedirect />;
}
