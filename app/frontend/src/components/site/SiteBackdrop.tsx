import Image from "next/image";

import styles from "./SiteBackdrop.module.css";

/** The artwork's own proportions: 2000 × 1150. */
const ARTWORK_ASPECT = "40/23";

/**
 * `sizes` for a backdrop that fills a page taller than the viewport (the
 * landing page scrolls on tablets and phones). Whenever the page is narrower
 * than the artwork's proportions the cover crop is wider than the viewport,
 * so ask for the full-width source rather than a viewport-wide one that would
 * then be scaled up.
 */
export const BACKDROP_SIZES_FULL_PAGE = `(max-aspect-ratio: ${ARTWORK_ASPECT}) 2000px, 100vw`;

/**
 * The public site's background: the supplied Astrion artwork, used whole.
 *
 * One implementation for every page that wears it — the landing page and the
 * sign-in page — so both show the same image in the same way:
 *
 * - covered to its box and never stretched, pinned to the right edge, so a
 *   narrower window gives up starfield on the left and never the planet;
 * - the "HIGHER CONTEXT / BRIGHTER OUTCOMES" line drawn in the image's own
 *   coordinate space (an SVG sliced exactly as the image is covered), so it
 *   stays on the same patch of the planet at every size;
 * - a soft shade on the left and at the foot, only as much as the copy over
 *   it needs. The artwork itself is not recoloured.
 *
 * Fills its nearest positioned ancestor, behind everything else in it. Purely
 * decorative, so hidden from assistive technology.
 */
export function SiteBackdrop({ sizes = "100vw" }: { sizes?: string }) {
  return (
    <div className={styles.backdrop} aria-hidden="true">
      <Image
        className={styles.backdropImage}
        src="/astrion-background.webp"
        alt=""
        fill
        priority
        sizes={sizes}
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
  );
}
