"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { StatusPill } from "@/components/StatusPill";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { EmptyState } from "@/components/ui/EmptyState";
import { SelectField } from "@/components/ui/Field";
import { SkeletonRows } from "@/components/ui/Loading";
import { fetchAuditLog } from "@/lib/auth-client";
import { ApiError } from "@/lib/client";
import {
  AUDIT_CATEGORY_LABELS,
  auditDetails,
  auditEventCategory,
  auditEventLabel,
  auditOutcome,
  formatAuditTime,
  type AuditCategory,
} from "@/lib/audit-presentation";
import type { AuditListing } from "@/lib/auth-types";

import styles from "./AuditTrail.module.css";

/**
 * "Who did what here, and can I prove it?"
 *
 * The trail itself has existed since workspaces did — append-only,
 * hash-chained, redacting, workspace-scoped and permission-gated. What it had
 * no way to be was *read*: the interface told users their work was audited,
 * granted `read_audit_log` to three roles, and offered nothing to read it
 * with. This is that surface, and nothing more — it renders what the endpoint
 * returns and re-derives none of it.
 *
 * Three things it is careful about:
 *
 * - **The chain result is reported, never assumed.** The server verifies it;
 *   a broken chain is shown at the top, before the entries, because a list
 *   rendered as though it were intact is worse than no list.
 * - **Filtering is client-side over the page already fetched, and says so.**
 *   Pretending to search the whole trail while looking at the last hundred
 *   rows would be a lie about scope.
 * - **A refusal is not a fault.** `denied` wears caution: those entries are
 *   the control working, and they are what a reviewer came to find.
 */
export function AuditTrail({ limit = 100 }: { limit?: number }) {
  const [listing, setListing] = useState<AuditListing | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  const [category, setCategory] = useState<AuditCategory | "all">("all");
  const [actor, setActor] = useState<string>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setListing(await fetchAuditLog(limit));
    } catch (cause) {
      setListing(null);
      setError(
        cause instanceof ApiError
          ? cause
          : new ApiError(String(cause), { code: "client_error", status: 0 }),
      );
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    void load();
  }, [load]);

  const events = useMemo(() => listing?.events ?? [], [listing]);

  const actors = useMemo(
    () =>
      Array.from(
        new Set(events.map((e) => e.actor_user_id).filter((a): a is string => !!a)),
      ).sort(),
    [events],
  );

  const shown = useMemo(
    () =>
      events.filter(
        (entry) =>
          (category === "all" || auditEventCategory(entry.event_type) === category) &&
          (actor === "all" || entry.actor_user_id === actor),
      ),
    [events, category, actor],
  );

  if (loading) {
    return <SkeletonRows rows={5} label="Loading the audit trail." />;
  }

  if (error) {
    // A role without the permission is refused by the server, which is the
    // boundary. The message is the backend's own.
    const forbidden = error.code === "forbidden";
    return (
      <Callout
        tone={forbidden ? "neutral" : "fail"}
        role="alert"
        title={
          forbidden ? "You cannot read the audit trail" : "Could not load the audit trail"
        }
      >
        {error.message}
        {forbidden ? null : (
          <div style={{ marginTop: "var(--space-3)" }}>
            <Button size="sm" onClick={() => void load()}>
              Try again
            </Button>
          </div>
        )}
      </Callout>
    );
  }

  return (
    <>
      {listing && !listing.chain_intact ? (
        <div className={styles.chain}>
          <Callout tone="fail" role="alert" title="This trail has been altered">
            The hash chain does not verify from entry {listing.first_invalid_seq}{" "}
            onward. Every entry commits to the one before it, so a row that was
            edited, deleted or reordered breaks the chain from that point.
            Treat what follows as unproven.
          </Callout>
        </div>
      ) : null}

      {events.length === 0 ? (
        <EmptyState title="Nothing recorded yet">
          This workspace has no audit entries. Signing in, asking the
          assistant, and confirming an action all write one.
        </EmptyState>
      ) : (
        <>
          <div className={styles.controls}>
            <SelectField
              className={styles.filter}
              label="Event type"
              value={category}
              onChange={(event) =>
                setCategory(event.target.value as AuditCategory | "all")
              }
            >
              <option value="all">All events</option>
              {(Object.keys(AUDIT_CATEGORY_LABELS) as AuditCategory[]).map((key) => (
                <option key={key} value={key}>
                  {AUDIT_CATEGORY_LABELS[key]}
                </option>
              ))}
            </SelectField>

            <SelectField
              className={styles.filter}
              label="Who"
              value={actor}
              onChange={(event) => setActor(event.target.value)}
            >
              <option value="all">Anyone</option>
              {actors.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </SelectField>

            <p className={styles.count}>
              {shown.length} of the {events.length} most recent{" "}
              {events.length === 1 ? "entry" : "entries"}
              {listing?.chain_intact ? " · chain verified" : ""}. Filtering
              applies to what is loaded here, not to the whole trail.
            </p>
          </div>

          {shown.length === 0 ? (
            <EmptyState title="No entries match this filter">
              Widen the event type or choose a different person.
            </EmptyState>
          ) : (
            <ul className={styles.list}>
              {shown.map((entry) => {
                const outcome = auditOutcome(entry.outcome);
                return (
                  <li
                    key={entry.event_id}
                    className={styles.entry}
                    data-outcome={entry.outcome}
                  >
                    <span className={styles.top}>
                      <span className={styles.label}>
                        {auditEventLabel(entry.event_type)}
                      </span>
                      <StatusPill tone={outcome.tone} quiet>
                        {outcome.label}
                      </StatusPill>
                    </span>

                    <span className={styles.meta}>
                      <span className={styles.actor}>
                        {entry.actor_user_id ?? "the system"}
                      </span>
                      {entry.actor_role ? (
                        <>
                          <span className={styles.divider} aria-hidden="true">
                            ·
                          </span>
                          <span>{entry.actor_role}</span>
                        </>
                      ) : null}
                      <span className={styles.divider} aria-hidden="true">
                        ·
                      </span>
                      <time dateTime={entry.occurred_at_utc}>
                        {formatAuditTime(entry.occurred_at_utc)}
                      </time>
                      {entry.target_id ? (
                        <>
                          <span className={styles.divider} aria-hidden="true">
                            ·
                          </span>
                          <span className={styles.mono}>
                            {entry.target_type
                              ? `${entry.target_type} ${entry.target_id}`
                              : entry.target_id}
                          </span>
                        </>
                      ) : null}
                    </span>

                    {auditDetails(entry).length > 0 ? (
                      <dl className={styles.details}>
                        {auditDetails(entry).map((detail) => (
                          <div key={detail.label} className={styles.detail}>
                            <dt>{detail.label}</dt>
                            <dd>{detail.value}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </>
      )}
    </>
  );
}
