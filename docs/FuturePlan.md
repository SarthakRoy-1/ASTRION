# ParcelPilot — Future Product & Technical Plan

This document is a prioritised list of what I would build **beyond** the
current assessment submission. Every item below is genuinely unbuilt today —
none of it should be read as a description of existing behaviour. Where the
current system already does something adjacent, that is stated explicitly so
the boundary between "shipped" and "planned" stays unambiguous.

The order is deliberate: each item is placed where it is because of what it
depends on and what it unblocks, not because of novelty. See
[docs/architecture.md §13 — Deferred decisions](architecture.md#13-deferred-decisions)
and [docs/product.md — Future work](product.md#future-work) for the source
list this plan expands on, and the README's
[Think Beyond the Immediate Requirements](../README.md#think-beyond-the-immediate-requirements)
section for the condensed version of the same reasoning.

---

## 1. Production Authentication and Authorization

**What would be built.** A real identity provider — OAuth/OIDC, or signed
API tokens issued out-of-band — behind `resolve_principal`
(`app/backend/api/dependencies.py`), replacing the fixed identity directory
in `auth/principals.py`. `AUTH_SECRET_KEY` and `AUTH_TOKEN_TTL_MINUTES` are
already reserved in `.env.example` for this and are unused today. Alongside
it: internal support roles with real, enforced permission boundaries (not
just today's flat "may this identity change state at all"), and continued
account-level scoping for the two customer contexts.

**Why it matters.** The [live deployment](../README.md#live-deployment) is a
real, working system, but anyone who can reach it can call the API as
`support.manager` or any of the other four demo identities simply by naming
one — no credential is checked. That is documented, not hidden (see
[Before deploying this publicly](../README.md#before-deploying-this-publicly-authentication-is-still-a-mock)).
Until identity is trustworthy, nothing downstream of it — durable memory,
expanded actions, an audit trail with real accountability — is safe to build
on top of, because all of it would inherit a self-asserted identity as its
foundation.

**Why this priority.** Everything else on this list either reads customer
data across time (item 2), changes more state (item 4), or records who did
what (item 5). Each of those becomes meaningfully riskier, not just
incrementally riskier, if the "who" underneath them can be spoofed by naming
a string in a header. This is the one item that gates every other item.

**What foundation enables it.** `AgentContext` (user, role,
`allowed_account_ids`) is already the seam the rest of the system consumes,
and it was built to be independent of how identity is established —
authorization, account scoping, source precedence and the confirmation gate
are all enforced below the model, in SQL and in `policies/`, and none of
that code reads how identity was established. Replacing `auth/principals.py`
with a real provider is confined to one module; retrieval, policy
evaluation and the action state machine need no change. Model instructions
were never part of this boundary and are not being asked to become part of
it — a prompt is not an enforcement mechanism, and this system's scoping was
built specifically to not depend on the model behaving.

**Risk reduced.** Identity spoofing and unauthorised cross-account access on
a public host.

**Category.** Trust/reliability (primary), engineering.

---

## 2. Durable Customer-Scoped Conversation Memory

**What would be built.** Persistent conversation history, stored per
customer/account, with prior turns replayed into the tool-calling loop on
request — and, critically, **re-authorization on every restore**: when a
stored conversation is reopened, the system re-checks that the requesting
identity's current scope still covers that conversation's account, rather
than trusting that it did at creation time.

**Why it matters — and how this differs from today.** `POST /api/chat`
already issues and echoes a `session_id`, and the frontend already has a
conversation list per customer context — but that `session_id` binds
*prepared actions*, not model memory, and the frontend's list is a
client-side transcript of past responses. No prior turn is replayed to the
model; each request is investigated from scratch. Durable memory is a
different, larger claim: that a stored turn can be safely handed back to the
model as context on a later, possibly-different request.

**Why this priority.** It sits directly behind item 1 because the hard part
isn't storage — it's what happens when a conversation crosses a boundary: a
support agent reopening a thread days later, an identity's permissions
changing between the original turn and the restore, or (in the worst case) a
account boundary being crossed if the storage key were ever wrong. Replaying
a transcript into a tool-calling loop without deciding what scope a stored
turn carries would risk quietly reintroducing the exact class of
cross-account leak that Phase 7's adversarial pass tested for and found
nothing on. Retention and privacy also need an explicit answer here — how
long a conversation is kept, and whether a customer-side conversation is
retained differently from an internal one — which is a policy decision, not
just an engineering one, and is easier to get right once identity (item 1)
is real.

**What foundation enables it.** The `session_id` correlation and the
per-context conversation storage the frontend already exercises are the
shape this extends. The boundary to add is authorization *on read* — the
storage model itself does not need to be reinvented.

**Risk reduced.** Cross-conversation / cross-account data exposure from
memory that outlives the authorization context it was created under.

**Category.** Trust/reliability (primary), product (agent usability).

---

## 3. Proactive Issue Detection

**What would be built.** A background detection layer, separate from the
request/response path, that surfaces:

- recurring complaints across tickets for the same account or the same root
  cause,
- multiple tickets tracing to the same product/known issue,
- high-severity tickets approaching or past their SLA target,
- unusual order or support patterns on an account, and
- incidents whose symptoms match across more than one customer.

**Why it matters — and what exists today instead.** The current system does
**not** have a proactive detection engine. There is no background scan, no
risk dashboard, and nothing that volunteers an observation the caller did
not ask about in that request. What ships today is a narrower thing:
*defensive surfaces* — a monthly-credit-cap warning, the manager-approval
flag, a historical-resolution caution, a stale-pickup flag, related-orders
lookup on a ticket investigation, and P1 escalation flagging (see
[docs/product.md — Defensive and proactive behaviour that ships](product.md#defensive-and-proactive-behaviour-that-ships)).
Every one of those is triggered *within* an answer to a specific question,
by a specific document rule — none of them run unprompted, and none of them
look across tickets or accounts. Proactive detection is a genuinely new
capability, not an extension of what already exists.

**Why this priority.** It is additive rather than foundational — it needs no
change to retrieval, the record layer, or the policy engine, only a new
scheduled pass over data those layers already expose — which is why it can
come after the trust items rather than before them. It is placed ahead of
expanded actions (item 4) because detecting a pattern is lower-risk than
acting on one, and a detector is far more useful once durable history (item
2) gives it more than a single request's worth of context to look across.

**What foundation enables it.** `search_documents`' known-issue matching, the
account-scoped `lookup_record` layer, and the tier/authority model in
`retrieval/authority.py` are the exact primitives a scheduled detector would
run against — no new data path, just a new caller and a new schedule.

**The design constraint that has to hold.** Every finding a detector
surfaces must trace to a specific clause, record, or known-issue document,
the same standard the existing defensive surfaces already meet. A
general-purpose "surface anything unusual" feature is the explicit failure
mode to avoid — it becomes noise a support agent learns to ignore, which is
worse than not building it at all.

**Risk reduced.** Slow human discovery of cross-ticket patterns and
multi-customer incidents; missed SLA risk that a single-request investigation
would never surface.

**Category.** Product (primary), operational value.

---

## 4. Controlled Operational Actions

**What would be built.** Additional state-changing actions beyond today's
three (`create_escalation`, `add_ticket_note`, `issue_service_credit` — the
third shipped in Phase 5) — updating a ticket, creating a follow-up task, and
assigning or re-escalating ownership are the remaining candidates. Every one of them goes through the
same `prepare_*` → confirm pipeline that exists today: a preview call that
writes nothing, and a separate, explicit confirmation call that actually
executes. For financially meaningful actions specifically — a credit above
the SOP's stated threshold is the clear case — confirmation must require the
`support_manager` role, not merely any identity that may change state at
all.

**Why it matters.** The SOP's rule that "any individual credit above INR
1,000 requires manager approval" is already **computed and displayed** by
`policies/service_credit.py` today — but it is not enforced, because neither
action that exists today issues a credit, so there is nothing for the
threshold to gate. `support_manager` and `support_agent` are therefore
identical in capability today (see
[docs/product.md — Roles and what each may do](product.md#roles-and-what-each-may-do)).
A credit-issuing action is what makes that distinction real rather than
theoretical.

**Why this priority.** It comes after the trust foundation (item 1) and the
usability foundation (item 2) deliberately: this is the first item on this
list where real money or a real customer-facing state change is on the
other end of a confirmation click, and that should not be the first thing
built on top of a still-mock identity layer.

**Why action authority stays separate from agent reasoning.** This is not a
new principle for this item — it is the same one the system already
enforces: the model chooses *which* tool to call, but it never computes the
number a customer would see, and it can never reach execution on its own.
`confirm_action` is an orchestrator method, deliberately absent from every
tool registry a natural-language request could reach — a request, however
urgently phrased, can at most produce a *proposal*. A fourth or fifth action
type gets the identical shape: prepared, previewed, expiring, single-use,
and re-validated under the *confirming* caller at execution time — never a
shortcut that lets a new action execute on the model's own confidence.

**What foundation enables it.** The confirmation architecture this would
plug into is already built and already generalises — three action types exist
today, and the third was added in Phase 5 without touching it. A fourth is a
new `prepare_*` tool, one effect writer, and (only if it carries a financial
threshold) one authorization check in the confirm endpoint; the state machine,
expiry, re-validation, and single-use execution all carry over unchanged.

**Risk reduced.** An unauthorised or unreviewed financial action; a support
role distinction that exists in name but not in enforcement.

**Category.** Product (primary), trust/reliability.

---

## 5. End-to-End Auditability

**What would be built.** A durable, queryable audit trail spanning the
authenticated identity, the customer/account context, every retrieved
source, every tool call, every policy decision, every proposed action, every
confirmation (or rejection), and every executed state change — one record
per meaningful event, not just the current action's own row.

**Why it matters, and the frame to hold this in.** This is the natural
evolution of items 1 and 4, not a separate initiative:
**identity → authorization → auditability.** Authentication answers "who is
this," authorization answers "what may they do," and auditability answers
"what did they actually do." Today, `GET /api/actions/{action_id}` gives an
audit record for *one action's own lifecycle* — who initiated it, what was
requested, what evidence it cited, when it was prepared, confirmed, and
executed. That is real, but it is scoped to a single action, not a durable,
cross-action trail a support organisation could query.

**Why this priority.** It matters most exactly when items 1 and 4 exist —
real identity and real financial actions are what make "who did this,
provably" a requirement rather than a nice-to-have. Building a full audit
system ahead of real identity would mean auditing a self-asserted string,
which is not meaningfully different from not auditing at all.

**What foundation enables it.** Every action already carries a fingerprint,
an expiry, and a confirming-caller re-validation step (see
[Prepare and confirm an action](../README.md#prepare-and-confirm-an-action)).
The state machine already produces the events an audit trail would record —
today they simply are not persisted anywhere beyond the action's own row.

**Risk reduced.** Inability to reconstruct who authorised a given change, or
prove it, after the fact.

**Category.** Trust/reliability (primary), operational value.

---

## 6. Streaming Investigation and Better Operational UX

**What would be built.** A streaming transport (SSE or websocket) exposing
the orchestrator's actual tool-by-tool progress as it happens, so the
existing `AgentActivity` component fills in rows live instead of
rendering a completed investigation after one blocking response. Alongside
that: clearer intermediate evidence as it's found, a more structured
source/provenance display, and richer action previews before confirmation.

**Why it matters.** A multi-step investigation — document search, record
lookup, policy evaluation — can take a few real seconds. Today's UI is
honest about this rather than faking it: the tool-activity panel renders
every step the orchestrator actually took, it is just rendered after the
fact, because `POST /api/chat` answers in one response and does not stream.
Showing that progress live is a better experience for the same reason a
progress bar beats a spinner.

**Why this is placed after items 1–5.** This is explicitly UX and transport,
not trust or correctness — nothing about scoping, precedence, or the
confirmation gate depends on it. It is deliberately ordered behind the trust
and authorization foundations: a demo that streams a self-asserted
identity's investigation live is not more trustworthy than one that
doesn't, and polish should follow substance, not substitute for it.

**What foundation enables it.** `AgentActivity` is already shaped to
receive incremental rows; the orchestration loop already emits a discrete
internal event per tool call (today's `tools_used[]` response field is built
from exactly those events, just collected rather than streamed). The work
is a streaming response path over the existing loop, not a change to what
the loop does.

**Risk reduced.** None directly — this is a usability improvement, not a
safety boundary.

**Category.** Product/UX (primary), engineering.

---

## 7. Broader Evaluation and Real-Mode Validation

**What would be built.** An evaluation program beyond the current pytest and
Vitest suites: broader natural-language query coverage (not just the
assessment's worked examples), adversarial and conflicting-source test
cases, continued customer-isolation and authorization tests as new tools are
added, action-confirmation tests for each new action type, a regression
dataset that grows over time, evaluation specifically against the
`LLM_PROVIDER=real` (OpenAI-backed) path, and explicit tests for how the
system behaves on genuinely unsupported or uncertain questions.

**Why it matters.** In `LLM_PROVIDER=deterministic` — the default, and what
the entire test suite runs on — the answer's prose is assembled by
`agent/composer.py` directly from typed tool-result fields, so it cannot
disagree with the decision it describes. In `LLM_PROVIDER=real`, that
guarantee does not hold the same way: the model writes the answer text, and
a prompt is an instruction, not an enforcement mechanism. In principle, a
transcription slip or an over-confident paraphrase could put a figure in the
prose that does not match the structured decision rendered beside it.
Nothing in the current build detects that specific case today — though
authorization, scoping, precedence and the confirmation gate are entirely
unaffected by it, since none of them read the answer string. See
[docs/architecture.md §10.12](architecture.md#1012-what-authority-the-models-prose-carries)
for the full statement of this residual risk. The natural hardening is to
assert that every monetary figure in a model-authored answer also appears in
`policy_decisions[]`, and fall back to the deterministic composer's prose
when it does not — deliberately not built yet, because it is a real check
with real false-positive design work behind it (percentages, dates, and
record IDs all look like figures), and a half-tuned validator that silently
rewrites answers would be worse than the documented gap it replaces.

**Why this priority.** It is last because it is the narrowest-scope,
lowest-likelihood risk on this list, and because it only applies to the
optional real-model path — the default, tested path has no such gap by
construction. That does not make it unimportant: it is what turns "we
believe this still holds" into something measured every time a tool, a
document, or a policy rule changes, and it is the natural counterpart to
everything above — a broader action set (item 4) and a real audit trail
(item 5) both deserve tests that keep pace with them.

**What foundation enables it.** `policy_decisions[]` is already the
authoritative, UI-rendered source of truth, and the deterministic composer
that would provide the real-mode fallback already exists and already runs
by default. Prose validation is a check inserted at one point in
`AgentOrchestrator.handle`, not new infrastructure — and the existing
pytest/Vitest suites are the base a broader regression dataset extends
rather than replaces.

**Risk reduced.** An undetected mismatch between a real-mode model's prose
and the deterministic decision it is describing; regressions in
authorization or isolation as new tools and actions are added over time.

**Category.** Engineering (primary), trust/reliability.

---

## What Is Deliberately Not Claimed as Implemented

To keep this document unambiguous: **none of the following exist in the
current submission.** Each is future work, in the priority order above.

- **Production authentication.** Today's identity is a mock — a
  self-asserted string resolved against a fixed directory, with no token, no
  signature, and no session.
- **Production authorization beyond today's scope.** Account scoping and the
  confirmation gate are real and enforced in code, but role-based permission
  granularity beyond "may this identity change state at all" does not exist
  — `support_manager` and `support_agent` are identical in capability today.
- **Durable production conversation memory.** No prior turn is replayed to
  the model on any request today; `session_id` binds prepared actions, not
  memory.
- **Full proactive issue detection.** No background scan, risk dashboard, or
  cross-ticket/cross-account pattern detector exists. Only request-triggered
  defensive surfaces exist today.
- **Expanded operational actions.** Three action types exist today:
  `create_escalation`, `add_ticket_note` and `issue_service_credit`. No
  ticket-update, task-creation, or reassignment action exists.
- **Externally witnessed audit.** A durable, hash-chained, workspace-scoped
  trail exists and is readable at `/workspace/audit`, but it is
  tamper-*evident* rather than tamper-proof: anyone with write access to the
  database could recompute the chain forward. Making that impossible needs a
  witness outside the box — an append-only log service, or periodic
  publication of the head hash — along with retention and export policy.
- **Streaming investigation UX.** `POST /api/chat` answers in one blocking
  response today. No SSE/websocket transport or live-updating investigation
  view exists.
- **Broader real-model/evaluation infrastructure.** Today's test suites
  cover the deterministic planner and the assessment's scenarios; no
  standing real-mode prose-validation check, adversarial regression dataset,
  or continuous evaluation pipeline exists yet.

If any of the above is ever described elsewhere as "coming soon" or
"planned," this document — not marketing language — is the source of truth
for what that means: not implemented, not scheduled, and not started beyond
the design reasoning captured here.

---

## How This Extends the Assessment

The current submission already establishes the foundation this plan builds
on, not a placeholder for it:

- **Document retrieval** — authority-ranked search over the supplied policy,
  SOP, and agreement pack, with page and section citations on every claim.
- **Structured operational lookup** — accounts, orders, tickets, and the
  dataset snapshot, scoped in SQL to what the caller may see.
- **State-changing action preparation** — `prepare_escalation` and
  `prepare_ticket_note`, each producing a proposal and nothing else.
- **Confirmation before state changes** — execution is unreachable by the
  model; it exists only behind a separate, explicit confirmation call that
  re-validates under the confirming caller.
- **Source precedence** — a signed customer agreement outranks the current
  support policy, which outranks current product documentation; historical
  tickets are context only, never authority.
- **Uncertainty handling** — severity is never inferred, business-hours
  targets are reported rather than converted into a deadline, and an
  incomplete term set produces `REQUIRES_VERIFICATION` rather than an
  invented figure.
- **Customer isolation** — account scope is enforced below the model, and an
  out-of-scope record is indistinguishable from a missing one.
- **Deterministic calculation where it matters** — cancellation fees,
  service credits, and SLA targets/breach are computed in
  `app/backend/policies/`, never composed in model prose.

Every item in this plan is additive to that foundation, not a replacement
for it. Real authentication (item 1) swaps out one module
(`auth/principals.py`) without touching the scoping, precedence, or
confirmation logic beneath it. Durable memory (item 2) extends the existing
per-context conversation storage rather than redesigning it. Expanded
actions (item 4) reuse the exact `prepare_*` → confirm shape the two
existing actions already prove out. The architectural split this system was
built around — **the model reasons, code decides** — does not change for
any item on this list; each one is scoped specifically so that it doesn't
have to.
