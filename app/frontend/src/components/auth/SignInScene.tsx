import Image from "next/image";

import { SiteHeader } from "@/components/site/SiteHeader";

import styles from "./SignInScene.module.css";

/**
 * Footer entries without pages yet. Like the header's site sections they are
 * shown as designed but are not links, and each says so to assistive
 * technology rather than leading nowhere.
 */
const FOOTER_ITEMS = ["Privacy", "Terms", "Help", "Contact us"] as const;

/**
 * The sign-in page: one view of the same public site as the landing page.
 *
 * Presentation only. It wraps whatever the sign-in flow renders — the form,
 * the second-factor challenge, or the connection notice while the API wakes —
 * in a translucent card over the brand background, under the landing page's
 * own header. It holds no state and makes no request.
 *
 * The background is the supplied artwork, used whole: covered to the viewport
 * and pinned to its right edge so the planet stays on the right. The
 * "HIGHER CONTEXT / BRIGHTER OUTCOMES" line is drawn in the image's own
 * coordinate space (an SVG sliced exactly as the image is covered), so it stays
 * on the same patch of the planet at every viewport size.
 */
export function SignInScene({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.scene}>
      <a className="skip-link" href="#sign-in-main">
        Skip to main content
      </a>

      <div className={styles.backdrop} aria-hidden="true">
        <Image
          className={styles.backdropImage}
          src="/astrion-background.webp"
          alt=""
          fill
          priority
          sizes="100vw"
        />
        <svg
          className={styles.tagline}
          viewBox="0 0 2000 1150"
          preserveAspectRatio="xMaxYMid slice"
          focusable="false"
        >
          <text x="1647" y="628">
            <tspan x="1647">HIGHER</tspan>
            <tspan x="1647" dy="44">CONTEXT</tspan>
            <tspan x="1647" dy="78">BRIGHTER</tspan>
            <tspan x="1647" dy="44">OUTCOMES</tspan>
          </text>
        </svg>
      </div>

      <SiteHeader current="sign-in" />

      <main id="sign-in-main" className={styles.stage}>
        <div className={styles.hero}>
          <h1 className={styles.headline}>
            <span>Welcome Back</span> <span>to Astrion</span>
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
export function SignInWaiting({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.waiting}>
      <h2 className={styles.waitingTitle}>Sign in</h2>
      {children}
    </div>
  );
}
