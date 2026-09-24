"use client";

import { useEffect, useState } from "react";

import { oauthStartUrl } from "@/lib/auth-client";
import type { OAuthProvider } from "@/lib/auth-types";

import styles from "./ProviderButtons.module.css";

const LABELS: Record<OAuthProvider, string> = { google: "Google", github: "GitHub" };
const ORDER: OAuthProvider[] = ["google", "github"];

/**
 * "Continue with Google" and "Continue with GitHub".
 *
 * Each is a navigation to the backend, which sets the state cookie and sends
 * the browser on to the provider — so there is no token, secret or callback
 * URL anywhere in this file. Once one is pressed both are disabled until the
 * page is left, so a double click cannot start two flows; coming *back* to the
 * page (the browser's back button restores it from cache) re-enables them.
 *
 * A provider the server has no credentials for is still shown, disabled, with
 * a note saying so — rather than silently missing, which would read as a
 * layout bug, or enabled, which would lead to an error page.
 */
export function ProviderButtons({
  available,
  disabled = false,
  navigate = (url: string) => window.location.assign(url),
}: {
  available: OAuthProvider[];
  disabled?: boolean;
  /** Injected in tests; the browser's own navigation otherwise. */
  navigate?(url: string): void;
}) {
  const [connecting, setConnecting] = useState<OAuthProvider | null>(null);

  useEffect(() => {
    const reset = (event: PageTransitionEvent) => {
      if (event.persisted) setConnecting(null);
    };
    window.addEventListener("pageshow", reset);
    return () => window.removeEventListener("pageshow", reset);
  }, []);

  const missing = ORDER.filter((p) => !available.includes(p));

  return (
    <div className={styles.providers}>
      {ORDER.map((provider) => {
        const offered = available.includes(provider);
        const label = LABELS[provider];
        const isConnecting = connecting === provider;
        return (
          <button
            key={provider}
            type="button"
            className={styles.provider}
            disabled={disabled || !offered || connecting !== null}
            aria-busy={isConnecting || undefined}
            aria-describedby={offered ? undefined : "provider-unavailable"}
            onClick={() => {
              if (connecting !== null) return;
              setConnecting(provider);
              navigate(oauthStartUrl(provider));
            }}
          >
            {provider === "google" ? <GoogleMark /> : <GitHubMark />}
            <span>{isConnecting ? `Connecting to ${label}…` : `Continue with ${label}`}</span>
          </button>
        );
      })}
      {missing.length > 0 ? (
        <p id="provider-unavailable" className={styles.note}>
          {missing.map((p) => LABELS[p]).join(" and ")} sign-in{" "}
          {missing.length > 1 ? "aren’t" : "isn’t"} set up on this deployment yet.
        </p>
      ) : null}
    </div>
  );
}

/** Google's four-colour "G", as its brand guidelines ask sign-in buttons to show it. */
function GoogleMark() {
  return (
    <svg className={styles.mark} viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
    </svg>
  );
}

/** The GitHub mark, drawn in the text colour so it holds on light and dark. */
function GitHubMark() {
  return (
    <svg className={styles.mark} viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
      />
    </svg>
  );
}
