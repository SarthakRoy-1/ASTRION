"""How far an answer can be trusted, decided in code rather than by the model.

`ResponseOutcome` already says what *shape* a response has — answered,
uncertain, needs confirmation, refused, errored. That is a statement about the
interaction. It is not a statement about whether the answer can be relied on,
and the two come apart constantly: an answer can be perfectly well-formed and
rest on two sources that contradict each other.

This module adds the second axis. `TrustStatus` is derived deterministically
from what the tools actually returned — decisions, authority resolution,
retrieval failures — and never from prose, never from the model, and never
from how confident anything sounded.

    CONFIDENT          a governing source settled it, nothing outstanding
    CONDITIONAL        settled, but only if a stated premise holds
    CONFLICT           two sources of equal authority disagree
    INSUFFICIENT_DATA  the inputs needed to answer were missing
    ESCALATE           a human must decide

The ordering above is not cosmetic: `_PRECEDENCE` ranks them, and the worst
status present wins. An answer that is CONFIDENT about one thing and missing
data for another is INSUFFICIENT_DATA overall, because a caller acting on the
confident half would be acting on an incomplete answer.

**Why this is not a confidence score.** A number invites a threshold, a
threshold invites tuning, and tuning invites shipping "0.82 is probably fine".
These five states each imply a different *action* by the reader — proceed,
check the premise, reconcile the sources, get more data, involve a person —
which is what a support agent actually needs to know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.backend.models.agent import ToolStatus
from app.backend.models.documents import AuthorityTier
from app.backend.models.policy import PolicyOutcome


class TrustStatus(StrEnum):
    """How far the answer can be relied on. Derived, never asserted."""

    CONFIDENT = "confident"
    CONDITIONAL = "conditional"
    CONFLICT = "conflict"
    INSUFFICIENT_DATA = "insufficient_data"
    ESCALATE = "escalate"


#: Worst-wins ordering. Higher number means less usable without further work.
#: ESCALATE is highest because it is the only status that says the system
#: should stop and hand over, rather than that the reader should look closer.
_PRECEDENCE: dict[TrustStatus, int] = {
    TrustStatus.CONFIDENT: 0,
    TrustStatus.CONDITIONAL: 1,
    TrustStatus.INSUFFICIENT_DATA: 2,
    TrustStatus.CONFLICT: 3,
    TrustStatus.ESCALATE: 4,
}


def worst(statuses) -> TrustStatus:
    """The least trustworthy status present. CONFIDENT for an empty set."""
    collected = list(statuses)
    if not collected:
        return TrustStatus.CONFIDENT
    return max(collected, key=lambda s: _PRECEDENCE[s])


@dataclass(frozen=True)
class AuthoritySummary:
    """What governed the answer, and what it beat.

    Exists because this was previously knowable only by reading the composed
    prose or digging into a tool's `data` blob. A client that wants to render
    "a customer agreement overrode the standard policy" should not have to
    parse a sentence to find that out.
    """

    #: The strongest tier that actually governed, or None when nothing did.
    governing_tier: AuthorityTier | None = None
    governing_chunk_ids: tuple[str, ...] = ()
    #: True when a customer agreement decided it — the single most
    #: consequential fact about a support answer, and the one a reader is
    #: most likely to get wrong by assuming the default policy applied.
    customer_agreement_applied: bool = False
    #: Human-readable, already-composed notes from the authority layer.
    overrides: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    #: True when deprecated or otherwise non-authoritative material was
    #: retrieved. It never governs; saying it was *seen* is what lets an
    #: answer explain that a rule changed.
    non_authoritative_seen: bool = False

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)


@dataclass(frozen=True)
class TrustAssessment:
    """The full trust picture for one response."""

    status: TrustStatus
    authority: AuthoritySummary
    #: Why the status is what it is, in the order the reasons were found.
    #: Each entry is safe to show a user: they are drawn from tool messages
    #: and policy verification reasons, both of which are already user-facing.
    reasons: tuple[str, ...] = ()
    #: Set only when `status is ESCALATE`.
    escalation_reason: str | None = None

    @property
    def is_actionable(self) -> bool:
        """Whether it is safe to let a state-changing action be *offered*.

        CONFLICT and INSUFFICIENT_DATA are not actionable: proposing an
        executable action on evidence the system has just said it cannot
        reconcile would make the confirmation gate a rubber stamp on a guess.
        ESCALATE is excluded for the same reason — the point of escalating is
        that a person decides.
        """
        return self.status in (TrustStatus.CONFIDENT, TrustStatus.CONDITIONAL)


@dataclass
class _Findings:
    """Mutable accumulator, so the assessment reads as one pass over history."""

    statuses: list[TrustStatus] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    governing_tiers: list[AuthorityTier] = field(default_factory=list)
    governing_chunk_ids: list[str] = field(default_factory=list)
    customer_agreement: bool = False
    non_authoritative: bool = False
    escalation_reason: str | None = None

    def note(self, status: TrustStatus, reason: str | None = None) -> None:
        self.statuses.append(status)
        if reason and reason not in self.reasons:
            self.reasons.append(reason)


def _assess_decisions(history, findings: _Findings) -> None:
    """Read the deterministic policy engine's own verdicts.

    The policy layer already distinguishes a settled decision from one that
    needs verifying; this translates that vocabulary rather than second-guessing
    it. `REQUIRES_VERIFICATION` is exactly CONDITIONAL: the arithmetic is done
    and its inputs are not all confirmed.
    """
    for step in history:
        for decision in step.result.decisions:
            # `REQUIRES_VERIFICATION` is the only outcome the policy engine
            # uses to mean "computed, but an input is unconfirmed". The other
            # four (allowed / not_allowed / eligible / not_eligible) are
            # settled verdicts and contribute no trust signal of their own —
            # a firm "not eligible" is exactly as trustworthy as a firm "yes".
            outcome = getattr(decision, "outcome", None)
            if outcome is PolicyOutcome.REQUIRES_VERIFICATION:
                for reason in decision.verification_reasons:
                    findings.note(TrustStatus.CONDITIONAL, reason)
                if not decision.verification_reasons:
                    findings.note(
                        TrustStatus.CONDITIONAL,
                        "the policy engine reported this decision as provisional",
                    )

            # A settled decision can still demand a person: the current policy
            # directs that P1 incidents and breached response targets be
            # escalated rather than reported quietly. That is a property the
            # policy engine set, not an opinion formed here.
            if getattr(decision, "requires_immediate_escalation", False):
                reason = "the governing policy requires immediate escalation"
                findings.note(TrustStatus.ESCALATE, reason)
                findings.escalation_reason = findings.escalation_reason or reason
            if getattr(decision, "breached", None) is True:
                reason = "a response target was breached and must be escalated"
                findings.note(TrustStatus.ESCALATE, reason)
                findings.escalation_reason = findings.escalation_reason or reason

            findings.overrides.extend(
                o for o in decision.overrides if o not in findings.overrides
            )


def _assess_retrieval(history, findings: _Findings) -> None:
    """Read the authority layer's resolution out of the retrieval tool results.

    The search tool already returns `governing`, `contextual`, `overrides` and
    `conflicts`. This lifts them to the response level so a caller does not have
    to reach into a tool's payload to learn what governed.
    """
    for step in history:
        data = step.result.data or {}

        for entry in data.get("governing", []) or []:
            if not isinstance(entry, dict):
                continue
            tier = entry.get("authority_tier")
            if tier is not None:
                try:
                    parsed = AuthorityTier(int(tier))
                except (ValueError, TypeError):
                    parsed = None
                if parsed is not None:
                    findings.governing_tiers.append(parsed)
                    if parsed is AuthorityTier.CUSTOMER_AGREEMENT:
                        findings.customer_agreement = True
            chunk_id = entry.get("chunk_id")
            if chunk_id and chunk_id not in findings.governing_chunk_ids:
                findings.governing_chunk_ids.append(chunk_id)

        for note in data.get("overrides", []) or []:
            if isinstance(note, str) and note not in findings.overrides:
                findings.overrides.append(note)

        for note in data.get("conflicts", []) or []:
            if isinstance(note, str) and note not in findings.conflicts:
                findings.conflicts.append(note)
                findings.note(TrustStatus.CONFLICT, note)

        # Retrieved-but-outranked material is normal and not a trust signal on
        # its own. What matters is whether anything *non-authoritative* came
        # back, because that is what a reader might mistake for policy.
        for entry in data.get("contextual", []) or []:
            if isinstance(entry, dict) and entry.get("is_authoritative") is False:
                findings.non_authoritative = True


def _assess_tool_failures(history, findings: _Findings) -> None:
    """A tool that could not answer is a gap in the evidence, not a detail.

    NOT_FOUND and FORBIDDEN are deliberately treated the same way here, exactly
    as the record layer treats them: the answer is missing an input either way,
    and distinguishing them at this level would leak the difference the tool
    layer works to hide.
    """
    for step in history:
        result = step.result
        if result.status in (ToolStatus.NOT_FOUND, ToolStatus.FORBIDDEN):
            findings.note(
                TrustStatus.INSUFFICIENT_DATA,
                result.message or f"{step.tool_name} returned no usable result",
            )
        elif result.status is ToolStatus.ERROR:
            reason = result.message or f"{step.tool_name} failed"
            findings.note(TrustStatus.ESCALATE, reason)
            findings.escalation_reason = findings.escalation_reason or reason
        elif result.status is ToolStatus.UNCERTAIN:
            findings.note(
                TrustStatus.CONDITIONAL,
                result.message or f"{step.tool_name} reported uncertainty",
            )
        elif result.status is ToolStatus.INVALID_INPUT:
            findings.note(
                TrustStatus.INSUFFICIENT_DATA,
                result.message or f"{step.tool_name} could not be called correctly",
            )


def assess(
    history,
    *,
    unmet_requirements: list[str] | None = None,
    step_budget_exhausted: bool = False,
) -> TrustAssessment:
    """Derive the trust assessment for one completed investigation.

    `unmet_requirements` are prerequisites the composer identified as missing —
    a policy question asked with no order to evaluate, say. They are the
    clearest INSUFFICIENT_DATA signal there is, because the system knows
    precisely what it would need.

    A truncated investigation (`step_budget_exhausted`) is INSUFFICIENT_DATA
    rather than CONFIDENT: the loop stopped with work still queued, so the
    evidence set is incomplete by construction, whatever it happens to contain.
    """
    findings = _Findings()

    _assess_decisions(history, findings)
    _assess_retrieval(history, findings)
    _assess_tool_failures(history, findings)

    for requirement in unmet_requirements or []:
        findings.note(TrustStatus.INSUFFICIENT_DATA, requirement)

    if step_budget_exhausted:
        findings.note(
            TrustStatus.INSUFFICIENT_DATA,
            "the investigation stopped at its step limit and may be incomplete",
        )

    status = worst(findings.statuses)

    # An unresolvable conflict is escalated rather than merely reported. The
    # authority layer has already established that precedence *cannot* settle
    # it, so there is no further computation that would help — only a person.
    if status is TrustStatus.CONFLICT:
        status = TrustStatus.ESCALATE
        findings.escalation_reason = findings.escalation_reason or (
            findings.conflicts[0]
            if findings.conflicts
            else "sources of equal authority disagree"
        )

    authority = AuthoritySummary(
        governing_tier=min(findings.governing_tiers) if findings.governing_tiers else None,
        governing_chunk_ids=tuple(findings.governing_chunk_ids),
        customer_agreement_applied=findings.customer_agreement,
        overrides=tuple(findings.overrides),
        conflicts=tuple(findings.conflicts),
        non_authoritative_seen=findings.non_authoritative,
    )

    return TrustAssessment(
        status=status,
        authority=authority,
        reasons=tuple(findings.reasons),
        escalation_reason=(
            findings.escalation_reason if status is TrustStatus.ESCALATE else None
        ),
    )
