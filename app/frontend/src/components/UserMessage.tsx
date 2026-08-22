import styles from "./UserMessage.module.css";

/** What the user asked. Visually distinct from, and aligned opposite to, the
 *  agent's reply so a scrolled-back transcript stays legible at a glance. */
export function UserMessage({ text }: { text: string }) {
  return (
    <article className={styles.wrapper}>
      <h2 className="visually-hidden">Your message</h2>
      <p className={styles.bubble}>{text}</p>
    </article>
  );
}
