import Image from "next/image";

import { SiteBackdrop } from "@/components/site/SiteBackdrop";
import { SiteHeader } from "@/components/site/SiteHeader";

import styles from "./SignInScene.module.css";

/**
 * Footer entries without pages yet. Like the header's site sections they are
 * shown as designed but are not links, and each says so to assistive
 * technology rather than leading nowhere.
 */
const FOOTER_ITEMS = ["Privacy", "Terms", "Help", "Contact us"] as const;

/** The hero's two lines on `/sign-in`, one per line of the headline. */
const SIGN_IN_HEADLINE = ["Welcome Back", "to Astrion"] as const;

/** The same hero, for someone who has no account yet. */
export const GET_STARTED_HEADLINE = ["Welcome", "to Astrion"] as const;

/**
 * The sign-in and get-started pages: one view of the same public site as the
 * landing page.
 *
 * Presentation only. It wraps whatever the sign-in flow renders — the form,
 * the second-factor challenge, or the connection notice while the API wakes —
 * in a translucent card over the brand background, under the landing page's
 * own header. It holds no state and makes no request.
 *
 * The background is the public site's shared artwork (`site/SiteBackdrop`),
 * the same one the landing page wears.
 */
export function SignInScene({
  headline = SIGN_IN_HEADLINE,
  children,
}: {
  /** The hero's two lines. `/get-started` says "Welcome to Astrion". */
  headline?: readonly [string, string];
  children: React.ReactNode;
}) {
  return (
    <div className={styles.scene}>
      <a className="skip-link" href="#sign-in-main">
        Skip to main content
      </a>

      <SiteBackdrop />

      <SiteHeader current="sign-in" />

      <main id="sign-in-main" className={styles.stage}>
        <div className={styles.hero}>
          <h1 className={styles.headline}>
            <span>{headline[0]}</span> <span>{headline[1]}</span>
          </h1>
          <div className={styles.card}>{children}</div>
        </div>
      </main>

      <footer className={styles.footer}>
        <ul className={styles.links} aria-label="Site information">
          {FOOTER_ITEMS.map((item) => (
            <li key={item}>
              {item}
              <span className="visually-hidden"> (coming soon)</span>
            </li>
          ))}
        </ul>
        {/* The mark from the supplied lockup, as a quiet signature. */}
        <Image className={styles.mark} src="/astrion-mark-light.png" alt="" width={30} height={25} />
      </footer>
    </div>
  );
}

/**
 * What the card holds while the API is still being reached: the form's own
 * title, and the notice explaining the wait.
 */
export function SignInWaiting({
  title = "Sign in",
  children,
}: {
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={styles.waiting}>
      <h2 className={styles.waitingTitle}>{title}</h2>
      {children}
    </div>
  );
}
