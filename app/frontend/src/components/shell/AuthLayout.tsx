import Image from "next/image";
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
          <Image className={styles.pitchLogo} src="/astrion-wordmark.svg" alt="ASTRION" width={220} height={38} priority />
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
          <Image className={styles.compactLogo} src="/astrion-wordmark.svg" alt="ASTRION" width={190} height={33} priority />
          {children}
        </div>
      </main>
    </div>
  );
}
