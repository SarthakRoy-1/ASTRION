# Product Notes

What this product is, who it is for, what it actually does today, and where it
deliberately stops. Every figure, clause and document status below is quoted
from the supplied source pack in `data/source/` — nothing here is illustrative
or invented.

## Who this is for

An authorised **internal ParcelPilot support or operations employee**. The
system also ships two external customer contexts (scoped to one account each),
but the product is designed around the internal user: competent at their job,
under time pressure, and accountable for what they tell a customer.

That shapes three commitments:

- **Show the source.** An agent quoting policy to a customer needs the document
  and page it came from, and needs to know whether that document is current.
- **Show the arithmetic.** "INR 300 credit" is unusable on its own. "INR 300
  fixed credit — pickup 4.50h past the window end against LumenWorks'
  agreed 4h threshold, carrier at fault, customer not at fault" is defensible.
- **Say "I don't know" clearly.** A confident wrong answer about a service
  credit becomes a real refund to a real customer.

## The problem this exists to solve

The supplied pack contradicts itself on purpose, and every contradiction is one
a human support agent could plausibly get wrong under time pressure:

| Trap in the pack | What a careless answer does | What this product does |
| --- | --- | --- |
| `02_Support_Policy_v2_DEPRECATED.pdf` states an Enterprise P1 target of 1 hour; `01_Support_Policy_v3_CURRENT.pdf` states 30 minutes | Quotes the wrong target — v2 matches a keyword search just as well | v2 is non-authoritative by *status*, so it can never govern regardless of how well it matches |
| Northstar's agreement replaces the Enterprise P1 target with 15 minutes | Applies the plan default to a customer who negotiated better terms | The signed agreement outranks general policy for that account, and the override is named in the answer |
| `TKT-450`'s historical resolution says a INR 250 cancellation fee applied after 30 minutes | Reproduces a past mistake as if it were policy | Historical resolutions are flagged context-only and can never justify an answer |
| A SwiftShip order still reads `BOOKED` after collection (KI-211: webhooks up to 20 minutes late) | Tells the customer the pickup failed, or cancels a parcel already collected | Treats a `BOOKED` status inside the documented window as inconclusive, and says so |

## What it does today

All of the following is implemented, tested, and runs with no API key.

- **Answers natural-language questions** about policy, product behaviour,
  accounts, orders, tickets, and known issues.
- **Retrieves governing document text and cites it** — file, page, and section
  path, with the authority tier attached.
- **Looks up structured records** — accounts, orders, tickets, and the dataset
  snapshot — scoped in SQL to what the caller may see.
- **Computes three decisions deterministically**, never in the model:
  cancellation fees, failed-pickup service credits, and first-response SLA
  targets with breach detection.
- **Applies signed customer agreements ahead of general policy**, per account
  and per topic, and names the override.
- **Prepares state-changing actions** — an escalation or a ticket note — and
  executes them only through a separate confirmation call.
- **Declares uncertainty** rather than guessing, and recommends escalation when
  the documents say to.
- **Shows its work**: which tools ran, what each returned, and the evidence
  behind every claim.

### Worked examples, all verifiable against the pack

| Ask | Answer | Why |
| --- | --- | --- |
| Can Northstar cancel `ORD-1001` without a fee? | **Yes, INR 0.00** | Booked 09:00, cancellation requested 11:00 — 120 minutes, well past the SOP's 30-minute free window. The SOP would charge INR 250; Northstar's agreement §2 waives the fee for any `BOOKED` shipment before pickup "regardless of how long ago the shipment was booked", and outranks it. |
| Can LumenWorks cancel `ORD-2001` without a fee? | **No — INR 250** | Same SOP rule, opposite outcome. LumenWorks' agreement §2 states "No special cancellation-fee waiver applies", so the default stands. The two accounts differ because their agreements differ, not because of any rule naming them. |
| Can `ORD-1002` be cancelled? | **No** | Status is `PICKED_UP`. The SOP directs the return-to-origin workflow instead. |
| Does `ORD-2002` qualify for a failed-pickup credit? | **Yes — INR 300** | 4.50h past the window end, carrier at fault, customer not at fault. LumenWorks' agreement §3 sets a 4h threshold and a fixed INR 300, replacing the SOP's default 2h / lower-of-INR-500-or-10%. |
| Has `TKT-501` breached its first-response SLA? | **Yes** | Opened 10:30, dataset snapshot 11:00 — 30 minutes elapsed against Northstar's agreed P1 target of "15 minutes, 24x7", which replaces the Enterprise default of 30 minutes. |
| Has `TKT-505` breached its first-response SLA? | **Yes** | ACCT-004 has no agreement in the pack, so the Enterprise default of "30 minutes, 24x7" applies. 150 minutes elapsed. The same question, resolved from a different source, with no cross-customer bleed. |

