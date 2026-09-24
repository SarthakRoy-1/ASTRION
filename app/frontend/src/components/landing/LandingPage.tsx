import Image from "next/image";
import Link from "next/link";

import { HeroVisual } from "./HeroVisual";
import { WatchDemoButton } from "./WatchDemoButton";
import { ActionIcon, ArrowRightIcon, ShieldIcon, TeamIcon } from "./icons";

import styles from "./LandingPage.module.css";

/**
 * Site sections not yet built. Shown so the header reads as designed, but not
 * links: a link to a page that does not exist is a broken promise, and each
 * item says so to assistive technology.
 */
const SITE_SECTIONS = ["Product", "Solutions", "Pricing", "Resources"] as const;

const VALUES = [
  { title: "Trusted by teams", body: "Built for real workflows", Icon: TeamIcon },
  { title: "Secure & private", body: "Your data stays yours", Icon: ShieldIcon },
  { title: "From questions\nto action", body: "Not just answers", Icon: ActionIcon },
] as const;

/**
 * The public landing page, shown at `/` to anyone not signed in.
 *
 * Presentation only. It holds no session state and makes no request: "Sign
 * in" and "Get Started" lead to the application's existing sign-in and
 * registration forms (`/sign-in`, `/get-started`), and everything after that
 * is the same authentication flow as before. It deliberately offers no demo
 * account — see `PUBLIC_DEMO_SIGN_IN_ENABLED` in `lib/features.ts`.
 */
export function LandingPage() {
  return (
    <div className={styles.page}>
      <a className="skip-link" href="#landing-main">
        Skip to main content
      </a>

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
          <Link href="/sign-in" className={styles.signIn}>
            Sign in
          </Link>
          <Link href="/get-started" className={styles.headerCta}>
            Get Started
          </Link>
        </div>
      </header>

      <main id="landing-main" className={styles.hero}>
        <div className={styles.copy}>
          <p className={styles.eyebrow}>AI for real work</p>
          <h1 className={styles.headline}>
            <span>Build what’s next</span> <span>with AI that understands.</span>
          </h1>
          <p className={styles.lede}>
            Astrion helps teams turn complex data, documents, and decisions into
            real progress — with secure, reliable AI agents built for the real
            world.
          </p>
          <div className={styles.ctas}>
            <Link href="/get-started" className={styles.primaryCta}>
              Get Started
              <ArrowRightIcon className={styles.ctaArrow} />
            </Link>
            <WatchDemoButton className={styles.secondaryCta} />
          </div>
        </div>

        <HeroVisual className={styles.visual} />

        <ul className={styles.values} aria-label="Why ASTRION">
          {VALUES.map(({ title, body, Icon }) => (
            <li key={title} className={styles.value}>
              <span className={styles.valueIcon}>
                <Icon />
              </span>
              <span className={styles.valueText}>
                <span className={styles.valueTitle}>{title}</span>
                <span className={styles.valueBody}>{body}</span>
              </span>
            </li>
          ))}
        </ul>
      </main>
    </div>
  );
}
