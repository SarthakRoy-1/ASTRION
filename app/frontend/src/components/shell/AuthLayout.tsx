import Image from "next/image";
import Link from "next/link";

import styles from "./AuthLayout.module.css";

const POINTS: { title: string; body: string }[] = [
  { title: "Every answer cites its sources.", body: "Policies, SOPs and signed customer agreements, ranked by authority." },
  { title: "Money is calculated, not written.", body: "Cancellation fees, service credits and response targets come from deterministic rules." },
  { title: "Nothing changes without you.", body: "Actions are prepared first, then explicitly confirmed and audited." },
];

export function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.page}>
      <aside className={styles.pitch}>
        <header className={styles.pitchHeader}>
          <Link href="/" className={styles.homeLink} aria-label="ASTRION home"><Image className={styles.pitchLogo} src="/astrion-logo-light.png" alt="ASTRION" width={178} height={35} priority /></Link>
          <span className={styles.headerNote}>AI for real work</span>
        </header>
        <div className={styles.orbit} aria-hidden="true"><span /></div>
        <div className={styles.pitchCopy}>
          <p className={styles.eyebrow}>Grounded operations intelligence</p>
          <h1 className={styles.lede}>Build what’s next with AI that understands.</h1>
          <p className={styles.summary}>A composed workspace for teams who need clear answers, reliable context, and deliberate action.</p>
        </div>
        <ul className={styles.points}>
          {POINTS.map((point) => <li key={point.title} className={styles.point}><span className={styles.pointMark} aria-hidden="true" /><span><span className={styles.pointTitle}>{point.title}</span>{" "}{point.body}</span></li>)}
        </ul>
      </aside>
      <main className={styles.stage}>
        <div className={styles.card}>
          <Link href="/" className={styles.compactHome} aria-label="ASTRION home"><Image className={styles.compactLogo} src="/astrion-logo-dark.png" alt="ASTRION" width={170} height={33} priority /></Link>
          {children}
        </div>
      </main>
    </div>
  );
}
