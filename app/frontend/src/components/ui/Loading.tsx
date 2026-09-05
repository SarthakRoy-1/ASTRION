import styles from "./Loading.module.css";

/**
 * A shaped placeholder for content that is on its way.
 *
 * Marked `aria-hidden`: a screen reader gains nothing from four grey bars, and
 * the surrounding region carries a `role="status"` message saying what is
 * loading. Two announcements for one wait is one too many.
 */
export function Skeleton({ width, height }: { width?: string; height?: number }) {
  return (
    <div
      className={styles.skeleton}
      aria-hidden="true"
      style={{
        width,
        ...(height ? ({ "--skeleton-height": `${height}px` } as React.CSSProperties) : {}),
      }}
    />
  );
}

/** A list of placeholder rows, for a list that has not arrived. */
export function SkeletonRows({
  rows = 3,
  label,
}: {
  rows?: number;
  /** What is being waited for, announced once. */
  label: string;
}) {
  return (
    <div className={styles.skeletonList}>
      <p className="visually-hidden" role="status">
        {label}
      </p>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className={styles.row}>
          <Skeleton width="45%" height={14} />
          <Skeleton width="80%" height={12} />
        </div>
      ))}
    </div>
  );
}

/**
 * An inline "working on it".
 *
 * The label is a real, visible sentence rather than a bare spinner, and it is
 * inside a `role="status"` so it is announced politely once. Nothing here
 * implies progress: the backend answers in a single response, so a progress
 * bar would be an animation of a number nobody has.
 */
export function Spinner({ label }: { label: string }) {
  return (
    <p className={styles.spinnerRow} role="status">
      <span className={styles.spinner} aria-hidden="true" />
      {label}
    </p>
  );
}
