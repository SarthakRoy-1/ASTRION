# Product Note

This note covers what ASTRION is for, what it does today, and where it stops on
purpose.

Every figure, clause and document status below comes from the supplied
ParcelPilot source pack in `data/source/`, or from the running application.
Nothing is illustrative. [architecture.md](architecture.md) covers the
technical design; [demo-script.md](demo-script.md) walks through it in five
minutes.

- **Live:** <https://astrion-app.vercel.app/>. The one-click **Sign in to the
  demo** button is hidden from the public UI (`PUBLIC_DEMO_SIGN_IN_ENABLED` in
  `app/frontend/src/lib/features.ts`); the backend demo account is unchanged.
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
- **Confirmation-gated actions.** The agent can prepare an escalation, a ticket
  note or a service credit. A person confirms or rejects it, and the outcome
  is audited.
- **Operations.** A ranked, explained list of what needs attention across the
  workspace, detected from the records.
- **Documents.** The indexed source documents, each with its type, status,
  authority tier and chunks.
  - Admins can upload account-scoped documents and re-index them.
- **Workspace.** Members, roles and a hash-chained audit trail.

These are real results, taken from the live data:

| Ask | Answer | Why |
| --- | --- | --- |
| Can Northstar cancel `ORD-1001` without a fee? | **Yes, INR 0** | Northstar agreement §2 waives the fee for a `BOOKED` order before pickup and outranks SOP §1, which would charge it. |
| Can LumenWorks cancel `ORD-2001` without a fee? | **No, INR 250** | Same SOP rule, opposite result. LumenWorks' agreement §2 says no special waiver applies, so the SOP default stands. |
| Can `ORD-1002` be cancelled? | **No** | Status is `PICKED_UP`, so the SOP directs the return-to-origin workflow instead. |
| Does `ORD-2002` qualify for a failed-pickup credit? | **Yes, INR 300** | 4.50h past the window end, carrier at fault. LumenWorks §3 sets a 4h threshold and a fixed INR 300, replacing the SOP default. |
| Has `TKT-501` breached its first-response SLA? | **No verdict, conditional** | 30 minutes have elapsed. Northstar's agreement (tier 1) sets P1 15 minutes, P2 1 hour and P3 8 business hours, but the ticket has no severity. ASTRION lists the targets and asks for a severity instead of guessing one (see [Trust and reliability](#trust-and-reliability)). |

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

**Which role the demo uses.** The one-click demo signs in as the **operations**
member of the shared *ASTRION Demo* workspace. That identity can prepare an
action, confirm it, and read the audit trail.

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

The live demo workspace currently shows six signals. The top one is
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
    infers one from ticket text.
  - An earlier version tried to infer severity. It rated a billing question as
    P1 on a single shared word, and was removed.
- **Insufficient data.** When a needed record is missing, out of scope, or
  nothing relevant is found, the answer is **Not enough information**
  (`insufficient_data`), never a plausible guess.
  - For example, *"What is the weather in Mumbai today?"* returns no evidence
    and is reported exactly that way.
- **Citations.** Answers list their sources with file, page, section, tier and
  whether each is authoritative.
  - Every computed figure carries the rule and inputs that produced it.
- **Escalation.** A breached SLA, an unresolvable conflict, a policy decision
  that requires escalation, or a tool error recommends escalation.
  - The agent can prepare that escalation as an action.
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
   - the parameters still match the fingerprint they were shown;
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

1. **Persistent production storage.** The hosted backend is on an ephemeral
   disk. The demo rebuilds itself after a cold start, but confirmed actions and
   the audit trail do not survive one.
   - A managed database is the prerequisite for any real team using this.
2. **Action approval workflows.** A proposal is bound to the conversation that
   prepared it, so a support agent cannot hand a large credit to a manager for
   approval today.
   - A delegated approval queue would add assignment, notification and
     approve/reject, while keeping every existing check.
3. **Citation relevance.** Sources are listed per document section retrieved,
   so an answer can cite neighbouring sections that did not decide it.
   - The fix is to separate "decided the answer" from "retrieved alongside
     it", and cite the first prominently.
4. **Evaluation and feedback loops.** A regression suite of real support
   questions with expected decisions and trust statuses, run on every change.
   - Add per-answer "this was wrong" feedback from agents, fed back into that
     suite.
5. **Richer proactive detection.**
   - Scheduled rather than on-demand detection, with notifications.
   - Semantic rather than lexical ticket clustering.
   - Aggregating issued credits against agreement monthly caps. Credits are
     recorded, but the cap is reported, not totalled.
6. **Observability.** Traces across tool steps, latency and error dashboards,
   and alerting on trust-status distribution shifts. Today there is structured
   logging and audit events only.
7. **Stronger customer-facing authentication.** A proper customer portal with
   per-customer identity, if customer self-service is wanted, instead of the
   local-only demo personas.

## What was intentionally left out

- **Customer self-service in production.** ASTRION is an internal tool; the
  customer personas exist only locally to prove account isolation.
- **Severity inference.** Deciding whether a ticket is P1 is a business-impact
  judgement; ASTRION reports every candidate target instead of guessing.
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
- **Self-registration on the hosted deployment.** Verification requires email
  and there is no mail transport. The one-click demo account still exists on
  the backend, but its sign-in button is hidden from the public UI.

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
