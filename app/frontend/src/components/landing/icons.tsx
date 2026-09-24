/**
 * Line icons for the landing page.
 *
 * Drawn inline rather than loaded, so they inherit `currentColor` and cost no
 * request. All are decorative: every one sits beside text that says the same
 * thing, so each is `aria-hidden`.
 */

type IconProps = { className?: string };

function Svg({ className, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** Two people, side by side — "Trusted by teams". */
export function TeamIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="9" cy="8.5" r="2.6" />
      <path d="M4.2 17.5c.6-2.7 2.5-4.2 4.8-4.2s4.2 1.5 4.8 4.2" />
      <circle cx="15.8" cy="9.4" r="2.1" />
      <path d="M15.2 13.4c2 0 3.8 1.2 4.5 3.6" />
    </Svg>
  );
}

/** A shield with a closed facet — "Secure & private". */
export function ShieldIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M12 3.6 18.6 6v5.3c0 4.1-2.8 7.4-6.6 9.1-3.8-1.7-6.6-5-6.6-9.1V6L12 3.6Z" />
      <path d="M9 10.6 12 9l3 1.6v3.3L12 15.5l-3-1.6v-3.3Z" />
    </Svg>
  );
}

/** Two linked nodes branching into action — "From questions to action". */
export function ActionIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="6.4" cy="7" r="2" />
      <circle cx="17.6" cy="7" r="2" />
      <circle cx="12" cy="17.4" r="2" />
      <path d="M8 8.3 11 15.6M16 8.3 13 15.6M8.4 7h7.2" />
    </Svg>
  );
}

/** The right arrow on the primary call to action. */
export function ArrowRightIcon(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M5 12h13.5M13.5 7l5 5-5 5" />
    </Svg>
  );
}

/** A filled play mark in a solid disc, as on the "Watch Demo" button. */
export function PlayCircleIcon({ className }: IconProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      width="22"
      height="22"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="12" cy="12" r="11" fill="currentColor" />
      <path d="M10 7.8v8.4l6.6-4.2L10 7.8Z" fill="#070c11" />
    </svg>
  );
}
