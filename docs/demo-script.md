# Demo Video Script (about 5 minutes)

A recording plan for the ASTRION submission video. It demonstrates the
engineering decisions, not just the screens.

The prompts and the results below are what the agent returns on the supplied
assessment snapshot (measured at 2026-08-16 11:00 Asia/Kolkata), which is checked
by the test suite. Re-check them on the deployment you record against, because
they depend on which workspace holds that data.

## Before recording

- **Have a workspace that holds the snapshot.** The hosted deployment has no
  shared demo login, and a new workspace is empty (it has only the platform's
  four general documents). An operator loads the snapshot into the workspace you
  will record in:

  ```powershell
  python scripts/ingest_dataset.py   --org-id <workspace-id>
  python scripts/ingest_documents.py --org-id <workspace-id>
  ```

  These run against the production `DATABASE_URL` and replace only that
  workspace's rows. Do not record on a workspace that has not had them, or every
  question will be answered *not found*.
- **Use a real account.** Register at <https://astrion-app.vercel.app/> with an
  address that can receive mail (the mail sender is Resend; on its testing sender
  only the Resend account's own address receives codes), enter the emailed code,
  and create or join the prepared workspace. Sign in ahead of time so the first
  request is not on camera.
- **Wake the backend.** The API sleeps when idle. Open the app and ask any
  question 1–2 minutes before recording.
- **Start clean.** Press **New conversation** so the transcript is empty.
- **Prepare tabs:**
  1. Support, the live app;
  2. [architecture.md §0.2](architecture.md#02-diagram), showing the diagram;
  3. Documents;
  4. Operations.
- **Browser setup.** Use a 1440×900 window at 100% zoom.
- **Role.** The owner can confirm actions and read the audit trail. A support
  member can prepare an action but not confirm it.

---

## 0:00–0:30 · What ASTRION is

**Screen:** the landing page, then the signed-in Support page.

> "This is ASTRION, a support and operations agent for ParcelPilot. Support
> agents answer questions like *can this customer cancel for free?* or *is
> this late pickup owed a credit?* To get it right they have to combine a
> support policy, an SOP, a product guide, sometimes a signed customer
> agreement that overrides all of them, and the customer's actual orders and
> tickets.
>
> A chatbot that summarises those documents will eventually quote the
> deprecated policy or promise a credit nobody approved. ASTRION is built so
> that it can't. I signed in with an emailed code, like any user, and this is
> my workspace."

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
> planner. Data lives in PostgreSQL, original files in object storage. A
> separate trust layer grades every answer from the tool results.
>
> The planner sits behind one interface. It can be an OpenAI model. This
> deployment runs the deterministic planner, which `/health` reports, and it has
> exactly the same authority: none over the decisions."

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

## 2:00–2:50 · Source precedence, and doubt

**Screen:** Support. Click *"Can Northstar cancel ORD-1001 without a
cancellation fee? Explain why."*

**Show:**

- The answer: the fee is **waived**, but cancelling **cannot be confirmed yet**.
- **Rule applied:** a signed customer agreement waives the fee for a `BOOKED`
  order before pickup, overriding the SOP's default fee.
- **Precedence:** *Northstar agreement §2 (tier 1, customer agreement)
  outranks SOP §1 (tier 3, current operational doc) on topic 'cancellation'.*
- The **Verify** line: an open ticket, TKT-504, says the driver already
  collected the parcel, and KI-211 documents SwiftShip confirmations up to 20
  minutes late.
- The trust notice: *Conditional*, and the verdict card reading *Uncertain*.

> "ORD-1001 was booked two hours before the cancellation request. The SOP's
> free window is thirty minutes, so by the SOP this costs a fee. Northstar's
> signed agreement waives it, and the answer says which source won and why.
>
> But look at what else it found: an open ticket from the same customer says the
> driver already collected this parcel while the order still reads BOOKED. The
> fee isn't in doubt. Whether the parcel is still there is. So it doesn't
> authorise the cancellation. It names the conflict and asks for a carrier check.
>
> Precedence is per topic and per account. The same question for LumenWorks,
> whose agreement doesn't waive the fee, returns INR 250."

---

## 2:50–3:30 · Trust and severity

**Screen:** Support. Type *"Investigate TKT-501 and tell me what to do."* Then
*"What is the weather in Mumbai today?"*

**Show for TKT-501:**

- The investigation ran the **response-clock** step without anyone saying "SLA".
- *The ticket's text matches the current policy's P1 definition* ("Complete
  production outage preventing all shipment creation"), as **an indication to
  verify, not a classification**.
- *If it is P1, the target is 15 minutes and 30 have elapsed*, from Northstar's
  agreement, which replaces the plan default.
- Escalation advised. No breach asserted, and nothing prepared.

**Show for the weather question:**

- *"I could not find enough information in the supplied sources to answer
  that."* The trust chip: **Not enough information**. No escalation is
  suggested, because there is nothing to hand over.

> "Severity is a business judgement, so the system won't set it. An earlier
> version did, and rated a billing question P1 on one shared word, so it was
> deleted. What it does now is say which clause of the policy the ticket
> resembles, what the target would be, and that a person has to verify.
> If I *state* the severity, 'TKT-501 is a P1', it computes: breached, 30 minutes
> against 15, escalate immediately.
>
> Trust is computed in code from the tool results. A turn with no evidence
> cannot be confident, and a ticket that carries an old resolution can't be
> either, because that resolution is context that may be wrong."

---

## 3:30–4:20 · A state-changing action

**Screen:** Support, then **New conversation**. Click *"Investigate TKT-501 and
escalate it if the outage warrants it."*

**Show, in order:**

1. **Request.** The prompt.
2. **Proposed action.** The card shows **Awaiting confirmation**:
   - "Create an escalation against ticket TKT-501…";
   - a reason that quotes the finding (the P1 clause, the target, the minutes),
     not the sentence I typed;
   - the account, ACCT-001, and its cited evidence.
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
> - the request carries the fingerprint of the proposal I reviewed, and the
>   stored parameters still match it. Without it the request is refused;
> - it hasn't expired;
> - it's single use, so a replay gets a 409.
>
> A credit over the SOP's INR 1,000 threshold would need a manager.
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
> never govern an answer. The four general documents are shared by every
> workspace and can't be deleted; agreements and uploads belong to a workspace,
> and uploading needs document-management permission."

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
> the model, doubt stops a state change, and nothing changes without a person's
> confirmation.**"

---

## Timing summary

| Time | Segment | Key proof point |
| --- | --- | --- |
| 0:00–0:30 | What ASTRION is | The problem is conflicting sources and real money |
| 0:30–1:15 | Architecture | Principle; scope in SQL; planner has no decision authority |
| 1:15–2:00 | Normal question | Lookup + policy engine + retrieval → cited INR 300 |
| 2:00–2:50 | Precedence and doubt | Agreement waives the fee; an open ticket stops the cancellation |
| 2:50–3:30 | Trust and severity | P1 indicated, never assigned; *Not enough information* instead of a guess |
| 3:30–4:20 | Action | Proposal → Confirm (with fingerprint) → ESC-… → audit, chain verified |
| 4:20–4:45 | Documents | CURRENT / DEPRECATED / ACTIVE drive authority |
| 4:45–5:00 | Operations | Six deterministic, itemised, ranked signals |

## If something goes wrong on camera

- **The first request is slow.** The backend is waking. Keep talking over the
  architecture section, which does not depend on it.
- **Every question says "not found".** The workspace has no data. Load the
  snapshot (see *Before recording*), or switch to the workspace that has it.
- **No verification email arrives.** Check the Resend dashboard for the send. On
  the testing sender only the Resend account's own address receives mail, and an
  address that already has an account is emailed a notice instead of a code (sign
  in with its password to get a real code).
- **Signal count or order differs.** Signals are computed from the workspace's
  current records. Describe what is on screen rather than the numbers above.
- **The audit trail shows other members' entries.** It is the workspace's. Point
  to your own `create_escalation` entry by its time.
