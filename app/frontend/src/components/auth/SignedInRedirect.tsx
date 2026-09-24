"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { Spinner } from "@/components/ui/Loading";
import { useSession } from "@/app/providers";

/**
 * The page body of `/sign-in` and `/get-started`.
 *
 * Signed out, `AppFrame` never renders it: it shows the sign-in or
 * registration form in its place. It only renders once there is a session —
 * someone who has just signed in, or who followed a bookmark while already
 * signed in — and its one job then is to send them to the application.
 */
export function SignedInRedirect() {
  const session = useSession();
  const router = useRouter();
  const signedIn = session.stage === "ready" || session.stage === "demo";

  useEffect(() => {
    if (signedIn) router.replace("/");
  }, [router, signedIn]);

  return signedIn ? <Spinner label="Opening your workspace…" /> : null;
}
