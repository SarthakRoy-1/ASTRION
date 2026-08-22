"use client";

import {
  ACTION_STATE_LABELS,
  actionOutcomeMessage,
  actionTypeLabel,
} from "@/lib/presentation";
import { StatusPill } from "./StatusPill";
import type { ApiError } from "@/lib/client";
import type { ActionProgress } from "@/hooks/useConversation";
import type { ProposedActionView } from "@/lib/types";

import styles from "./ActionCard.module.css";

/**
 * The confirmation gate, as the operator sees it.
 *
 * Three properties this component is responsible for:
 *
 * - **Nothing has happened yet.** While the state is `pending_confirmation`
 *   the card says so in words, not just in styling. The backend guarantees it;
 *   the UI must not imply otherwise.
 * - **One approval, one request.** Both buttons disable the moment either is
 *   submitted. The backend makes execution single-use regardless, but a UI
 *   that lets a second click through and then reports `action_not_pending`
 *   blames the user for its own race.
 * - **The reviewed proposal is the executed one.** The `parameter_fingerprint`
 *   the API returned with this preview is echoed back on confirmation, so a
 *   proposal that changed underneath is refused rather than run.
 *
 * Only fields the API exposes for review are shown, and none are editable —
 * an operator approves what the agent proposed, or rejects it.
 */
export function ActionCard({
  proposal,
  progress,
  canConfirm,
  onRespond,
}: {
  proposal: ProposedActionView;
  progress: ActionProgress;
  canConfirm: boolean;
  onRespond: (decision: "approve" | "reject") => void;
}) {
  const pending = progress.state === "pending_confirmation";
  const submitting = progress.submitting !== null;

  return (
    <section className={styles.card} data-state={progress.state} aria-labelledby="action-heading">
      <header className={styles.header}>
        <h3 id="action-heading" className={styles.heading}>
          {pending ? "Proposed action" : "Action"}
        </h3>
        <StatusPill tone={stateTone(progress.state)}>
          {ACTION_STATE_LABELS[progress.state]}
        </StatusPill>
      </header>

      <p className={styles.summary}>
        {actionTypeLabel(proposal.action_type)}{" "}
        <span className={styles.target}>{proposal.target_id}</span>
      </p>

      <dl className={styles.details}>
        {proposal.account_id && (
          <div className={styles.detailRow}>
            <dt>Account</dt>
            <dd className={styles.mono}>{proposal.account_id}</dd>
          </div>
        )}
        {proposal.reason && (
          <div className={styles.detailRow}>
            <dt>Reason</dt>
            <dd>{proposal.reason}</dd>
          </div>
        )}
        {proposal.evidence_chunk_ids.length > 0 && (
          <div className={styles.detailRow}>
            <dt>Evidence</dt>
            <dd>
              {proposal.evidence_chunk_ids.length} cited{" "}
              {proposal.evidence_chunk_ids.length === 1 ? "source" : "sources"} — see
              Sources below
            </dd>
          </div>
        )}
      </dl>

      {pending && (
        <>
          <p className={styles.notice}>
            Nothing has been changed yet. This runs only when you confirm it.
          </p>

          {canConfirm ? (
            <div className={styles.actions}>
              <button
                type="button"
                className={styles.confirm}
                onClick={() => onRespond("approve")}
                disabled={submitting}
              >
                {progress.submitting === "approve"
                  ? "Confirming…"
                  : `Confirm ${confirmVerb(proposal.action_type)}`}
              </button>
              <button
                type="button"
                className={styles.reject}
                onClick={() => onRespond("reject")}
                disabled={submitting}
              >
                {progress.submitting === "reject" ? "Rejecting…" : "Reject"}
              </button>
            </div>
          ) : (
            <p className={styles.blocked}>
              Your current context cannot approve state-changing actions. An
              internal support agent or manager must confirm this.
            </p>
          )}
        </>
      )}

      {!pending && (
        <p className={styles.outcome} data-state={progress.state}>
          {actionOutcomeMessage(proposal.action_type, progress.state)}
          {progress.executed?.result?.escalation_id && (
            <span className={styles.receipt}>
              {progress.executed.result.escalation_id}
            </span>
          )}
          {progress.executed?.result?.note_id && (
            <span className={styles.receipt}>{progress.executed.result.note_id}</span>
          )}
        </p>
      )}

      {progress.error && <ActionError error={progress.error} />}
    </section>
  );
}

function ActionError({ error }: { error: ApiError }) {
  return (
    <p className={styles.error} role="alert">
      <span className={styles.errorLabel}>Could not complete</span>
      {error.message}
    </p>
  );
}

function stateTone(state: string) {
  if (state === "executed" || state === "confirmed") return "ok" as const;
  if (state === "pending_confirmation") return "caution" as const;
  if (state === "rejected") return "caution" as const;
  return "fail" as const;
}

function confirmVerb(actionType: string): string {
  return actionType === "create_escalation" ? "escalation" : "note";
}
