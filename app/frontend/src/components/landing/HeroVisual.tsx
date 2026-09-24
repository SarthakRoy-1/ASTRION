import styles from "./HeroVisual.module.css";

/**
 * The landing page's planet: a dark sphere lit along its upper limb, an orbit
 * that passes behind it, and the stacked tagline to its right.
 *
 * One inline SVG, drawn in the coordinate space of the 717 × 420 design
 * reference, so the sphere, its rim light, the orbit and the tagline keep
 * their relative positions at every size. It is anchored to the bottom-right
 * corner and scaled to cover, which is how the composition crops on wider or
 * taller screens without drifting apart.
 *
 * Static on purpose: gradients and one blur, no animation and no script, so it
 * costs a single paint.
 *
 * Geometry (reference units):
 *   planet  centre (740, 440), radius 375 — mostly off the bottom-right edge
 *   light   the brightest point of the limb, at (528, 124)
 *   orbit   ellipse centred (666.7, 272.9), radii 249 × 126, rotated 17.6°;
 *           drawn only from where it leaves the limb, round the front of the
 *           sphere, and off the edge — the back half is hidden by the planet.
 */
export function HeroVisual({ className }: { className?: string }) {
  return (
    <div className={className ? `${styles.visual} ${className}` : styles.visual} aria-hidden="true">
      <svg
        className={styles.svg}
        viewBox="0 0 717 420"
        preserveAspectRatio="xMaxYMax slice"
        focusable="false"
      >
        <defs>
          {/* Light on the limb: a ring of light just inside the edge of the
              sphere, strongest at the edge and gone well before the centre. */}
          <radialGradient id="astrion-planet-limb" cx="0.5" cy="0.5" r="0.5">
            <stop offset="0" stopColor="#c9c4ba" stopOpacity="0" />
            <stop offset="0.8" stopColor="#c9c4ba" stopOpacity="0" />
            <stop offset="0.92" stopColor="#a9a59d" stopOpacity="0.08" />
            <stop offset="0.975" stopColor="#cfc9bf" stopOpacity="0.2" />
            <stop offset="1" stopColor="#e9e2d6" stopOpacity="0.4" />
          </radialGradient>

          {/* …seen only on the side facing the light, upper left, and fading
              out round towards the lower right. */}
          <linearGradient
            id="astrion-planet-light"
            gradientUnits="userSpaceOnUse"
            x1="455"
            y1="170"
            x2="650"
            y2="250"
          >
            <stop offset="0" stopColor="#fff" />
            <stop offset="0.5" stopColor="#fff" stopOpacity="0.45" />
            <stop offset="1" stopColor="#fff" stopOpacity="0" />
          </linearGradient>
          <mask id="astrion-planet-lit-side" maskUnits="userSpaceOnUse" x="300" y="0" width="460" height="420">
            <rect x="300" y="0" width="460" height="420" fill="url(#astrion-planet-light)" />
          </mask>

          {/* The rim line, brightest where the light grazes the limb and
              cooling to a thin blue-grey on either side. */}
          <linearGradient
            id="astrion-planet-rim"
            gradientUnits="userSpaceOnUse"
            x1="365"
            y1="420"
            x2="717"
            y2="64"
          >
            <stop offset="0" stopColor="#8d8a84" stopOpacity="0.08" />
            <stop offset="0.32" stopColor="#b9b3aa" stopOpacity="0.4" />
            <stop offset="0.58" stopColor="#efe6d8" stopOpacity="0.85" />
            <stop offset="0.655" stopColor="#fff8ec" stopOpacity="1" />
            <stop offset="0.78" stopColor="#c9d4e2" stopOpacity="0.5" />
            <stop offset="1" stopColor="#9fb0c6" stopOpacity="0.22" />
          </linearGradient>

          {/* Where the light breaks over the limb: a hot core and a wider haze. */}
          <radialGradient id="astrion-planet-flare">
            <stop offset="0" stopColor="#fffaf1" stopOpacity="1" />
            <stop offset="0.25" stopColor="#fbeedb" stopOpacity="0.55" />
            <stop offset="0.6" stopColor="#e2cdb0" stopOpacity="0.12" />
            <stop offset="1" stopColor="#c7b79f" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="astrion-planet-haze-fill">
            <stop offset="0" stopColor="#f6efe4" stopOpacity="0.42" />
            <stop offset="0.5" stopColor="#d8cbb9" stopOpacity="0.1" />
            <stop offset="1" stopColor="#c7b79f" stopOpacity="0" />
          </radialGradient>

          {/* Surface: fine mottling, rendered once, visible only where the
              limb catches the light. */}
          <filter id="astrion-planet-grain" x="0" y="0" width="100%" height="100%">
            <feTurbulence type="fractalNoise" baseFrequency="0.32" numOctaves="3" seed="11" />
            <feColorMatrix
              type="matrix"
              values="0 0 0 0 0.86  0 0 0 0 0.84  0 0 0 0 0.8  0 0 0 2.2 -1.05"
            />
          </filter>
          <radialGradient id="astrion-planet-band" cx="0.5" cy="0.5" r="0.5">
            <stop offset="0.84" stopColor="#fff" stopOpacity="0" />
            <stop offset="0.95" stopColor="#fff" stopOpacity="0.5" />
            <stop offset="1" stopColor="#fff" stopOpacity="0.9" />
          </radialGradient>
          <mask id="astrion-planet-surface" maskUnits="userSpaceOnUse" x="300" y="0" width="460" height="420">
            <circle
              cx="740"
              cy="440"
              r="375"
              fill="url(#astrion-planet-band)"
              mask="url(#astrion-planet-lit-side)"
            />
          </mask>

          <filter id="astrion-planet-haze" x="-10%" y="-10%" width="120%" height="120%">
            <feGaussianBlur stdDeviation="3.5" />
          </filter>
        </defs>

        {/* Atmosphere: the rim, blurred, just outside the body. */}
        <circle
          cx="740"
          cy="440"
          r="376.5"
          fill="none"
          stroke="url(#astrion-planet-rim)"
          strokeWidth="6"
          opacity="0.32"
          filter="url(#astrion-planet-haze)"
        />

        <circle cx="740" cy="440" r="375" fill="#05090d" />
        <circle
          cx="740"
          cy="440"
          r="375"
          fill="url(#astrion-planet-limb)"
          mask="url(#astrion-planet-lit-side)"
        />

        <rect
          x="360"
          y="60"
          width="400"
          height="360"
          filter="url(#astrion-planet-grain)"
          mask="url(#astrion-planet-surface)"
          opacity="0.24"
        />

        <circle
          cx="740"
          cy="440"
          r="375"
          fill="none"
          stroke="url(#astrion-planet-rim)"
          strokeWidth="0.9"
        />

        <ellipse
          cx="524"
          cy="118"
          rx="84"
          ry="42"
          fill="url(#astrion-planet-haze-fill)"
          transform="rotate(-33.4 524 118)"
        />
        <ellipse
          cx="531"
          cy="126"
          rx="48"
          ry="17"
          fill="url(#astrion-planet-flare)"
          transform="rotate(-33.4 531 126)"
        />

        <path
          className={styles.orbit}
          d="M 521.1 135.6 A 249 126 17.6 0 0 807.6 411.1"
          fill="none"
        />
        <circle className={styles.satellite} cx="454.2" cy="285" r="5.4" />

        <text className={styles.tagline} x="589" y="229">
          <tspan x="589" dy="0">HIGHER</tspan>
          <tspan x="589" dy="18">CONTEXT</tspan>
          <tspan x="589" dy="18">BRIGHTER</tspan>
          <tspan x="589" dy="18">OUTCOMES</tspan>
        </text>
      </svg>
    </div>
  );
}
