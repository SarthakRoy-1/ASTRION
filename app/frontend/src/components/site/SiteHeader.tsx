import Image from "next/image";
import Link from "next/link";

import styles from "./SiteHeader.module.css";

/**
 * Site sections not yet built. Shown so the header reads as designed, but not
 * links: a link to a page that does not exist is a broken promise, and each
 * item says so to assistive technology.
 */
const SITE_SECTIONS = ["Product", "Solutions", "Pricing", "Resources"] as const;

/**
 * The public site's header: the landing page and the sign-in page render this
 * one component, so logo, navigation and calls to action sit in exactly the
 * same place on both.
 *
 * `current` marks the page the visitor is on; on the sign-in page "Sign in" is
 * announced as the current page rather than offered as a link to itself.
 */
export function SiteHeader({ current }: { current?: "sign-in" }) {
  return (
    <header className={styles.header}>
      <Link href="/" className={styles.brand} aria-label="ASTRION home">
        <Image
          src="/astrion-logo-light.png"
          alt="ASTRION"
          width={182}
          height={36}
          priority
        />
      </Link>

      <nav className={styles.sections} aria-label="Site">
        <ul>
          {SITE_SECTIONS.map((section) => (
            <li key={section}>
              <span className={styles.section}>
                {section}
                <span className="visually-hidden"> (coming soon)</span>
              </span>
            </li>
          ))}
        </ul>
      </nav>

      <div className={styles.account}>
        <Link
          href="/sign-in"
          className={styles.signIn}
          aria-current={current === "sign-in" ? "page" : undefined}
        >
          Sign in
        </Link>
        <Link href="/get-started" className={styles.headerCta}>
          Get Started
        </Link>
      </div>
    </header>
  );
}
