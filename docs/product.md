# Product Note

This note covers what ASTRION is for, what it does today, and where it stops on
purpose.

Every figure, clause and document status below comes from the supplied
ParcelPilot source pack in `data/source/`, or from the running application.
Nothing is illustrative. [architecture.md](architecture.md) covers the
technical design; [demo-script.md](demo-script.md) walks through it in five
minutes.

- **Live:** <https://astrion-app.vercel.app/>. You register with an email
  address and password, verify it with an emailed six-digit code (or continue
  with Google or GitHub where configured), and create a workspace. There is no
  shared demo login on the hosted deployment: its PostgreSQL database refuses it
  (`/health` reports `demo_login_enabled: false`). The one-click demo remains
  for local development.
- **Data:** a new workspace holds the platform's four general documents and no
  customer records. The assessment snapshot is loaded into a workspace by an
  operator (see [persistence.md](persistence.md)); the examples below assume it
  has been.
- **Dataset snapshot:** all timing is measured against
  **2026-08-16 11:00 Asia/Kolkata**, never against today's date, so answers do
  not drift.

## Problem

ParcelPilot's support team answers questions that sound simple and are not:

- *Can this customer cancel without a fee?*
- *Is this late pickup owed a credit?*
- *Has this ticket breached its SLA?*

A correct answer means combining several sources:

- the current support policy;
- the cancellation and credit SOP;
- the product guide and its known issues;
- sometimes a signed customer agreement that overrides all of them;
- the customer's actual orders and tickets.

A wrong answer is not just a bad reply. It becomes a real fee charged, a real
credit paid, or an SLA miss nobody noticed.

The source pack contains these traps on purpose. Each is one a person under
time pressure, or a summarising chatbot, could plausibly get wrong:

| Trap in the pack | What a careless answer does | What ASTRION does |
| --- | --- | --- |
| `02_Support_Policy_v2_DEPRECATED.pdf` states an Enterprise P1 target of 1 hour, and v3 (current) states 30 minutes | Quotes the wrong target, since v2 matches a keyword search just as well | v2 is tier 4, non-authoritative by *status*, so it can never govern however well it matches |
| Northstar's agreement waives the SOP's cancellation fee for a `BOOKED` order before pickup | Charges the SOP's INR 250 fee | The signed agreement (tier 1) outranks the SOP for that account, and the answer names the override |
| `TKT-450`'s historical resolution says an INR 250 fee applied after 30 minutes | Repeats a past mistake as if it were policy | Historical resolutions are context only and never justify an answer |
| A SwiftShip order still reads `BOOKED` after collection (KI-211: webhooks up to 20 minutes late) | Tells the customer the pickup failed | Treats `BOOKED` inside the documented lag window as inconclusive, and says so |
| `TKT-504` says the driver already collected the parcel while Northstar's `ORD-1001` still reads `BOOKED` and a cancellation is requested | Cancels a parcel that has already left, because the order row says it can | Reads the open ticket, names it and KI-211, and returns *requires verification*: the fee waiver stands, cancelling is not authorised until the carrier is checked |
| `TKT-501` (every shipment creation fails) and `TKT-505` (a production API key posted publicly) never use the word "P1" | Treats them as ordinary tickets until someone asks about an SLA | Reads the response clock on every investigation and reports that each ticket matches a P1 clause in the current policy, as an indication for a person to verify, with escalation advised |

What the team needs is fast answers they can defend to the customer. That
means three things:

- the **source**, and whether it is current;
- the **arithmetic** behind any figure;
- a clear **"I don't know"** when the data does not settle the question.

Nothing should change in a customer's account until a person has approved it.

## Product

ASTRION is a support and operations agent over ParcelPilot's policies and
operational data. Today it provides the following:

- **Support (chat).** Answers natural-language questions about policy,
  accounts, orders, tickets and known issues.
  - Every answer shows which tools ran, the governing sources with page and
    section, a trust status, and any rule and calculation it applied.
- **Deterministic decisions.** Code computes three decisions, never the model:
  - cancellation fees;
  - failed-pickup service credits;
  - first-response SLA targets, with breach detection.
- **Agreement-aware precedence.** A customer's signed agreement is applied
  ahead of general policy for that account and topic only.
- **Ticket investigation.** Opening a ticket reads its first-response clock
  against the governing targets, indicates (never assigns) a severity when its
  text matches a definition in the current policy, checks the customer's orders
  for a pickup that may already have happened, and searches the documentation
  for what the ticket says.
