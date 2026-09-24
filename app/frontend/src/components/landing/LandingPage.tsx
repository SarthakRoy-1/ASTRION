import Link from "next/link";

import { BACKDROP_SIZES_FULL_PAGE, SiteBackdrop } from "@/components/site/SiteBackdrop";
import { SiteHeader } from "@/components/site/SiteHeader";

import { WatchDemoButton } from "./WatchDemoButton";
import { ActionIcon, ArrowRightIcon, ShieldIcon, TeamIcon } from "./icons";

import styles from "./LandingPage.module.css";

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
 *
 * Its background is the public site's shared artwork (`site/SiteBackdrop`),
 * the same image, positioning and tagline as the sign-in page.
 */
export function LandingPage() {
  return (
    <div className={styles.page}>
      <a className="skip-link" href="#landing-main">
        Skip to main content
      </a>

      {/* The same artwork as the sign-in page, behind the whole page. */}
      <SiteBackdrop sizes={BACKDROP_SIZES_FULL_PAGE} />

      <SiteHeader />

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
