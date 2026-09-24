import { SignedInRedirect } from "@/components/auth/SignedInRedirect";

/**
 * `/get-started` — where the landing page's "Get Started" leads.
 *
 * The existing onboarding path, unchanged: `AppFrame` shows `SignInPanel` on
 * its "Create account" tab, then the email-verification prompt, then workspace
 * creation. This page only handles the case where there is already a session.
 */
export default function GetStartedPage() {
  return <SignedInRedirect />;
}