All timing is measured against the dataset's own snapshot —
**2026-08-16 11:00 Asia/Kolkata** — never against today's date, so these answers
do not drift.

## Defensive and proactive behaviour that ships

The product does **not** have a proactive issue-detection engine. There is no
background scan, no risk dashboard, and no component that volunteers unrelated
observations. That was scoped out, and the honest framing is that it is future
work (see below).

What does ship is a set of *defensive surfaces* — points where the system
raises something the user did not ask about, because a specific document told
it to:

- **Monthly cap warning.** A credit decision on an account whose agreement caps
  monthly credits reports the cap and prompts a check of credits already issued
  — Northstar's agreement §3 caps them at INR 5,000.
- **Manager-approval threshold.** A credit above INR 1,000 is flagged as
  requiring manager sign-off, per the SOP §3, and — since the product issues
  credits — enforced at confirmation time. See
  [Roles](#roles-and-what-each-may-do).
- **Historical-resolution caution.** Any resolved ticket carrying a past
  resolution is surfaced with an explicit warning that it is context, not
  policy, and may be wrong.
- **Stale pickup status.** A `BOOKED` order past its pickup window is flagged
  before cancellation, and when the order's carrier matches a documented
  confirmation-lag known issue and the elapsed time is still inside that
  window, the answer cites the issue by name rather than assuming the pickup
  failed.
- **Related orders on the account.** A ticket investigation also resolves the
  account's other orders, so "are other shipments affected?" is answerable from
  the same response.
- **P1 escalation.** A P1 is flagged for immediate escalation whether or not its
  target has elapsed, and a breached target recommends escalation on its own.

The distinction matters: each of these is traceable to a clause in the pack. A
general-purpose "surface anything interesting" feature would be the kind of
unbounded proactivity that becomes noise.

## Trying it

The hosted deployment runs real authentication, so there is a published demo
account rather than a persona picker. Three accounts are seeded at three roles
— support, operations and owner — into one shared workspace holding the
supplied synthetic dataset. They are ordinary accounts: they sign in at the
ordinary endpoint, and every control applies to them unchanged.

Three things a visitor should know before acting:

- **The workspace is shared.** What one visitor confirms, the next one sees.
- **The records are synthetic.** Fictional accounts, orders, tickets and
  agreements. No real customer data exists in this system at all.
- **The actions are real inside it.** Confirming a credit issues one and
  writes an audit entry. Faking that would demonstrate nothing.

Registering your own account works, but cannot complete on a hosted
deployment: verification is required and there is no mail transport, so the
link reaches nobody. That is stated plainly in the interface rather than
hidden behind a "check your inbox" that would be false.

## Roles and what each may do

Below is the **demo identity directory** used by `AUTH_MODE=demo_header`, the
original assessment mode, which is available locally and refused in
production. Under real authentication (the default, and what the hosted
deployment runs) roles come from workspace membership instead — see
[docs/SECURITY.md](SECURITY.md).

| Identity | Role | Account scope | May change state | May approve a large credit |
| --- | --- | --- | --- | --- |
| `support.agent` | `support_agent` | all accounts | yes | **no** |
| `support.manager` | `support_manager` | all accounts | yes | yes |
| `support.readonly` | `read_only` | all accounts | **no** | **no** |
| `customer.northstar` | `customer` | ACCT-001 only | **no** | **no** |
| `customer.lumenworks` | `customer` | ACCT-002 only | **no** | **no** |

`support_manager` is now distinguishable from `support_agent`: the SOP's rule
that "any individual credit above INR 1,000 requires manager approval" is
computed by the policy engine *and enforced* when a credit is confirmed. Both
internal staff roles may prepare one; only a manager may confirm one above the
threshold, and the check is made against whoever is confirming, at the moment
they confirm.

Under real authentication the same rule is expressed as a permission rather
than a role: `approve_high_value_action`, granted from `admin` upwards, so the
person who signs off a large credit is not the person who confirms every small
one.

## What it does not do

- Talk to customers directly. It is an internal assistant, and the customer
  contexts exist to demonstrate account isolation, not to be a support channel.
- Change any state without an explicit, separate confirmation. Typing "yes,
  do it" into the chat endpoint confirms nothing — that endpoint has no
  execution path at all.
- Answer outside the caller's account scope. An out-of-scope record is reported
  exactly as a non-existent one, so the API is not an existence oracle for
  another customer's data.
- Treat a deprecated policy as current, or a historical ticket resolution as
  authoritative.
- Produce a figure without the rule, the inputs, and the citation behind it.
- Invent a business calendar. Targets stated in business hours are reported,
  but no breach is asserted from them, because the pack defines no business
  calendar anywhere.
- Decide a ticket's severity. See below.

## Two deliberate refusals

**Severity is never inferred.** The SLA engine computes a target and a breach,
but will not decide whether a ticket is P1, P2 or P3 — that is a judgement
about business impact. Asked without a severity, it reports the elapsed time
and every candidate target and asserts no breach.

This was built and then removed. An earlier version scored ticket text against
the policy's severity definitions and took the best match; on this corpus it
rated a billing question ("Cancellation fee after 30 minutes") as P1 on a
single shared word and then announced a breach against a 15-minute target. Word
overlap is not evidence of business impact, and a breach verdict is exactly the
kind of claim that must not rest on a guess.

**A missing pickup confirmation is not, by itself, doubt.** KI-211 documents a
20-minute SwiftShip webhook lag. That lag is matched against the *order's own
carrier* and its stated window — so it cannot be borrowed to withhold a credit
that a signed agreement grants on a different carrier's shipment, hours past
any documented window, with carrier fault already accepted.

## Behavioural commitments

**Cite or escalate.** Every substantive claim carries a source. No source, no
claim.

**Precedence is visible.** When an agreement overrides general policy, the
answer names both sides and the topic. The user needs to understand *why* this
customer is treated differently.

**Confirmation is real.** The preview shows exactly what will change, execution
is a separate call with a closed `approve`/`reject` vocabulary, and the action
is re-validated under the confirming user before anything is written.

**Uncertainty is a first-class answer.** "The applicable target is stated in
business hours and no business calendar is defined — confirm before committing"
is a good outcome, not a failure.

**Deterministic figures are authoritative.** The model chooses which tools to
call and explains the result; it never computes a number a customer would see.
See [architecture §10.12](architecture.md#1012-what-authority-the-models-prose-carries).

## Future work

Scoped out deliberately, in rough order of value:

1. **Hosting.** The deployment configuration is verified end to end; no
   platform is chosen. Blocked behind real authentication for a public URL.
2. **Real authentication.** The identity-acceptance step is a mock. Everything
   downstream of it is real and was built to be independent of how identity is
   established — see [README](../README.md#before-deploying-this-publicly-authentication-is-still-a-mock).
3. **Aggregating issued credits against the monthly cap.** Credits are now
   issued and recorded, but a decision still reports the agreement's cap
   without totalling what has already been paid against it.
4. **A designed proactive detector** — matching ticket symptoms to known issues
   unprompted, and flagging other orders on an account affected by the same
   issue. The retrieval and record layers this needs already exist.
5. **Conversation memory across turns.** A session is issued and binds prepared
   actions, but no prior turn is replayed to the model.
6. **Prose validation in real mode** — asserting that every figure in a
   model-authored answer appears in the structured decisions beside it.