- **Confirmation-gated actions.** The agent can prepare an escalation, a ticket
  note or a service credit. A person confirms or rejects it, and the outcome
  is audited.
- **Operations.** A ranked, explained list of what needs attention across the
  workspace, detected from the records.
- **Documents.** The indexed source documents, each with its type, status,
  authority tier and chunks.
  - Admins can upload account-scoped documents and re-index them.
- **Workspace.** Members, roles and a hash-chained audit trail.

These are real results, taken from the assessment snapshot as loaded into a
workspace:

| Ask | Answer | Why |
| --- | --- | --- |
| Can Northstar cancel `ORD-1001` without a fee? | **Fee waived (INR 0), but not yet confirmable** | Northstar agreement §2 waives the fee for a `BOOKED` order before pickup and outranks SOP §1. An open ticket, `TKT-504`, says the driver has already collected it, and KI-211 documents SwiftShip pickup confirmations up to 20 minutes late, so the answer is *requires verification*: confirm the carrier's status before cancelling. |
| Can LumenWorks cancel `ORD-2001` without a fee? | **No, INR 250** | Same SOP rule, opposite result. LumenWorks' agreement §2 says no special waiver applies, so the SOP default stands (75 minutes after booking against a 30-minute window). |
| Can `ORD-1002` be cancelled? | **No** | Status is `PICKED_UP`, so the SOP directs the return-to-origin workflow instead. |
| Does `ORD-2002` qualify for a failed-pickup credit? | **Yes, INR 300** | 4.50h past the window end, carrier at fault. LumenWorks §3 sets a 4h threshold and a fixed INR 300, replacing the SOP default. |
| Investigate `TKT-501` | **Matches the P1 definition; not a verdict** | 30 minutes have elapsed. The ticket's text matches "complete production outage preventing all shipment creation". If it is P1, Northstar's target is 15 minutes and 30 have elapsed. Severity is not set, no breach is asserted, and escalation is advised while it is verified. |
| `TKT-501` is a P1. Has it breached? | **Yes** | The stated severity is used: 30.00 minutes against Northstar's 15-minute P1 target. Escalate immediately (policy §4). |
| Investigate `TKT-505` | **Matches the P1 definition; not a verdict** | "Suspected credential exposure". If it is P1, the Enterprise default is 30 minutes (v3, not v2's 1 hour) and 150 have elapsed. |

## User contexts

ASTRION is built for **internal ParcelPilot support and operations staff**.
Under real authentication (what the hosted deployment runs), what a person may
do comes from their **role in a workspace**:

| Role | Ask the agent, view Operations and Documents | Prepare actions | Confirm and execute actions, read the audit trail | Approve credits above the manager threshold, manage documents and members |
| --- | --- | --- | --- | --- |
| `viewer` | ✓ | | | |
| `support` | ✓ | ✓ | | |
| `operations` | ✓ | ✓ | ✓ | |
| `admin` / `owner` | ✓ | ✓ | ✓ | ✓ |

**Which role you have.** Whoever creates a workspace is its **owner**, which
holds every permission. Other members join with the workspace code and password
or by invitation, at the role they are given. The local one-click demo signs in
as the **operations** member of a seeded workspace: it can prepare an action,
confirm it, and read the audit trail.

- A proposal is bound to the conversation of the person who prepared it.
- A support member can therefore prepare an action but not confirm one.
- Operations cannot approve a credit above the SOP's INR 1,000 manager
  threshold. That still needs `admin`.

**Account scope.** A workspace owns a set of customer accounts, and every
query is limited to them in SQL. A record outside the caller's scope is
reported exactly like a record that does not exist.

**Customer context.** Customer-facing access is **not** part of the hosted
product.

- The original assessment's identity directory (`AUTH_MODE=demo_header`) is
  local-only and refused in production. It includes two customer personas,
  `customer.northstar` (ACCT-001 only) and `customer.lumenworks` (ACCT-002
  only).
- Those personas can ask about their own account and cannot prepare or
  execute any action.
- They exist to demonstrate account isolation, not to be a support channel.

## Proactive issue detection

A support assistant that only answers questions helps only once someone thinks
to ask. The **Operations** view turns the workspace's tickets and orders into a
ranked, explained list of what needs attention.

**Detection and ranking are deterministic, rule-based code** with no model
involved. Four detectors run on demand:

| Detector | Fires when |
| --- | --- |
| SLA risk | An open ticket has no first response, measured against every computable first-response target. Agreement overrides are applied automatically. |
| Recurring issue | One account has two or more tickets sharing distinctive terms |
| Cross-customer issue | The same problem spans accounts, or one carrier misses pickups for several |
| Operational anomaly | Pickup windows closed with no pickup, or most orders carry a cancellation request |

Each signal comes with:

- the records it rests on;
- the accounts affected;
- any matching documentation;
- a trust status;
- a recommended next step;
- an **itemised priority**, for example `+40 severity critical`,
  `+15 sla_risk needs faster handling`, `−5 already documented`.

Two ranking rules keep the list honest:

- **Breadth amplifies severity but cannot replace it.** A widespread but minor
  pattern cannot outrank a missed SLA.
- **Unverified signals are ranked lower, never higher.**

On the assessment snapshot the workspace shows six signals. The top one is
`TKT-505 has no first response after 150.00 minutes` (critical, priority 52).
Next is `TKT-501` (conditional, because its severity is not recorded).

Recommended next steps are advisory. Acting on a signal goes through the agent
and the same confirmation gate as everything else.

## Trust and reliability

- **Authority.** Every document has a tier derived from its own type and
  in-document status:

  | Tier | Source | Governs? |
  | --- | --- | --- |
  | 1 | Signed customer agreement | For that customer's accounts only |
  | 2 | Current support policy | Yes |
  | 3 | Current SOP / product documentation | Yes |
  | 4 | Deprecated documents | Never; context only |

  - Relevance finds evidence; authority decides what governs.
  - Precedence is resolved per topic and per account.
  - When a higher tier overrides a lower one, the answer says so, for example
    *"Northstar agreement §2 (tier 1) outranks SOP §1 (tier 3) on topic
    'cancellation'"*.
- **Conflicts.** If two sources at the same tier disagree on a topic,
  precedence cannot settle it. The answer is marked as a conflict and escalated
  rather than resolved by picking one.
- **Uncertainty.** An answer that depends on something the data does not state
  is `conditional`, and names the premise to check.
  - Severity is the main case. Tickets carry no severity, and ASTRION never
    *sets* one from ticket text.
  - An earlier version chose a severity by word overlap. It rated a billing
    question as P1 on a single shared word, and was removed.
  - What it does now is *indicate*: it parses the current policy's own P1
    definition into its clauses and reports when the ticket's words cover enough
    of one ("complete production outage preventing all shipment creation";
    "suspected credential exposure"). The result says which clause and which
    document, says it is an indication for a person to verify, shows what the
    target would be if confirmed, and advises escalation because the policy
    directs that P1 incidents be escalated immediately. A ticket that says the
    affected thing still works, or matches too little, is not indicated.
  - A `BOOKED` order contradicted by an open ticket from the same customer
    (the driver has already been) is *requires verification*, not allowed. The
    fee position is still computed and reported, because it is not what is in
    doubt.
  - A ticket's historical resolution lowers the answer's trust from
    `confident`, because it is context that may be wrong and no answer may rest
    on it.
- **Insufficient data.** When a needed record is missing, out of scope, or
  nothing relevant is found, the answer is **Not enough information**
  (`insufficient_data`), never a plausible guess.
  - For example, *"What is the weather in Mumbai today?"* returns no evidence
    and is reported exactly that way.
- **Citations.** Answers list their sources with file, page, section, tier and
  whether each is authoritative.
  - Every computed figure carries the rule and inputs that produced it.
- **Escalation.** A breached SLA, a ticket that matches the P1 definition, an
  unresolvable conflict, a policy decision that requires verification, or a tool
  error recommends escalation.
  - The agent can prepare that escalation as an action, with a reason and
    evidence taken from the finding behind it (the clause matched, the target,
    the minutes elapsed, the sources cited), not the operator's own sentence.
    The severity is recorded on it only if a person stated one.
  - A question the sources simply cannot answer (a record not found, an
    unrelated question) is reported as insufficient data and does **not**
    recommend escalation: there is nothing to hand over.
  - Escalating is treated as a correct outcome, not a failure.

The trust status is computed in code from tool results (`agent/trust.py`), not
asserted by the model. There are five statuses, and the worst one wins. Each
implies a different next step for the reader: **proceed, check a premise,
reconcile sources, get the missing input, or hand over**.

## Confirmation-gated actions

```text
request  →  proposed action  →  explicit confirmation  →  execution  →  audit
```

1. **Request.** A user asks, for example *"Investigate TKT-501 and escalate it
   if the outage warrants it."*
2. **Proposal.** The agent's action tool writes a `pending_confirmation`
   proposal and nothing else. The UI shows exactly what would change.
   - For a service credit, the amount comes from the policy engine. The tool
     refuses an amount supplied by the model.
   - Typing *"yes, do it"* in chat confirms nothing, because the chat endpoint
     has no execution path.
3. **Confirmation.** The user presses **Confirm** or **Reject**, which is a
   separate API call. Before anything is written, the server checks:
   - the confirming user holds `execute_action`;
   - the proposal belongs to their conversation;
   - the request carries the fingerprint of the proposal that was reviewed
     (required; a confirmation that cannot say what it reviewed is refused) and
     the stored parameters still match it;
   - it has not expired and has not already been used, so a replay returns 409;
   - for credits, the decision is recomputed, and anything above INR 1,000
     requires `approve_high_value_action`.
4. **Execution.** Only then is the effect written, for example
   *Escalation created ESC-…*.
5. **Audit.** The workspace's hash-chained trail records `action.proposed`,
   then `action.executed`, `action.rejected` or
   `action.confirmation_refused`.
   - The trail is visible at **Workspace → View audit trail** to roles holding
     `read_audit_log`.

## What I would build next

*Future work. None of this is implemented.* Ordered by product value:

1. **Loading data without an operator.** Production is durable (PostgreSQL and
   object storage), but a new workspace is empty and the assessment snapshot
   goes in through an operator-run import.
   - A workspace-scoped import, or a guided way to create accounts, orders and
     tickets, would let a team use the product on day one.
2. **A model behind the same tools.** The hosted planner is rule-based. The
   OpenAI-backed planner exists and is tested with fakes; running it live needs
   a key and an evaluation of how well it classifies severity from the policy's
   definitions.
3. **Action approval workflows.** A proposal is bound to the conversation that
   prepared it, so a support agent cannot hand a large credit to a manager for
   approval today.
   - A delegated approval queue would add assignment, notification and
     approve/reject, while keeping every existing check.
4. **Citation relevance.** Sources are listed per document section retrieved,
   so an answer can cite neighbouring sections that did not decide it.
   - The fix is to separate "decided the answer" from "retrieved alongside
     it", and cite the first prominently.
5. **Evaluation and feedback loops.** A regression suite of real support
   questions with expected decisions and trust statuses, run on every change.
   - Add per-answer "this was wrong" feedback from agents, fed back into that
     suite.
6. **Richer proactive detection.**
   - Scheduled rather than on-demand detection, with notifications.
   - Semantic rather than lexical ticket clustering.
   - Aggregating issued credits against agreement monthly caps. Credits are
     recorded, but the cap is reported, not totalled.
7. **Observability.** Traces across tool steps, latency and error dashboards,
   and alerting on trust-status distribution shifts. Today there is structured
   logging and audit events only.
8. **Stronger customer-facing authentication.** A proper customer portal with
   per-customer identity, if customer self-service is wanted, instead of the
   local-only demo personas.

## What was intentionally left out

- **Customer self-service in production.** ASTRION is an internal tool; the
  customer personas exist only locally to prove account isolation.
- **Severity assignment.** Deciding whether a ticket is P1 is a business-impact
  judgement. ASTRION indicates where a ticket matches a definition and reports
  every candidate target, and a person decides.
- **Business-hours SLA breaches.** The pack defines no business calendar, so
  targets stated in business hours are reported but no breach is asserted.
- **Statistical anomaly detection or forecasting.** Six orders and seven
  tickets cannot support it, and the output could not be explained to the
  person acting on it.
- **Execution from chat.** By design there is no path from a chat message to a
  state change.
- **Model-computed figures.** Even with `LLM_PROVIDER=real`, the model chooses
  tools and writes prose. Every fee, credit and deadline comes from code.
- **Multi-turn conversation memory.** Each turn is answered from tools, and
  prior turns are not replayed to the model.
- **A shared demo login on the hosted deployment.** Everyone registers and
  gets a workspace of their own; PostgreSQL refuses the one-click demo.

## Success metric

**Policy-commitment correction rate.** This is the percentage of support cases
where a cancellation fee, service credit or SLA commitment given to a customer
is later reversed or amended because it was wrong.

**Why this metric:**

- It measures the exact failure ASTRION exists to prevent: the deprecated
  policy quoted, the agreement override missed, the credit mis-calculated.
- It is observable from operational records: fee reversals, credit amendments
  and reopened tickets.
- It cannot be gamed by answering faster or refusing more. A refusal that
  pushes the case to escalation does not reduce corrections, it only defers
  them.

**How to measure it:** take a baseline over a fixed period before rollout, then
compare the same period after, for cases where agents used ASTRION.

**Success:** a sustained reduction from that baseline, with no increase in
median time to first response.
