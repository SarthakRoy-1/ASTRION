# Demo Video Script (about 5 minutes)

A recording plan for the ASTRION submission video. It demonstrates the
engineering decisions, not just the screens.

Every prompt, label and result below was checked against the live deployment:
<https://astrion-app.vercel.app/>.

## Before recording

- **Show the demo button.** The public UI no longer offers **Sign in to the
  demo**; it is hidden behind `PUBLIC_DEMO_SIGN_IN_ENABLED` in
  `app/frontend/src/lib/features.ts`. Record from a build with the flag set
  to `true`. The steps below assume it.
- **Wake the backend.** Open the app and press **Sign in to the demo** once,
  1–2 minutes before recording.
  - The API sleeps when idle, and a cold start can take several seconds.
- **Start clean.** Press **New conversation** so the transcript is empty.
- **Prepare tabs:**
  1. Support, the live app;
  2. [architecture.md §0.2](architecture.md#02-diagram), showing the diagram;
  3. Documents;
  4. Operations.
- **Browser setup.** Use a 1440×900 window at 100% zoom.
- **Keep the audit trail recent.** The demo workspace is shared and the hosted
  disk is ephemeral, so earlier visitors' entries may or may not be there.
  Confirm a fresh action on camera (3:30) so the newest audit entries are
  yours.

---

## 0:00–0:30 · What ASTRION is

**Screen:** the sign-in page, then press **Sign in to the demo**.

> "This is ASTRION, a support and operations agent for ParcelPilot. Support
> agents answer questions like *can this customer cancel for free?* or *is
> this late pickup owed a credit?* To get it right they have to combine a
> support policy, an SOP, a product guide, sometimes a signed customer
> agreement that overrides all of them, and the customer's actual orders.
>
> A chatbot that summarises those documents will eventually quote the
> deprecated policy or promise a credit nobody approved. ASTRION is built so
> that it can't. One click signs me in as the operations member of a shared
> demo workspace. No password is involved, and none is in the frontend."

---

## 0:30–1:15 · Architecture

**Screen:** the architecture diagram tab (§0.2). Point to each layer as it is
named.

> "The core principle: **LLM reasoning proposes and coordinates.
> Deterministic application logic makes the policy-sensitive decisions.
> Authorization is enforced below the model. State-changing actions require
> explicit confirmation.**
>
> A request reaches the FastAPI backend with a session. The session fixes the
> workspace, the role and the account scope. None of those come from the model
> or the request body.
>
> The orchestrator runs a bounded tool loop over twelve tools:
>
> - record lookups;
> - authority-ranked document search;
> - deterministic policy engines for fees, credits and SLAs;
> - action tools that can only *prepare* a change.
>
> Account scope is compiled into SQL, so out-of-scope data never reaches the
> planner. A separate trust layer grades every answer from the tool results.
>
> The planner sits behind one interface. It can be an OpenAI model. This
> deployment runs the deterministic planner, which the sidebar shows, and it
> has exactly the same authority: none over the decisions."

---

## 1:15–2:00 · A normal support question

**Screen:** Support. Click the starter prompt *"Is ORD-2002 eligible for a
failed pickup service credit?"*

**Show, in order:**

1. The **question** in natural language.
2. The **answer**: eligible, **INR 300**.
3. **Rule applied** and **Calculation**. The window was missed by 4.50 hours
   with the carrier at fault, and the LumenWorks agreement §3 sets a 4-hour
   threshold and a fixed INR 300.
4. The **Investigation** steps:
   - *Looked up the record* (structured lookup);
   - *Applied the service-credit rules* (deterministic policy);
   - *Searched the document set* (retrieval).
5. **Sources.** Expand the governing LumenWorks agreement citation, which shows
   the page and section.

> "The agent looked up the order in the database, ran the service-credit
> engine, and retrieved the governing clause.
>
> The figure isn't generated text. It comes from code, with its inputs and
> citation attached. The credit tool actually refuses an amount supplied by the
> model."

---

## 2:00–2:45 · Source precedence

**Screen:** Support. Click *"Can Northstar cancel ORD-1001 without a
cancellation fee? Explain why."*

**Show:**

- The answer: **no cancellation fee** (fee waived, 0).
- **Rule applied:** a signed customer agreement waives the fee for a `BOOKED`
  order before pickup, overriding the SOP's default fee.
- **Precedence:** *Northstar agreement §2 (tier 1, customer agreement)
  outranks SOP §1 (tier 3, current operational doc) on topic 'cancellation'.*
- The trust notice saying a customer agreement governed.

> "ORD-1001 was booked two hours before cancellation. The SOP's free window is
> thirty minutes, so by the SOP this costs a fee. But Northstar's signed
> agreement waives it.
>
> Retrieval ranks sources by authority, not just relevance:
>
> 1. signed customer agreement;
> 2. current support policy;
> 3. current SOP and product documentation;
> 4. deprecated documents as context only.
>
> Precedence is per topic and per account. The same question for LumenWorks,
> whose agreement doesn't waive the fee, correctly returns INR 250. The answer
> names the override, so the agent can tell the customer *why*."

---

## 2:45–3:30 · Trust and reliability

**Screen:** Support. Type *"What is the weather in Mumbai today?"*

**Show:**

- The answer: *"I could not find enough information in the supplied sources to
  answer that."*
- The trust chip: **Not enough information**.
- The investigation step: *Searched the document set*, which found no
  evidence.
- No Sources block, because there is nothing to cite.

> "A system that must always answer will make something up. Here, trust status
> is computed in code from the tool results, not claimed by the model.
>
> A turn with no records and no document evidence **cannot** be marked
> confident. It is reported as not enough information.
>
> The same mechanism gives *Conditional* when a premise is unknown. Ask whether
> TKT-501 breached its SLA, and ASTRION lists the agreed P1, P2 and P3 targets
> but won't assert a breach without a severity it would otherwise have to
> guess. When two equally authoritative sources conflict, it escalates instead
> of picking one."

*Optional if time allows: show the TKT-501 SLA question for about 10
seconds.*

---

## 3:30–4:20 · A state-changing action

**Screen:** Support, then **New conversation**. Click *"Investigate TKT-501 and
escalate it if the outage warrants it."*

**Show, in order:**

1. **Request.** The prompt.
2. **Proposed action.** The card shows **Awaiting confirmation**:
   - "Create an escalation against ticket TKT-501…";
   - the account, ACCT-001;
   - its cited evidence.
3. **Explicit confirmation.** Press **Confirm escalation**.
4. **Execution.** The card now reads **Escalation created. ESC-…**
5. **Audit.** Open **Workspace → View audit trail**. The newest entries are
   *Prepared an action for confirmation* and *Confirmed and executed an
   action*, and the page shows *chain verified*.

> "The chat endpoint has no execution path. The agent's action tool only writes
> a proposal, and typing 'yes, do it' confirms nothing.
>
> Confirm is a separate API call, and the server re-checks everything before
> writing:
>
> - this role holds execute permission;
> - it's my conversation;
> - the parameters still match what I was shown;
> - it hasn't expired;
> - it's single use, so a replay gets a 409.
>
> A credit over the SOP's INR 1,000 threshold would still be refused for this
> operations role, because it needs a manager.
>
> Every step lands in an append-only, hash-chained audit trail."

---

## 4:20–4:45 · Documents

**Screen:** Documents.

**Show:**

- Support Policy v3 **CURRENT**.
- Support Policy v2 **DEPRECATED**.
- The SOP v4 and the Product Operations Guide, both **CURRENT**.
- The Northstar and LumenWorks agreements, both **ACTIVE** and each tied to one
  account.

Open one document to show its chunks.

> "These are the six source documents. Status is read from each document
> itself, and it decides the authority tier:
>
> - the agreements are tier 1, and only for their own account;
> - v3 policy is tier 2;
> - the SOP and product guide are tier 3;
> - v2 is deprecated.
>
> v2 is still indexed, so ASTRION can explain that a rule changed, but it can
> never govern an answer. Upload and delete need document-management
> permission. The source pack itself can't be deleted."

---

## 4:45–5:00 · Operations and takeaway

**Screen:** Operations, *What needs attention*.

**Show:** six ranked signals. The top one is *TKT-505 has no first response
after 150.00 minutes*, critical, priority 52. Expand its itemised priority.

> "Operations doesn't wait for a question. Deterministic detectors rank SLA
> risk, recurring issues, cross-customer issues and anomalies, and every point
> of priority is itemised.
>
> The takeaway: **the model coordinates, code decides, authorization sits below
> the model, and nothing changes without a person's confirmation.**"

---

## Timing summary

| Time | Segment | Key proof point |
| --- | --- | --- |
| 0:00–0:30 | What ASTRION is | The problem is conflicting sources and real money |
| 0:30–1:15 | Architecture | Principle; scope in SQL; planner has no decision authority |
| 1:15–2:00 | Normal question | Lookup + policy engine + retrieval → cited INR 300 |
| 2:00–2:45 | Source precedence | Northstar agreement §2 (tier 1) overrides SOP §1 (tier 3) |
| 2:45–3:30 | Trust | *Not enough information* instead of a guess |
| 3:30–4:20 | Action | Proposal → Confirm → ESC-… → audit, chain verified |
| 4:20–4:45 | Documents | CURRENT / DEPRECATED / ACTIVE drive authority |
| 4:45–5:00 | Operations | Six deterministic, itemised, ranked signals |

## If something goes wrong on camera

- **The first request is slow.** The backend is waking. Keep talking over the
  architecture section, which does not depend on it.
- **Signal count or order differs.** Signals are computed from the workspace's
  current records. Describe what is on screen rather than the numbers above.
- **The audit trail shows other visitors' entries.** The demo workspace is
  shared. Point to your own `create_escalation` entry by its time.
