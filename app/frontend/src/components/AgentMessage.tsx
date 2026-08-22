"use client";

import { ActionCard } from "./ActionCard";
import { DecisionCard } from "./DecisionCard";
import { EvidenceSection } from "./EvidenceSection";
import { InvestigationSummary } from "./InvestigationSummary";
import { UncertaintyNotice } from "./UncertaintyNotice";
import { mayChangeState } from "@/lib/types";
import type { ActionProgress } from "@/hooks/useConversation";
import type { ChatResponse, Role } from "@/lib/types";

import styles from "./AgentMessage.module.css";

/**
 * One agent turn, assembled from the structured response.
 *
 * The order is the order a reader needs: the answer, then what could not be
 * settled, then any decision's arithmetic, then the action awaiting them, then
 * the provenance and the investigation behind it all. Conclusions first,
 * supporting material below — never the reverse.
 *
 * Every section is driven by a field the backend actually returned. A response
 * with no evidence renders no Sources block rather than an empty one, because
 * an empty citation list invites the reader to assume the list was merely
 * collapsed.
 */
export function AgentMessage({
  response,
  action,
  role,
  onRespondToAction,
}: {
  response: ChatResponse;
  action: ActionProgress;
  role: Role;
  onRespondToAction: (decision: "approve" | "reject") => void;
}) {
  const proposal = response.proposed_action;
  const uncertain = response.outcome === "uncertain";

  return (
    <article className={styles.message} data-outcome={response.outcome}>
      <header className={styles.header}>
        <span className={styles.author}>ParcelPilot agent</span>
        {response.reference_time && (
          <span className={styles.reference} title="Business decisions are judged against the dataset snapshot, not today's date.">
            As of {formatReferenceTime(response.reference_time)}
          </span>
        )}
      </header>

      <div className={styles.answer}>
        {splitParagraphs(response.answer).map((paragraph, index) => (
          <p key={index}>{paragraph}</p>
        ))}
      </div>

      {uncertain && (
        <UncertaintyNotice
          reasons={response.uncertainties}
          escalationRecommended={response.escalation_recommended}
        />
      )}

      {response.policy_decisions.map((decision) => (
        <DecisionCard
          key={`${decision.decision_type}-${decision.order_id}`}
          decision={decision}
        />
      ))}

      {proposal && (
        <ActionCard
          proposal={proposal}
          progress={action}
          canConfirm={mayChangeState(role)}
          onRespond={onRespondToAction}
        />
      )}

      {response.step_budget_exhausted && (
        <p className={styles.truncated}>
          The investigation reached its step limit and may be incomplete.
        </p>
      )}

      <EvidenceSection sources={response.sources} />
      <InvestigationSummary tools={response.tools_used} />
    </article>
  );
}

/**
 * The backend composes its answer as newline-separated statements. Rendering
 * them as paragraphs preserves that structure; collapsing them into one block
 * would run a rule, a calculation and a citation together into a wall of text.
 */
function splitParagraphs(answer: string): string[] {
  return answer
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function formatReferenceTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
