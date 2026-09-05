import styles from "./AuthLayout.module.css";

/**
 * Every screen you see before you are inside a workspace.
 *
 * Sign in, the second factor, email verification, accepting an invitation and
 * creating a first workspace all share this frame, so a first-time user meets
 * one consistent surface rather than four differently shaped cards.
 *
 * The three points on the left are claims this product can actually back up in
 * the next screen — evidence on every answer, deterministic policy arithmetic,
 * a confirmation gate before anything changes. They are here because "sign in
 * to ParcelPilot" tells a first-time visitor nothing about what ParcelPilot
 * does, and because each one is verifiable a minute later.
 */
const POINTS: { title: string; body: string }[] = [
  {
    title: "Every answer cites its sources.",
    body: "Policies, SOPs and signed customer agreements, ranked by authority — you see which document decided, and which was outranked.",
  },
  {
    title: "Money is calculated, not written.",
    body: "Cancellation fees, service credits and response targets come from deterministic rules, with the arithmetic shown.",
  },
  {
    title: "Nothing changes without you.",
    body: "The assistant can prepare an escalation or a note. A person confirms it, and the whole exchange is audited.",
  },
];

export function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.page}>
      <aside className={styles.pitch}>
        <div className={styles.brand}>
          <span className={styles.brandName}>ParcelPilot</span>
          <span className={styles.brandRole}>Support &amp; Operations</span>
        </div>

        <p className={styles.lede}>
          Deterministic AI for logistics operations.
        </p>

        <ul className={styles.points}>
          {POINTS.map((point) => (
            <li key={point.title} className={styles.point}>
              <span className={styles.pointMark} aria-hidden="true" />
              <span>
                <span className={styles.pointTitle}>{point.title}</span>{" "}
                {point.body}
              </span>
            </li>
          ))}
        </ul>
      </aside>

      <main className={styles.stage}>
        <div className={styles.card}>
          <div className={styles.compactBrand}>
            <span className={styles.brandName}>ParcelPilot</span>
            <span className={styles.brandRole}>Support &amp; Operations</span>
          </div>
          {children}
        </div>
      </main>
    </div>
  );
}
