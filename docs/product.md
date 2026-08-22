# Product Notes

> **Placeholder — Phase 0.** Scope and behavioural intent only. Concrete flows,
> role definitions, and worked examples are written once the source pack has
> been ingested and the real entities, roles, and policies are known. Nothing
> here invents assessment data.

## Who this is for

An authorised **internal ParcelPilot support / operations employee** — not an
end customer, and not the public. The user is assumed competent at their job and
under time pressure. They want a correct, sourced answer they can act on and
defend, not a paragraph of hedging.

This shapes the product:

- Show the source. A support agent quoting policy to a customer needs to know
  which document and page it came from.
- Show the arithmetic. "£240 credit" is unusable; "£240 = 4 breached days ×
  £60/day under clause 7.2 of the Northstar agreement" is defensible.
- Say "I don't know" clearly. A confident wrong answer about a service credit
  becomes a real refund to a real customer.

## What it does

- Answers natural-language questions about policy, accounts, orders, and tickets.
- Retrieves the governing document text and cites it.
- Looks up structured records, scoped to what the user is permitted to see.
- Computes SLA deadlines, cancellation charges, and service credits deterministically.
- Applies customer-specific agreements ahead of general policy where they apply.
- Matches reported symptoms against documented known issues.
- Prepares state-changing actions and executes them only after explicit confirmation.
- Escalates when it cannot answer safely.
- Surfaces relevant risks the user did not ask about.

## What it does not do

- Talk to customers directly.
- Change state without an explicit confirmation step.
- Answer outside the authorised user's account scope.
- Treat a deprecated policy as current.
- Treat a historical ticket resolution as authoritative — past handling may have
  been wrong, and reproducing a past mistake is a failure mode this product
  exists to prevent.
- Produce a number without showing where it came from.

## Behavioural commitments

**Cite or escalate.** Every substantive claim carries a source. No source, no claim.

**Precedence is visible.** When a customer agreement overrides general policy,
say so and name both. The user needs to understand *why* this customer is
treated differently.

**Confirmation is real.** The preview shows exactly what will change. Approving
it is a deliberate act, not a reflexive "yes".

**Uncertainty is a first-class answer.** "The current policy and the Northstar
agreement conflict here — escalate to the account manager" is a good outcome.

**Proactivity is bounded.** Flagging "this account has two other orders affected
by the same known issue" is useful. Volunteering unrelated observations is noise.

## To be defined in later phases

- Role taxonomy and per-role permissions.
- The specific state-changing actions the data supports.
- Escalation routing and thresholds.
- Concrete worked examples drawn from the actual source pack.
- Evaluation scenarios — including adversarial ones: deprecated-policy traps,
  cross-customer scope probes, and wrong-historical-ticket traps.
