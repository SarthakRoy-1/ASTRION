"use client";

import { ActionCard } from "./ActionCard";
import { AnswerNotes } from "./AnswerNotes";
import { DecisionCard } from "./DecisionCard";
import { EvidenceSection } from "./EvidenceSection";
import { InvestigationSummary } from "./InvestigationSummary";
import { UncertaintyNotice } from "./UncertaintyNotice";
import {
  decisionSubject,
  parseAnswer,
  undisplayedNotes,
  withoutActionRestatement,
} from "@/lib/presentation";
import { mayChangeState } from "@/lib/types";
import type { ActionProgress } from "@/hooks/useConversation";
import type { ChatResponse, Role } from "@/lib/types";

import styles from "./AgentMessage.module.css";

/**
 * One agent turn, assembled from the structured response.
 *
 * The order is the order a reader needs: the conclusion, then what could not
 * be settled, then any decision's arithmetic, then the action awaiting them,
 * then the provenance and the investigation behind it all. Conclusions first,
 * supporting material below — never the reverse.
 *
 * The backend composes its answer as a conclusion followed by labelled
 * `Rule applied:` / `Calculation:` / `Source:` / `Precedence:` lines. Rendered
 * inline at equal weight those lines outnumbered the conclusion roughly eight
 * to one on an SLA answer, and every one of them is already rendered
 * structurally below — on the decision card, or in the evidence section. So
 * they are separated out and kept one click away rather than deleted: the
 * agent said them, and a reader checking the working must still find them.
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
  const parsed = parseAnswer(response.answer);

  // A prepared action's answer only restates the proposal. The action card
  // below says the same thing better, and without the internal action id.
  const lead = withoutActionRestatement(parsed.lead, proposal);
  // Anything the decision card or the evidence section already renders is
  // dropped here rather than repeated in a second, flatter form.
  const notes = undisplayedNotes(
    parsed.notes,
    response.policy_decisions,
    response.sources,
  );

  return (
    <article className={styles.message} data-outcome={response.outcome}>
      <h2 className="visually-hidden">ParcelPilot agent response</h2>

      <header className={styles.header}>
        <span className={styles.author}>ParcelPilot agent</span>
        {response.reference_time && (
          <span className={styles.reference}>
            As of {formatReferenceTime(response.reference_time)}
          </span>
        )}
      </header>

      {lead.length > 0 && (
        <div className={styles.answer}>
          {lead.map((paragraph, index) => (
            <p key={index} className={index === 0 ? styles.conclusion : undefined}>
              {paragraph}
            </p>
          ))}
        </div>
      )}

      {uncertain && (
        <UncertaintyNotice
          reasons={response.uncertainties}
          escalationRecommended={response.escalation_recommended}
        />
      )}

      {response.policy_decisions.map((decision) => (
        <DecisionCard
          key={`${decision.decision_type}-${decisionSubject(decision)}`}
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

      <AnswerNotes notes={notes} />
      <EvidenceSection sources={response.sources} />
      <InvestigationSummary tools={response.tools_used} />
    </article>
  );
}

function formatReferenceTime(iso: string): string {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}
