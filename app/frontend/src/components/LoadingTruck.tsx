"use client";

import { useEffect, useState } from "react";

import styles from "./LoadingTruck.module.css";

/**
 * Where the delivery-truck loading animation is served from.
 *
 * The source is the brand asset `Delivery Truck _ Loading _ Exporting.webm`,
 * committed byte-for-byte as `app/frontend/public/delivery-truck-loading.webm`
 * (VP8 with an alpha channel, 150 × 150, a 1.96-second loop). Served as-is so
 * the transparency survives; the stylesheet frames the truck in the slot.
 */
export const LOADING_TRUCK_SRC = "/delivery-truck-loading.webm";

/**
 * The small moving mark shown while the API is being reached.
 *
 * Purely decorative — the words beside it carry the status, so the whole
 * element is `aria-hidden`. It degrades rather than breaks, in three cases:
 *
 * - **Reduced motion.** A reader who asked for less movement gets the static
 *   dot, and the video is never requested.
 * - **Before the first frame.** The dot holds the space until the video can
 *   actually play, so a slow network never shows an empty box.
 * - **An asset that cannot load** (missing, blocked, or a codec the browser
 *   lacks). The error is absorbed and the dot stays, which is exactly what
 *   this notice showed before the animation existed.
 */
export function LoadingTruck() {
  const [motionAllowed, setMotionAllowed] = useState(false);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const query = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!query) {
      setMotionAllowed(true);
      return;
    }
    const update = () => setMotionAllowed(!query.matches);
    update();
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);

  const showVideo = motionAllowed && !failed;

  return (
    <span className={styles.slot} aria-hidden="true">
      {showVideo ? (
        <video
          className={ready ? styles.video : `${styles.video} ${styles.pending}`}
          src={LOADING_TRUCK_SRC}
          autoPlay
          loop
          muted
          playsInline
          preload="auto"
          disablePictureInPicture
          tabIndex={-1}
          onLoadedData={() => setReady(true)}
          onError={() => setFailed(true)}
          data-testid="loading-truck"
        />
      ) : null}
      {showVideo && ready ? null : <span className={styles.dot} />}
    </span>
  );
}
