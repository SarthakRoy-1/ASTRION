import type { ApiError } from "@/lib/client";

import styles from "./ErrorNotice.module.css";

/**
 * A request that failed, said plainly.
 *
 * The heading is chosen from the backend's own error `code`, so an
 * authorization refusal, a provider outage and a missing dataset each read as
 * what they are instead of collapsing into one generic apology. The body is
 * the backend's message verbatim: it was already written to be safe to show,
 * and rephrasing it here risks softening a refusal into something that sounds
 * retryable when it is not.
 *
 * Note what is deliberately absent: no stack trace, no status-code jargon in
 * the body, no raw `details` dump. The code is shown quietly for support, and
 * nothing else about the server's internals crosses this boundary.
 */
export function ErrorNotice({ error }: { error: ApiError }) {
  return (
    <section className={styles.notice} role="alert">
      <h3 className={styles.heading}>{headingFor(error)}</h3>
      <p className={styles.message}>{error.message}</p>
      <p className={styles.code}>
        <span className={styles.codeLabel}>Reference</span>
        <code>{error.code}</code>
        {error.requestId && <code>{error.requestId}</code>}
      </p>
    </section>
  );
}

function headingFor(error: ApiError): string {
  switch (error.code) {
    case "forbidden":
      return "Outside your permitted account scope";
    case "unauthenticated":
      return "Identity not recognised";
    case "provider_not_configured":
      return "The language model is not configured";
    case "provider_timeout":
      return "The investigation timed out";
    case "provider_error":
      return "Unable to complete the investigation";
    case "data_unavailable":
      return "The ASTRION dataset is not available";
    case "network_error":
      return "Cannot reach the ASTRION API";
    case "validation_error":
    case "invalid_request":
      return "That request could not be sent";
    case "not_found":
      return "Not available";
    default:
      return "Something went wrong";
  }
}
