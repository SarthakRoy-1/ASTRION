# Architecture

> **Current through Phase 5.** Sections 1–6 state the constraints the design
> had to satisfy, written before the source pack was ingested; sections 7–10
> record what each phase actually built and why. Where an early section was
> refined by later evidence, the later section says so and wins. Section 11
> lists what remains deliberately open.

## 1. The non-negotiable split

The single most important structural decision: **the LLM reasons, code decides.**

| Owned by the LLM | Owned by deterministic Python |
| --- | --- |
| Understanding the question | Authorization / role checks |
| Choosing which tools to call | Account scoping of every query |
| Sequencing multi-step tool use | Source precedence resolution |
| Reasoning over retrieved evidence | SLA calculation |
| Explaining an answer in prose | Cancellation fee calculation |
| Judging when it is uncertain | Service-credit calculation |
| Drafting an action for review | Input validation |
| | Executing any state change |

Consequences to hold to:

- A monetary figure or SLA deadline the model produces in free text is not an
  answer — it must come back from a tool as a computed value with its inputs
  attached.
- Access control is never expressed as a prompt instruction. The tool layer
  filters by the caller's identity before results ever reach the model, so a
  prompt-injection payload in a document cannot widen scope.
- Every deterministic rule is unit-testable with no API key and no network.

## 2. Source authority

Retrieval is not similarity-only. Each chunk carries a tier, and precedence
resolves conflicts before an answer is composed:

```text
Tier 1  Active signed customer agreement    (scoped to that customer only)
Tier 2  Current support policy / SOP / product documentation
Tier 3  Structured operational facts        (accounts, orders, tickets, SLAs)
Tier 4  Historical tickets & internal notes (context only — never authority)
```

Rules, all now implemented for the document tiers in
`app/backend/retrieval/authority.py` (see section 8):

- Deprecated documents are indexed but flagged. They can never be cited as
  current policy; they may be surfaced only to explain that a rule changed.
- A customer agreement outranks general policy **only** for that customer's
  accounts. Cross-customer bleed is a correctness bug, not a ranking preference.
- A historical ticket resolution may be wrong. It is never sufficient
  justification on its own.
- Where tiers conflict irreconcilably, the system escalates rather than picking.

The Phase 1 open questions are answered. Agreement terms are structured as
numbered sections whose headings name the domain they alter ("2. Shipment
cancellation", "3. Failed-pickup credits"), which is what makes a
topic-scoped override decidable in code. Status is stated **in-document**
(`Status: ACTIVE` in each agreement, `Status: CURRENT` / `Status: DEPRECATED`
in the policies), not only in the spreadsheet — so authority is derived from
the document itself and cross-checked against the workbook rather than
depending on it. Override clauses are per-section and explicit; one agreement
(LumenWorks) even defers back to the SOP in writing.

One refinement Phase 3 made to the tier list above: tiers 1–2 are unchanged,
but "current support policy / SOP / product documentation" is not a single
undifferentiated tier in practice. The current *support policy* and the
current *SOP / product documentation* are separated (tiers 2 and 3), and
precedence is resolved **per subject-matter topic**, so the cancellation SOP
governing cancellations does not make it govern severity definitions. Section
8.4 explains why.

## 3. Components

| Module | Responsibility |
| --- | --- |
| `app/backend/api/` | FastAPI routes, request/response schemas, error mapping |
| `app/backend/agent/` | Orchestration loop, system prompts, tool dispatch, step budget |
| `app/backend/tools/` | Tool definitions exposed to the LLM; thin, validated wrappers |
| `app/backend/policies/` | Deterministic rule engine — SLA, cancellation, credits |
| `app/backend/retrieval/` | `extraction.py` PDF→chunks · `authority.py` precedence · `search.py` BM25 + evidence |
| `app/backend/auth/` | Identity, roles, account scoping |
| `app/backend/services/` | `database.py` schema · `records.py` structured facts · `documents.py` documents/chunks |
| `app/backend/models/` | Pydantic schemas — `records.py` (P2), `documents.py` (P3), `policy.py`/`actions.py`/`agent.py` (P4) |
| `app/backend/core/` | `config.py` settings · `errors.py` typed failures and their HTTP contract |
| `app/backend/main.py` | ASGI entry point — `uvicorn app.backend.main:app` |

All nine exist as of Phase 5. `api/` holds `app.py` (assembly), `routes.py`,
`schemas.py` (the wire contract), `dependencies.py` (per-request wiring) and
`errors.py` (one envelope); `auth/principals.py` holds the mock identity
directory that produces an `AgentContext`.

Tools are deliberately thin. A tool validates its arguments, applies scoping,
calls into `policies/` or `services/`, and returns a structured result with
provenance. Business logic does not live in the tool layer.

## 4. Tool surface

At least three distinct tools are required. This was the Phase 0 sketch; §9.2
records what was built and §10.5 how the loop around it behaves.

1. **`search_documents`** — authority-ranked retrieval; returns chunks with
   document, page, tier, and deprecation status.
2. **`lookup_*` / `calculate_*`** — structured record lookup and deterministic
   policy computation; returns values with the inputs and rule that produced them.
3. **`prepare_action`** → **`execute_action`** — a two-call state change. The
   first returns a preview and a token; the second requires that token plus
   explicit user confirmation. The model can never reach `execute_action`
   without a human having approved the preview.

It shipped stronger than sketched: execution is not a second *tool* at all but
an orchestrator method absent from every registry, reached only through
`POST /api/actions/{id}/confirm` (§9.6, §10.7).

## 5. Confirmation, escalation, provenance

- **Confirmation.** No state change without an explicit approval step. Prepared
  actions are single-use, expiring, and re-validated at execution time — the
  world may have changed since the preview.
- **Escalation.** The agent must be able to answer "I can't determine this
  safely." Triggers: retrieval below a confidence floor, an unresolved
  cross-tier conflict, an out-of-scope account, or a missing input to a
  calculation. Escalation is a success case, not a failure.
- **Provenance.** Every claim ties to a document + page or to a record id.
  Calculated figures carry their inputs and the rule applied. An answer whose
  reasoning cannot be reconstructed is not acceptable.

## 6. Data flow

```text
data/source/  (supplied PDFs + XLSX — read-only, never edited)
      │
      ├─ scripts/verify_source_pack.py   (presence, SHA-256, readability)
      ├─ scripts/inspect_sources.py      (structural discovery report)
      │
      ├── 6 PDFs ────────────────────┐        ┌──── XLSX
      │   PyMuPDF                    │        │     openpyxl
      ▼                              ▼        ▼
scripts/ingest_documents.py     scripts/ingest_dataset.py
      │                                       │
      │  documents                            │  accounts / orders / tickets
      │  document_chunks                      │  dataset_metadata
      │  document_ingestion_runs              │  ingestion_runs
      │                                       │  source_provenance
      └──────────────┬────────────────────────┘
                     ▼
       data/processed/parcelpilot.db (SQLite)
       data/processed/source_inspection.json
                     │
   ┌─────────────────┴──────────────────┐
   ▼                                    ▼
services/records.py            retrieval/search.py
  (structured facts)             ├─ BM25 relevance
                                 └─ retrieval/authority.py
                                      (precedence, overrides, conflicts)
   └─────────────────┬──────────────────┘
                     ▼
                policies/    (Phase 4)
                     ▼
                   tools/
                     ▼
                  agent/  ←→  PlanningProvider
                     │          ├─ DeterministicPlanner  (no key, no network)
                     │          └─ OpenAIPlanningProvider (Phase 5)
                     ▼
                   api/  (FastAPI, Phase 5)  →  Next.js UI (Phase 6)
```

The two ingestion scripts write disjoint sets of tables into one SQLite file
and can be run in either order, repeatedly. Neither destroys the other's
data — see section 8.2 for why that required keeping documents out of the
Phase 2 foreign-key graph.

`data/processed/` is disposable and rebuildable from `data/source/`.
`data/index/` remains unused: retrieval scores in-process from the database
rather than from a separate index artifact (section 8.5).

## 7. Structured-data layer (Phase 2)

The structured-data layer turns `ParcelPilot_Assessment_Data.xlsx` into a
queryable SQLite database. It is a facts layer only — see "Facts vs. policy"
below — built by `scripts/ingest_dataset.py` and read through
`app/backend/services/records.py`.

### 7.1 Canonical relational model

The schema mirrors the workbook's own three data sheets (`accounts`,
`orders`, `tickets`) plus three tables for metadata/provenance. No business
field was added that isn't a source column; the only non-source columns are
the explicitly-scoped ingestion/audit tables below.

```text
accounts (account_id PK)
   ├─ account_name, plan, status, csm, notes
   ├─ contract_file      nullable soft reference to a source PDF filename
   │                     (not a DB foreign key -- Phase 3 owns document identity)
   └─ premium_support    boolean

orders (order_id PK, account_id FK → accounts)
   ├─ carrier, status, notes
   ├─ shipment_fee_inr, carrier_fault, customer_fault
   └─ booked_at, pickup_window_start, pickup_window_end,
      pickup_actual_at, cancellation_requested_at     (nullable timestamps)

tickets (ticket_id PK, account_id FK → accounts)
   ├─ status, subject, description, channel, assigned_to
   ├─ historical_resolution                            (nullable; see 7.5)
   └─ created_at, last_customer_message_at              (timestamps)

dataset_metadata (single row, id = 1)
   snapshot time/timezone, currency, README notes, source workbook
   filename/hash/sheet names, ingestion version — see 7.4

ingestion_runs (append-only audit log)
   one row per ingestion attempt: started/finished, workbook hash, status

source_provenance (target_table, target_id) UNIQUE
   for every accounts/orders/tickets row: which run loaded it, which file/
   sheet/row it came from, and its raw pre-parsing cell values as JSON
```

`orders.account_id` and `tickets.account_id` are real `FOREIGN KEY`
constraints, enforced via `PRAGMA foreign_keys = ON` on every connection
(`app/backend/services/database.py`) — this is the same
`orders.account_id → accounts.account_id` /
`tickets.account_id → accounts.account_id` relationship discovered in Phase
1's structural inspection, now enforced by the database engine rather than
just observed. `accounts.contract_file` stays a soft, nullable reference: it
names a file, not a database row, and not every account has one (Beacon
Retail and Axis Labs carry no custom agreement in the supplied pack). Every
table is declared `STRICT` (SQLite ≥ 3.37; this environment runs 3.50) so a
type mistake in ingestion code fails immediately instead of silently
coercing.

### 7.2 Why SQLite

Already the stack's committed choice (see the technology table in
[README.md](../README.md)) and the right fit for this phase regardless: the
whole dataset is a handful of rows, there is exactly one writer
(`ingest_dataset.py`, run standalone), the database is a disposable build
artifact regenerated from `data/source/` on demand, and `sqlite3` needs no
extra dependency, no running service, and no ORM. `STRICT` tables plus
enforced foreign keys give real integrity checking without any of that
weight.

### 7.3 Timestamp handling

Every date/time value in the workbook is stored as plain text
(`"2026-08-16 09:00"`, format `YYYY-MM-DD HH:MM`) — openpyxl confirms these
are text cells, not native Excel date values, and no row carries its own
timezone. The only timezone stated anywhere in the source pack is on the
workbook's `README` sheet: `Dataset snapshot: 2026-08-16 11:00 Asia/Kolkata`.
None of the six PDFs mention a timezone at all (their dates are day-level:
"Effective: 1 May 2026").

The parsing strategy, implemented once in `scripts/ingest_dataset.py`
(`_parse_dataset_snapshot`, `_parse_row_timestamp`) and applied uniformly:

1. Read the README's `Dataset snapshot` value and split it into a naive
   timestamp and an IANA zone name (`Asia/Kolkata`). This is read from the
   workbook every run — the zone name is never hard-coded as application
   logic, only as the literal value the source happens to supply.
2. Validate the zone name against the standard library's `zoneinfo`
   database. An unrecognised zone fails ingestion immediately.
3. Apply that single dataset-wide zone to every row-level timestamp cell in
   `orders` and `tickets` (`strptime` with the exact `YYYY-MM-DD HH:MM`
   format, then `.replace(tzinfo=...)`). This is a deliberate inference —
   the workbook states one zone for the whole dataset, not per row — and is
   the only reading consistent with "do not silently reinterpret as UTC":
   UTC appears nowhere in the source pack.
4. Any value that doesn't match the expected format, or names an
   impossible calendar date/time, raises `IngestionError` and aborts the
   entire run before any database write happens.
5. Parsed timestamps are stored as SQLite `TEXT` in ISO 8601 with an
   explicit offset (`2026-08-16T09:00:00+05:30`) — deterministic,
   unambiguous, and (since India does not observe DST) stable for this
   dataset's date range.
6. The original, unparsed cell value is preserved separately — see
   Provenance below — so the exact source text is never lost even though
   the operational tables store the parsed/typed form.

### 7.4 Dataset metadata

`dataset_metadata` (always exactly one row — enforced by `CHECK (id = 1)`)
captures the workbook snapshot explicitly: the raw and parsed snapshot
timestamp, the timezone name, currency, the README's free-text notes, the
source workbook's filename and SHA-256 (via the same `sha256_of` helper
Phase 1's `verify_source_pack.py` uses — one hashing implementation, not
two), the sheet names present, and the ingestion script version. Nothing
here is hard-coded in application code; every field is read from the
workbook or computed from it at ingestion time. This is the row a later
policy engine or agent tool reads to answer "as of when is this data
current" rather than assuming "now".

### 7.5 Provenance approach

Every row loaded into `accounts`, `orders`, or `tickets` gets exactly one
matching row in `source_provenance`, keyed by `(target_table, target_id)`.
It records the source filename, sheet name, and the 1-based spreadsheet row
number (so "row 5" means what it means if you open the file), plus a JSON
snapshot of that row's values exactly as read from the worksheet — before
any type-coercion or timestamp-parsing. This is deliberately a whole-row
snapshot rather than a parallel `_raw` column next to every parsed column:
`orders` alone has five timestamp columns, and duplicating each would bloat
the schema for no benefit over one JSON blob per row. `ingestion_runs` is an
append-only audit log (one row per script run, never deleted) that
`source_provenance` rows link back to, so "which run produced this fact, and
when" is always answerable. Together these let a later agent tool answer
"where did this value come from" down to the file, sheet, row, and original
text — not just "the database says so".

### 7.6 Facts vs. policy — why calculation logic is not here

Section 1's split applies to this layer without exception: `accounts`,
`orders`, and `tickets` store what the source data says happened, never a
number derived by applying a business rule to it. Concretely, this layer
answers "order ORD-1001 is `BOOKED` and `cancellation_requested_at` is
2026-08-16T11:00+05:30" — it does not compute whether a cancellation fee
applies, what that fee is, whether an SLA was breached, or which of two
conflicting agreements governs. Those questions need the order state *and*
the applicable policy document *and* the current time, evaluated by rules
that do not exist yet (Phase 4). Keeping the boundary here — rather than,
say, adding an `orders.cancellation_fee_inr` column computed at ingestion
time — means the policy engine can be changed, corrected, or unit-tested
without ever re-ingesting data, and means this layer's tests never
accidentally encode a business rule as a fixture assertion.

### 7.7 Ingestion workflow

`scripts/ingest_dataset.py` runs in two clearly separated phases:

1. **Validate entirely in memory.** Open the workbook read-only, check that
   exactly the four expected sheets are present (`README`, `accounts`,
   `orders`, `tickets` — extra or missing both fail), check that each data
   sheet's header matches its expected column set exactly, parse and
   type-check every cell, parse every timestamp, and check that every
   `orders.account_id` / `tickets.account_id` matches a row in `accounts`
   and that every non-null `contract_file` names one of the six supplied
   PDFs. Any failure raises `IngestionError` with the sheet/row/column at
   fault, and the database is never opened.
2. **Load inside one transaction.** Only after validation succeeds: insert a
   new `ingestion_runs` row, wipe `orders`, `tickets`, `source_provenance`,
   and `accounts` (in FK-safe order), reinsert everything just validated,
   upsert the single `dataset_metadata` row, and mark the run `success`. If
   anything fails here, the transaction rolls back and the previous
   successful load is untouched.

This makes re-running the script safe: `accounts`/`orders`/`tickets`/
`source_provenance` reflect exactly the current workbook (never doubled),
`dataset_metadata` reflects the latest run, and `ingestion_runs` grows by
one row per attempt by design — it is an audit trail, not application data.
The workbook itself is opened read-only and never written to.

Run it with `python scripts/ingest_dataset.py` (writes
`data/processed/parcelpilot.db`, git-ignored and fully regenerable — see
[README.md](../README.md)).

### 7.8 Structured-data access

`app/backend/services/records.py` is the only sanctioned read path.
Every function takes an explicit `sqlite3.Connection`, uses parameterized
SQL exclusively (there is no function anywhere that accepts a SQL string
from a caller), and returns a typed Pydantic model
(`app/backend/models/records.py`) or `None`/`[]` — never a bare
`sqlite3.Row`. At minimum: `get_account`, `get_order`, `get_ticket`,
`get_account_orders`, `get_account_tickets`, plus `get_dataset_metadata` and
`get_source_provenance` for the metadata/provenance tables above.

"Not found" (`None`/`[]`) and "found with null fields" are distinguishable —
a record that exists but has a blank source field is still returned, just
with those fields `None`. Full authentication/authorization is Phase 5, not
built yet, but every function already accepts an optional
`allowed_account_ids` collection: when given, a record outside that set is
treated identically to "not found" (never a distinguishable "forbidden",
which would leak existence to an out-of-scope caller). When omitted — true
today, since no caller identity exists yet — no scoping is applied. This
means Phase 5 can wire real auth through this parameter without changing the
function signatures or any existing caller.

## 8. Document / evidence layer (Phase 3)

Turns the six PDFs into citable, authority-ranked evidence. Where Phase 2
answers "what is true of this order", this layer answers "what does the
governing document say, and which document is that".

### 8.1 Document ingestion

`scripts/ingest_documents.py` reads every supplied PDF via PyMuPDF and writes
`documents` + `document_chunks`. Same shape as Phase 2's dataset ingestion:
validate everything in memory, then load inside one transaction, so a
malformed document fails loudly and leaves the previous load intact.

Structure is recovered from **typography**, which the real files use
consistently: 24pt bold title, 18pt bold section heading, 14pt bold
sub-section, 11pt bold `Key: Value` metadata, 11pt body. Detecting headings
by size rather than by a numbering regex matters —
`02_Support_Policy_v2_DEPRECATED.pdf`'s only heading carries no number, and a
regex would have missed it and collapsed that document into one chunk.

Metadata (`Status:`, `Effective:`/`Updated:`, `Account:`, `Customer:`,
`Term:`, `Plan:`, `Supersedes:`/`Superseded by:`) is read **only from the
preamble**, the lines above the first section heading.
`04_Product_Operations_Guide` contains `Status: Investigating` and
`Status: Monitoring` inside its known-issue blocks; reading `Status:` from
anywhere on the page would have classified a current document as neither
current nor deprecated and silently changed its authority.

Nothing is invented. A document that states no `Effective:` date gets null,
not a date borrowed from `Updated:`. A document whose status cannot be
recognised raises rather than defaulting — defaulting either way would decide
authority by accident.

### 8.2 Storage, and why it is decoupled from Phase 2

Three new tables, `STRICT` like the rest: `documents`, `document_chunks`, and
an append-only `document_ingestion_runs` audit log.

Two deliberate non-couplings, both for the same reason:

- **`documents.account_id` is not a foreign key** to `accounts`.
- **Document provenance does not reuse `source_provenance`.**

`scripts/ingest_dataset.py` runs `DELETE FROM accounts` and
`DELETE FROM source_provenance` on every run. With an FK, reloading the
workbook would fail or cascade; with shared provenance, reloading the
workbook would silently delete every document's provenance. Either coupling
would make the two scripts order-dependent in a way that fails at the worst
time. Instead the account link is a soft reference — exactly how Phase 2
already treats `accounts.contract_file` — cross-checked at ingestion time.

That cross-check is graded. An agreement naming an account the workbook does
not contain is only *noted*: the two layers load from independent sources and
may be different vintages, and account isolation keys on the document's own
stated account, so it is unaffected. But both sources describing the *same*
account while disagreeing about which file is its contract is a genuine
contradiction, and fails.

### 8.3 Chunking and evidence provenance

A chunk starts at every section or sub-section heading and **never spans a
page**, so a chunk's page number is always exact; a section continuing across
a page break becomes two chunks carrying the same section title.
Sub-sections become their own chunks, which is why `KI-208` and `KI-211` are
independently retrievable instead of buried inside one "Current known issues"
blob — known-issue matching is an explicit product capability.

Every chunk answers the provenance questions in full: source file (plus the
file's SHA-256), page, section number/title, sub-section, document, and
account when customer-specific. It also stores `page_char_start` /
`page_char_end`, offsets into the page's normalised text — so a citation is
*verifiable*, not merely asserted: re-extract the page, slice the offsets,
and the stored chunk text must come back. A test asserts this for all 26
chunks against the real PDFs.

Trade-off: PyMuPDF linearises the response-target tables in the policy PDFs
row-major, so the grid shape is lost while every value survives adjacent to
its row label. Reconstructing table geometry was not worth the complexity for
two small tables that read correctly as text.

### 8.4 Source authority, and why it is separate from relevance

`app/backend/retrieval/authority.py` computes a tier from what a document
*states about itself*, never from how well it matched a query:

```text
Tier 1  CUSTOMER_AGREEMENT        active, and only for its own account
Tier 2  CURRENT_SUPPORT_POLICY
Tier 3  CURRENT_OPERATIONAL_DOC   current SOP / product documentation
Tier 4  NON_AUTHORITATIVE         deprecated or superseded — context only
```

Status dominates type: a *deprecated* support policy is tier 4, not tier 2.
That is what makes `02_Support_Policy_v2_DEPRECATED.pdf` structurally
incapable of governing, however well it scores — and it does score well on
response-target queries, which is the point.

Precedence resolves **per topic**, not globally. Each chunk carries a topic
derived from its section heading (`cancellation`, `service_credit`,
`support_response`, `product_known_issues`, `general`), and an override only
applies between sources discussing the same one. This is what "current SOP /
product documentation *according to the subject matter*" means operationally:
the cancellation SOP governs cancellations without thereby governing severity
definitions, and Northstar's "2. Shipment cancellation" overrides the SOP's
"1. Order cancellation" while leaving the support policy untouched.

**A customer agreement may only govern a resolution scoped to its own
account.** On an unscoped question every agreement is demoted to context — it
stays fully readable, it simply cannot decide the answer. Without this rule a
general "what is the P1 response target?" question was resolving to
Northstar's 15-minute term, which is precisely the cross-customer bleed
section 2 forbids.

`resolve_authority` returns governing evidence, contextual evidence, an
`OverrideNote` for every override naming both sources, and a `ConflictNote`
whenever two *different* documents share the governing tier on a topic. The
last case is not resolved by picking one: it is surfaced for escalation,
because silently choosing is the failure mode this layer exists to prevent.
Deprecated and overridden material is always returned in full — explaining
that a rule changed requires quoting the rule that changed.

### 8.5 Retrieval

`app/backend/retrieval/search.py` ranks with Okapi BM25 in pure Python over
the chunks the caller may see. For six documents and 26 chunks, an embedding
index or FTS5 table would be more machinery for the same answers plus a build
artifact that can drift from the PDFs. BM25 is deterministic, needs no API
key, and is strongest exactly where this corpus is queried — lexical clause
and identifier lookup (`KI-208`, `INR 250`, `P1`, `ACCT-001`). The tokenizer
keeps hyphenated identifiers whole *and* split, and normalises `3,000` to
`3000`. No parameter was tuned against the assessment's example questions.

`data/index/` therefore stays empty. Revisit only if the corpus grows enough
that paraphrase recall starts costing real answers.

### 8.6 Account scoping

Enforced **in SQL**, in `app/backend/services/documents.py`: the visibility
predicate is compiled into every query's WHERE clause, so another customer's
agreement is never loaded into the process at all. There is no filtered-out
object in memory for a later bug to leak, and no prompt instruction involved.

Two restrictions compose, and the stricter wins:

- `allowed_account_ids` — Phase 2's authorization hook, unchanged in meaning.
  `None` is unrestricted (no auth layer exists yet); an empty collection
  hides every customer-specific document.
- `account_id` — the account the question is *about*, which additionally
  hides other customers' agreements even from a caller authorized to see
  them.

General documents (`account_id IS NULL`) stay visible under both, which is
what lets an account-scoped search still retrieve the policy and SOP — and
therefore what makes precedence resolvable at all. Out-of-scope ids return
nothing rather than an error, so a caller cannot probe for the existence of
another customer's agreement.

Scoring statistics are computed over the visible candidate set only, so a
document the caller may not see cannot influence the ranking of ones they
can.

### 8.7 How the agent will consume this

`search_and_resolve(conn, query, account_id=..., allowed_account_ids=...)` is
the intended tool entry point. It searches, then resolves precedence, passing
`account_id` to both — which is what makes the common path correct by
default rather than by the caller remembering. It returns governing evidence
separated from context, plus the override and conflict notes needed to say
*why* a customer is treated differently and when to escalate.

`get_document_evidence(...)` re-resolves specific `chunk_id`s or reads a
whole document, under identical scoping, for when the model wants to expand a
citation it has already seen.

`Evidence` is deliberately flat: a tool can serialise it straight into a
prompt and every citation field is present without traversing relationships.
The model decides what the retrieved text *means*; it never decides which
source outranks which, and it never decides what it is allowed to see.

## 9. Agent, tools and actions (Phase 4)

Where Phase 2 supplies facts and Phase 3 supplies authority-ranked evidence,
Phase 4 is the layer that takes a natural-language request, decides which of
those to consult, and returns an answer whose every figure came from code.

### 9.1 Orchestration and the provider seam

`AgentOrchestrator.handle` runs a bounded plan/execute loop: ask the provider
for the next tool calls, execute them under the caller's context, feed the
results back, repeat until the provider stops or the step budget is spent.
Every call is recorded as a `ToolInvocation`, so a response can show its work.

The orchestrator talks to a `PlanningProvider` protocol, never to a vendor
SDK. Adding an OpenAI-backed provider means implementing one method —
`registry.schemas()` already emits function-calling definitions — without
touching the orchestrator, tools, or policy engine.

The provider shipped in this phase is `DeterministicPlanner`: it plans from
identifiers and intent keywords in the request, advancing only on what earlier
tool results actually returned. It is genuinely multi-step (resolve records →
derive scope → evaluate policy → retrieve documentation → propose an action),
and it keeps the entire agent runnable and testable with no API key and no
network, exactly as the policy engine is. It contributes no facts of its own;
when it finishes, `agent/composer.py` assembles prose strictly from typed tool
results.

One planning rule is load-bearing: **scope is derived from resolved records,
never from the request text.** A request naming a customer cannot widen what
the agent reads; the account comes from an order or ticket the caller was
permitted to look up.

### 9.2 Tool boundaries

| Tool | Wraps | Contributes |
| --- | --- | --- |
| `search_documents`, `get_document_evidence` | Phase 3 retrieval | agent-facing shape only — no second ranking or authority implementation |
| `lookup_record`, `lookup_record_provenance` | Phase 2 repository | enumerable entity set; no SQL reaches this layer |
| `evaluate_cancellation`, `evaluate_service_credit` | Phase 4 policy engine | deterministic decisions with inputs, rule and arithmetic |
| `prepare_escalation`, `prepare_ticket_note` | action service | proposals only — never an effect |

Tools are thin: parse arguments, apply scope, delegate, return a typed
`ToolResult`. A `ToolResult` that failed stays failed — `NOT_FOUND`,
`FORBIDDEN`, `NO_EVIDENCE`, `UNCERTAIN`, `ERROR` are distinct statuses the
composer surfaces rather than smooths into prose.

### 9.3 The policy engine, and generic agreement overrides

The hard requirement was that customer-agreement overrides work *generically*
— no branch on an account id anywhere. The mechanism:

1. `services/documents.py:get_evidence_by_topic` fetches the clauses on a
   topic **scoped to one account**, so only the general documents plus that
   account's agreement are in play.
2. `policies/terms.py` extracts parameters (free window, fee, delay
   threshold, credit amount, caps, approval threshold) from that text using
   documented patterns, applying evidence **weakest-authority-first** so a
   stronger source overwrites field by field.
3. `policies/cancellation.py` and `policies/service_credit.py` compute from
   the resulting terms plus the order's structured facts.

Layering is what makes this faithful to the documents. An agreement that
states only a monthly cap and says the SOP otherwise applies keeps the SOP's
threshold and formula; an agreement that states a threshold and a fixed
amount replaces both. Neither case is special-cased.

Two wording traps in the corpus shaped the extraction rules, and both are
tested: one agreement says *"No special cancellation-fee waiver applies"*,
and the SOP itself says a fee applies *"unless a customer agreement explicitly
waives"* it. A naive "no … cancellation fee" match would have read either as
granting a waiver. Waivers are therefore recognised only from affirmative
phrasing, only in agreement-scoped text, and only past an explicit negation
guard.

**Silence is not zero.** A parameter no document states stays `None`, and an
incomplete term set produces `REQUIRES_VERIFICATION` rather than a default.
Inventing a number here would be inventing an invoice line.

### 9.4 Uncertainty as a first-class outcome

The SOP forbids promising a credit when carrier fault, pickup timing, or
customer fault is unknown, and the product guide warns that pickup
confirmation can lag — a parcel may be collected while the system still shows
BOOKED. Both are implemented literally rather than as prompt guidance:

- When no pickup has been confirmed, lateness is *inferred* from the snapshot
  clock, and the decision says so, returning a **provisional** amount with
  `REQUIRES_VERIFICATION` instead of an eligibility promise.
- A BOOKED order whose pickup window has closed raises the same caution
  before cancellation, since cancelling a parcel already collected is the
  concrete harm.
- A policy question asked without an order id is reported as unanswerable
  with the missing input named — not answered with a pile of related
  documentation.

None of these name a carrier or a known-issue id; they follow from the
recorded facts and the retrieved evidence.

### 9.5 Time

Time-based questions are evaluated against `dataset_metadata.dataset_snapshot_at`,
loaded from the database, never `datetime.now()`. The supplied data is a fixed
snapshot; judging a 30-minute cancellation window against wall-clock time
would change the answer every day the assessment is re-run. Every decision
records which clock it used.

### 9.6 Actions and the confirmation gate

An action's lifecycle is persisted state, not conversation:

```text
prepare_*  ->  PENDING_CONFIRMATION  --confirm-->  EXECUTED
                       |                              (or FAILED)
                       +-----------reject---------->  REJECTED
                       +-----------lapse----------->  EXPIRED
```

Three structural guarantees:

- **Preparation is inert.** It writes a proposal row and nothing else.
- **Execution is unreachable by the model.** `confirm_action` is an
  orchestrator method, deliberately absent from every tool registry. A
  natural-language request — however urgently phrased — can at most produce a
  proposal. This is enforced by the shape of the tool surface, not by a
  prompt.
- **Execution re-validates.** Authorization is rechecked under the
  *confirming* caller, expiry is enforced, and the target is re-read before
  any effect is written. A status guard in the UPDATE makes execution
  single-use even under concurrent confirmation.

Effects land in `ticket_escalations` / `ticket_notes`, not in `tickets`. The
Phase 2 tables are rebuilt from the workbook on every ingest run, so an
effect written there would be silently reverted by the next reload — the same
reasoning that kept `documents.account_id` out of the foreign-key graph in
Phase 3.

The audit trail answers who initiated, what was requested, with which
parameters, on what evidence, and when it was prepared, confirmed, and
executed.

### 9.7 Authorization

`AgentContext` (user, role, `allowed_account_ids`) is constructed by the
caller and threaded to every tool call. Two properties make it robust:

- **Scope is injected, never accepted.** Tool arguments carrying
  `allowed_account_ids` or similar are *rejected*, not ignored, so an attempt
  to widen scope appears in the audit trail. Enforcement lives below the
  agent, in the Phase 2/3 repositories and their SQL-level visibility clause.
- **Out-of-scope is indistinguishable from missing.** Identical status and
  message, so the tool cannot be used as an existence oracle for another
  customer's data.

Roles are minimal (`support_agent`, `support_manager`, `read_only`); the full
taxonomy is Phase 5. `read_only` cannot prepare or confirm state changes.

### 9.8 Response model

`AgentResponse` carries the answer, the evidence with page and section, every
tool invocation, the typed policy decisions, the pending or executed action,
explicit uncertainties, and whether escalation is recommended — everything the
Phase 9 chat UI needs, with nothing left to re-derive.

## 10. Application API and real agent integration (Phase 5)

Phases 2–4 built a tested agent that could only be called from Python. Phase 5
puts an HTTP boundary in front of it, gives the `PlanningProvider` seam a real
model behind it, and turns the confirmation state machine into something a
client can actually drive.

Nothing below the API moved. The orchestrator, the tool registry, the policy
engine and the action service are reached through the same entry points a
non-HTTP caller would use, which is why the Phase 1–4 suite passes unchanged.

### 10.1 The FastAPI boundary

```text
POST /api/chat
      │
      ▼
resolve identity ──► auth/principals.py     (role + account scope, server-side)
      │
      ▼
AgentContext ──────► AgentOrchestrator.handle
                          │
                          ▼
                     PlanningProvider          deterministic | real
                          │  ▲
                    tool  │  │  ToolResult
                    call  ▼  │
                     ToolRegistry ──► retrieval / records / policies / actions
                          │
                          ▼
                     AgentResponse ──► ChatResponse   (answer + evidence +
                                                       decisions + action state)
```

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness, provider mode, data readiness. No secrets, no paths. |
| `POST /api/chat` | One natural-language request. May *prepare* an action; never performs one. |
| `POST /api/actions/{id}/confirm` | The only path to a state change. Closed decision vocabulary. |
| `GET /api/actions/pending` | Proposals awaiting confirmation, within the caller's scope. |
| `GET /api/actions/{id}` | The audit record: state, timeline, effect id. |
| `GET /api/principals` | The mock identities this deployment accepts. |

The routes are deliberately thin (`app/backend/api/routes.py`): resolve the
caller, delegate, project the typed result onto the wire contract. No route
computes a figure, ranks a source, or decides what an account may see.

Everything request-scoped is built per request in `api/dependencies.py` — the
SQLite connection (not shareable across the thread pool), the authorization
context, the provider, and the orchestrator. Only `Settings` lives on the app.

### 10.2 Request and authorization context

The critical property: **the API establishes the authorization context, and
the message cannot expand it.**

The client asserts an identity — `user_id` in the body or the
`X-ParcelPilot-User` header — and the server resolves that identity against a
fixed directory (`app/backend/auth/principals.py`) to get a role and an
account scope. The request never states its own permissions. There is no
anonymous path: an absent or unrecognised identity is a 401.

```text
customer.northstar    role=customer          scope = {ACCT-001}
customer.lumenworks   role=customer          scope = {ACCT-002}
support.agent         role=support_agent     scope = every account in the dataset
support.manager       role=support_manager   scope = every account in the dataset
support.readonly      role=read_only         scope = every account in the dataset
```

Two details that matter more than they look:

- **Internal scope is derived, not hard-coded.** Staff principals resolve to
  the account ids actually present in `accounts`, so support staff hold a real
  explicit scope that the same SQL predicate filters on — not an unrestricted
  bypass, and not a list maintained by hand in application code.
- **A request may narrow, never widen.** `account_scope` on the request is
  *intersected* with the principal's own scope. It exists so a support agent
  can deliberately work inside one customer's view. A customer passing
  `["ACCT-001", "ACCT-002"]` still gets `{ACCT-001}`.

`Role` gained `CUSTOMER` in this phase — additive, and like `READ_ONLY` it
cannot change state. `AgentContext` gained `session_id`, which is what binds a
prepared action to the conversation that produced it (§10.7).

Enforcement is unchanged and still lives below the model: the SQL visibility
predicate in `services/documents.py`, the `allowed_account_ids` checks in
`services/records.py`, and the reserved-argument rejection in
`tools/base.py`. The API supplies the scope; it does not implement the check.
A model that emits `allowed_account_ids` is refused by the tool layer, exactly
as in Phase 4.

### 10.3 The provider abstraction, and what is behind it

`PlanningProvider.next_step` is unchanged from Phase 4. Two implementations
now satisfy it, and a test asserts their signatures are identical to the
protocol's — if integrating a live model had required widening the seam, that
would show.

**`DeterministicPlanner`** (Phase 4) remains a first-class supported mode, not
a test stub. It is why every safety boundary in this system — scoping,
precedence, policy arithmetic, the confirmation gate — is verifiable with no
API key and no network, and it is what CI and the entire test suite run on.

**`OpenAIPlanningProvider`** (`agent/openai_provider.py`) is the real one:
OpenAI chat-completions function calling. `registry.schemas()` already emitted
function-calling definitions, so they are handed to the model verbatim — the
contract the model sees and the contract the code enforces come from one
definition. Model tool calls become `ToolCall`s the orchestrator executes;
`ToolResult`s go back as `tool` messages; a reply with content and no tool
calls becomes `final_answer`, which the orchestrator prefers over the
deterministic composer's prose.

What the provider cannot do is structural rather than instructed:

- It has no import path to the database, the policies package, or the action
  service. It cannot execute SQL because nothing in its reachable surface can.
- It never sees `allowed_account_ids`. Scope is injected by the orchestrator.
- `confirm_action` is not in any registry, so there is no tool call that
  reaches execution.
- An API failure, a timeout, or an unusable reply raises `ProviderError`. It
  never becomes prose — a provider failure that produced an answer anyway
  would be the most dangerous failure mode in the system.

The provider instance is per-request: it accumulates the transcript it is
building, so sharing one would splice conversations together.

### 10.4 Provider selection and configuration

`LLM_PROVIDER` is explicit — `deterministic` or `real` — and there is **no
fallback path** between them. `LLM_PROVIDER=real` without `OPENAI_API_KEY`
fails at startup (`ProviderConfigurationError`), before the process serves
anything. A deployment that asked for a live model and quietly got a
rule-based planner would be running a different system than its operator
believes, and the difference would surface only in an answer nobody could
explain.

Credentials come from the environment. `Settings` exposes
`has_provider_credentials`, never the key; `/health` reports the provider mode
and nothing about its configuration. The `sk-replace-me` placeholder shipped
in `.env.example` is treated as absent, so a copied template cannot look
configured.

The `openai` SDK is imported lazily inside `agent/factory.py`, so the
application and the whole test suite run on a machine that never installed it.
That is the practical proof the abstraction is real rather than decorative.

### 10.5 The tool loop

The Phase 4 orchestration loop is the tool loop; Phase 5 did not add a second
one. A request drives as many rounds as the provider asks for, up to
`AGENT_MAX_TOOL_STEPS`.

Safeguards, and where each lives:

| Hazard | Handled by |
| --- | --- |
| Unbounded loop | `max_steps` in the orchestrator; the response reports `step_budget_exhausted` and adds an uncertainty rather than hiding the truncation |
| Unknown tool | `ToolRegistry.execute` answers with the list of tools that do exist — the most useful correction the model can get |
| Malformed tool arguments | The provider answers the model with the JSON error and re-asks, bounded by `MAX_REPAIR_ATTEMPTS`; persistent garbage raises |
| Tool exception | `ToolRegistry.execute` converts it to `ToolStatus.ERROR`; the composer surfaces it and the outcome is not `answered` |
| Empty model reply | Challenged once, then raises |
| Provider timeout | `ProviderTimeoutError` → HTTP 504 |
| Oversized tool result | Visibly truncated, never silently dropped — the status still reaches the model |

Tool failures are never smoothed. A `NOT_FOUND`, `FORBIDDEN` or `ERROR`
result appears in `tools_used`, in `uncertainties`, and in the answer text.

### 10.6 The response contract

`ChatResponse` is a projection of `AgentResponse`, not a replacement. It
decides what crosses the wire:

```text
answer              prose, from the model or the deterministic composer
sources[]           citation, file, page, section, authority tier, excerpt
tools_used[]        step, tool name, status, one-line summary
policy_decisions[]  rule, calculation, amount, inputs, citations, verification
uncertainties[]     what is unknown, stated explicitly
action_status       none | pending_confirmation | executed | rejected | ...
proposed_action     preview, expiry, evidence, parameter fingerprint
reference_time      the dataset snapshot every time-based decision used
responded_at_utc    wall clock — technical metadata only
```

Two deliberate omissions. Tool **arguments** and the model's intermediate
messages do not cross the wire: the contract promises conclusions and
provenance, not a reasoning trace. And money is a **string**, not a JSON
number — a `Decimal` that becomes a float stops being the figure the policy
engine computed.

`action_status` is always present, so "nothing is pending" is an answer rather
than an absence. `ActionState.CONFIRMED` is in the enum but is not currently
emitted: Phase 4 confirms and executes inside one guarded transaction, so a
confirmed action is already `EXECUTED` before anyone can observe it. It is
declared so a future asynchronous executor needs no breaking change.

### 10.7 Confirmation through the API

`POST /api/chat` can reach `PENDING_CONFIRMATION` and no further. Execution is
a separate endpoint with a closed vocabulary — `approve` or `reject`. A user
typing "okay" into the chat endpoint confirms nothing, because that endpoint
has no execution path at all. The gate is structural, not a matter of parsing
intent correctly.

`POST /api/actions/{id}/confirm` re-validates everything, under the
*confirming* caller rather than the preparing one:

| Check | Enforced by |
| --- | --- |
| Role may change state | `AgentContext.may_change_state` |
| Action exists **and** is in the caller's account scope | `get_action(..., allowed_account_ids=...)` |
| Action belongs to this conversation | `agent_actions.session_id` vs. the request's session (new in Phase 5) |
| Still `PENDING_CONFIRMATION` | Phase 4 state machine |
| Not expired | Phase 4 TTL |
| Target still exists and is still visible | Re-read through the scoped repository |
| Parameters unchanged since review | `parameter_fingerprint` (new in Phase 5) |
| Executes exactly once | Status guard inside the `UPDATE` — holds under a race |

Two additions were needed and both are additive:

- **`session_id`.** `agent_actions` gained a nullable column, applied to
  existing databases by `ADDED_COLUMNS` in `services/database.py`
  (`CREATE TABLE IF NOT EXISTS` silently does nothing to an existing table, so
  a migration list was required). An action with no session — a Phase 4 caller
  or a script — stays confirmable by any authorized caller, because binding
  never happened. Once bound, it is enforced: two conversations under one
  support agent are still two separate approvals.
- **`parameter_fingerprint`.** A digest of what the action would do, returned
  with the preview and echoed on confirmation. It closes the gap between "the
  operator approved a preview" and "the system executed a proposal".

`ActionSessionError` subclasses `ActionStateError`, so every existing handler
still catches it while the API can report `action_session_mismatch` distinctly
from `action_not_pending` — "you are confirming from the wrong place" and
"this was already decided" are different situations.

### 10.8 Error handling

Every failure leaves as one envelope with a stable code:

```json
{"error": {"code": "...", "message": "...", "details": {}, "request_id": "..."}}
```

| Code | Status | Cause |
| --- | --- | --- |
| `validation_error` | 422 | Request body did not match the schema |
| `unauthenticated` | 401 | Absent or unrecognised identity |
| `forbidden` | 403 | Role refusal (e.g. actions disabled on this deployment) |
| `not_found` | 404 | Action absent, or outside the caller's scope |
| `action_not_pending` | 409 | Already executed, rejected, expired, or parameters changed |
| `action_session_mismatch` | 409 | Prepared in a different conversation |
| `action_execution_failed` | 409 | The action service refused deliberately |
| `provider_not_configured` | 503 | `real` requested without credentials |
| `data_unavailable` | 503 | Database not built or not ingested |
| `provider_error` | 502 | Model provider failed |
| `provider_timeout` | 504 | Model provider did not respond in time |
| `internal_error` | 500 | Anything unhandled — logged with traceback, reported without |

Two rules the handler holds:

- **Nothing internal leaks.** An unexpected exception becomes a generic
  `internal_error`. Upstream SDK error text is not forwarded either; the
  server log keeps the original.
- **The security model is not rewritten at the boundary.** The tool layer
  answers "out of scope" and "does not exist" identically on purpose, so an
  out-of-scope caller cannot use the API as an existence oracle. That decision
  is made below and passed through unchanged — the API does not invent a 404
  to hide a 403, nor a 403 to explain a 404. A test asserts the refusal for a
  real out-of-scope record and for a fabricated one are indistinguishable.

### 10.9 Time, unchanged

Business reasoning still runs on `dataset_metadata.dataset_snapshot_at`,
loaded through `load_evaluation_context` and passed to the real provider's
prompt so the model is told explicitly not to substitute today's date.
`datetime.now()` appears only in request/audit metadata: `responded_at_utc`,
`checked_at_utc`, and the action lifecycle timestamps.

### 10.10 Prompt scope

`agent/prompts.py` holds the minimum instructions the real agent needs: the
support role, use the tools, never invent a fact, historical ticket
resolutions are not authority, use the customer agreement where it applies,
never compute a policy figure yourself, prepare rather than execute, and say
when you are uncertain.

It deliberately contains no ParcelPilot rule, threshold, fee, customer name or
known-issue id. A prompt that repeated the policy would be a second policy
engine that nobody tests and that drifts the moment a document is revised. A
test asserts the system prompt contains none of the corpus's actual answers.

Nothing in the prompt is a security control. The per-request context block
tells the model which accounts the caller may reach, but only so it does not
waste steps probing records it cannot read — the SQL predicate is what stops
it.

### 10.11 What Phase 5 implemented, and what it did not

**Implemented now:** FastAPI application with typed request/response models;
mock authentication and server-side authorization context; a real OpenAI
provider behind the unchanged Phase 4 seam; explicit provider selection with
no silent fallback; the multi-step tool loop exposed end to end; evidence-,
decision- and action-bearing responses; the confirmation endpoint with
session binding and parameter fingerprinting; action audit reads; a
consistent error envelope; a working `ENABLE_STATE_CHANGING_ACTIONS` kill
switch.

**Deferred at the time:** the frontend (built in Phase 6 — see §11);
conversation memory across turns (a `session_id` is issued and binds actions,
but no prior-turn context is replayed to the model); a production identity
provider; streaming responses; proactive issue detection; actual hosting. See §13.

### 10.12 What authority the model's prose carries

In `LLM_PROVIDER=deterministic`, the answer text is assembled by
`agent/composer.py` from fields of typed tool results, so the prose cannot
disagree with the decision — it is built out of it.

In `LLM_PROVIDER=real` that is not true. `AgentOrchestrator.handle` prefers the
provider's own `final_answer` when one is present
(`orchestrator.py`, `_provider_answer`), so in real mode the `answer` string is
written by the model. This section states plainly what that does and does not
put at risk.

**The structured decision is authoritative, and it is what the UI renders.**
A response carries `policy_decisions[]` alongside `answer`. Those decisions come
from `app/backend/policies/` and are untouched by the provider: the model can
choose to call `evaluate_service_credit`, but it cannot alter what that call
returns, and it cannot produce a decision object of its own. The frontend's
`DecisionCard` renders **from the decision**, never by parsing prose — the
amount, the currency, the rule, the arithmetic, the breach state and the
citations on screen are all the deterministic values. Switching provider
changes which tools get called, not what any of them are permitted to do, and
not what the card shows.

**The prompt instructs exact reporting.** `agent/prompts.py` tells the model
never to compute a fee, credit, eligibility verdict, SLA target or breach
itself, never to restate one from memory, and to report exactly what the policy
tool returned including its stated rule and arithmetic. It also states that a
tool reporting `requires_verification` *is* the answer.

**The residual risk, stated rather than hidden.** A prompt is an instruction,
not an enforcement mechanism. In real mode a model could in principle write
prose containing a figure that does not match the decision returned beside it —
a transcription slip, or a confident paraphrase. Nothing in the current build
detects that. What bounds the damage:

- the decision card beside the prose shows the correct figure, so the two are
  visibly side by side rather than the prose standing alone;
- every citation attached to the response is the one the retrieval layer
  returned, so the sources cannot be fabricated even if the summary drifts;
- authorization, scoping, precedence and the confirmation gate are entirely
  unaffected — none of them reads the answer string, so a wrong sentence cannot
  become a wrong *action*;
- the deterministic mode, which has no such gap, is the default and is what the
  entire test suite runs on.

The natural hardening is to assert that every monetary figure appearing in a
model-authored answer also appears in `policy_decisions[]`, and to fall back to
the composer's deterministic prose when it does not. That is deliberately **not
built** — it is a real check with real false-positive design work behind it
(percentages, dates and record ids all look like figures), and shipping a
half-tuned validator that silently rewrote answers would be worse than the
documented gap it replaced. It is listed as future work in
[product.md](product.md#future-work).

## 11. The chat interface (Phase 6)

Phase 5 made the agent reachable over HTTP. Phase 6 makes it usable, and does
so as a *client* — the UI adds a rendering layer and nothing else. Every
judgement it shows was made below it.

### 11.1 The frontend boundary

```text
browser
   │  React state: identity, session, turns
   ▼
src/lib/client.ts          the only module that speaks HTTP
   │  fetch + ApiError
   ▼
FastAPI  (app.backend.main:app)
   │
   ▼
AgentOrchestrator → PlanningProvider → tools → policies / retrieval / actions
```

The browser calls FastAPI directly; CORS is configured for the UI's origin and
allows exactly the two headers the client sends. There is no Next.js API route
in between, because there is nothing for one to protect: the UI carries no
credential, holds no database handle, and knows no rule worth hiding. Adding a
proxy would add a hop and an impression of security it would not provide.

What the frontend deliberately does **not** contain:

- **No authorization logic.** It never computes, sends, or narrows an account
  scope. `POST /api/chat` carries a message and an asserted identity; the
  server resolves what that identity may reach. `mayChangeState()` in
  `lib/types.ts` decides whether to *draw* a confirm button — the backend
  re-checks the role on every confirmation regardless, so hiding the button is
  a courtesy, not a control.
- **No policy arithmetic.** Money crosses the wire as a decimal *string* and is
  never parsed into a JavaScript number; `formatAmount` trims a trailing `.00`
  and does nothing else. Parsing would reintroduce exactly the float error the
  `Decimal`-based policy engine exists to avoid.
- **No authority ranking.** `splitEvidence` partitions on the backend's own
  `is_authoritative` flag. The UI renders the precedence decision; it never
  forms one.
- **No second source of truth for identities.** The context selector is
  populated from `GET /api/principals`, so the browser can only assert an
  identity the server already knows.

### 11.2 One contract, generated

The frontend's TypeScript types are generated from the backend's own OpenAPI
document, not written by hand:

```text
app/backend/api/schemas.py
   │  scripts/export_openapi.py
   ▼
app/frontend/openapi.json          (committed)
   │  npm run generate:api  (openapi-typescript)
   ▼
src/lib/api-schema.d.ts            (committed)
   │  aliased, never re-declared
   ▼
src/lib/types.ts
```

Both generated artifacts are committed so the frontend builds and tests without
a running backend — which means both can go stale.
`tests/test_frontend_contract.py` is what stops that: it regenerates the schema
and re-records the fixtures, and fails the **backend** suite on any difference.
A field renamed in `schemas.py` therefore breaks a Python test immediately,
rather than surfacing as `undefined` in a browser weeks later.

### 11.3 Fixtures are recorded, not written

`scripts/export_ui_fixtures.py` drives the real application through
`TestClient` on the deterministic provider and saves what comes back into
`app/frontend/src/test/fixtures/`. The UI tests render those payloads.

This matters more than it first appears. A hand-written fixture tests the UI
against the developer's *belief* about the API, and keeps passing after that
belief stops being true. A recorded one cannot: the substance is checked back
against the live application on every backend test run. Volatile fields — ids,
timestamps — are replaced with stable placeholders so re-recording is a no-op
unless a response's shape or substance actually changed.

The recorded set deliberately spans the paths the UI renders differently: an
agreement override, a provisional decision, a documentation lookup, a
superseded document demoted to context, a cross-account refusal, a pending
action, its execution, and the conflict a replayed confirmation produces.

### 11.4 Rendering the structured response

`ChatResponse` is decomposed into components that each own one part of it, in
the order a reader needs them: answer, uncertainty, decision, action, then
provenance and investigation. Conclusions first, supporting material below.

| Component | Renders | Rule it holds |
| --- | --- | --- |
| `AgentMessage` | the whole turn | sections appear only when the field is present — an empty Sources block would suggest citations were merely collapsed |
| `UncertaintyNotice` | `uncertainties` | caution styling, never error styling: declining to answer is a correct outcome, not a fault |
| `DecisionCard` | `policy_decisions[]` | `requires_verification` overrides `applies`, so a provisional figure can never read as settled |
| `EvidenceSection` / `EvidenceCard` | `sources[]` | split on `is_authoritative`; deprecated material is shown, labelled, and explained |
| `InvestigationSummary` | `tools_used[]` | tool *arguments* never rendered; the raw step count is stated alongside the grouped rows |
| `ActionCard` | `proposed_action` | says "nothing has changed yet" in words while pending; both buttons disable on first submit |

Two things are never rendered, because the API never returns them: the model's
intermediate messages, and the arguments it passed to a tool. Both are planning
trace. The contract promises conclusions and provenance.

### 11.5 Tool activity without streaming

The assessment asks the interface to show which tool is being used. The API
answers a request in a single response and does not stream, so Phase 6 shows a
faithful *post-hoc* investigation summary rather than a simulated live feed.

`summariseInvestigation` groups calls by capability, preserves the order each
was first reached for, and gives a group its **worst** outcome — one refused
lookup among three must not read green. Nothing is timed, staggered, or
animated to imply progress that already finished.

The component's shape is what a live indicator would fill in: the same rows,
updated as events arrive. Streaming was not implemented because the backend
does not support it, and faking it would misrepresent what the system does.

### 11.6 Session and the confirmation gate

The backend issues a `session_id` on the first request and binds every prepared
action to it. The UI holds that session for the life of the conversation and
sends it on every later message.

It is reset in exactly two cases: an explicit "New conversation", and an
identity change. The second is the interesting one — carrying a session across
an identity change would leave a proposal made under one scope sitting in a
conversation running under another. Resetting is both safer and the honest
thing to show.

Conversation *history* is deliberately not replayed. Phase 5 deferred
conversation memory, so the session is the action/interaction context, not a
transcript the model sees. The UI does not pretend otherwise.

Confirmation is a distinct endpoint with a closed vocabulary (`approve` /
`reject`). Typing "yes" into the chat box confirms nothing, and cannot:
`/api/chat` has no path to execution at all. The UI echoes the
`parameter_fingerprint` it was shown, so the backend can refuse a proposal that
changed underneath the review. Both buttons disable on the first submit — the
backend makes execution single-use regardless, but a UI that lets a second
click through and then reports `action_not_pending` blames the user for its own
race. On a conflict the card leaves pending state, so it cannot invite a click
that can never succeed.

### 11.7 Errors

`ApiError` preserves the backend's `code`, and `ErrorNotice` chooses its
heading from it — an authorization refusal, a provider outage, a missing
dataset and an unreachable API each read as what they are. The body is the
backend's message verbatim: it was already written to be shown, and rephrasing
risks softening a refusal into something that sounds retryable.

A transport failure is reported as "cannot reach the API", not as a server
error; a non-JSON body still raises rather than becoming a blank render. No
stack trace, no configuration value and no raw `details` dump crosses into the
UI.

Note the case that is *not* an error: a cross-account request returns HTTP 200
with a `not_found` tool status, because the tool layer refuses to distinguish
"forbidden" from "missing" and thereby confirm another customer's record
exists. The UI surfaces that refusal as the agent's answer, preserving the
behaviour rather than promoting it to an error banner.

### 11.8 Design system

CSS Modules with custom properties in `src/app/globals.css`; no UI framework
and no styling dependency. One neutral ramp, one accent, and three semantic
tones — ok, caution, fail — which carry the only colour with meaning in the
product, so the vocabulary is learned once.

Colour is never the sole carrier of meaning: every tone is paired with a text
label, so the interface reads correctly in greyscale and to a screen reader.
The confirm and reject buttons differ in shape and weight as well as colour.
Light and dark are both defined. Accessibility is handled by using the platform
— a real `<form>`, a labelled `<textarea>`, `<details>` for evidence disclosure,
semantic `<button>`s, one visible focus treatment, and
`prefers-reduced-motion` honoured.

### 11.9 What Phase 6 implemented, and what it did not

**Implemented now:** a Next.js chat page consuming the Phase 5 API; a
server-driven demo context selector with a visible active scope; typed API
client generated from the backend contract; evidence rendering split by
authority with expandable cards; policy decisions with rule, arithmetic and
citations; post-hoc investigation summary; distinct uncertainty treatment; the
confirmation card with duplicate-submit prevention and all terminal action
states; session binding across a conversation; structured error rendering;
a UI test suite over recorded API responses (57 at the close of Phase 6; see
the README for the current count).

**Deferred:** streaming tool activity; conversation memory; a proactive-issue
dashboard; production authentication; actual hosting. See §13.

**One backend change was required**, and it was the smallest available:
`GET /api/principals` now returns the directory in declaration order rather
than sorted by id, and `MOCK_PRINCIPALS` leads with the support agent. A client
offering the first entry as its default should land on the primary
internal-support persona; sorting alphabetically put a narrow customer context
first for no reason anyone chose. No behaviour, scope or permission changed.

## 12. Deployment configuration

Two containers and one persistent volume — the same "no infrastructure the
application does not need" principle §11.9's dashboard-deferral and §9's
SQLite choice already apply, extended to hosting. No queue, no cache, no
managed database: the whole dataset is a handful of rows across six
documents.

```text
Next.js frontend  --->  FastAPI backend  --->  SQLite (named volume)
     :3000                  :8000                     |
                                              --->  OpenAI API (LLM_PROVIDER=real)
```

`Dockerfile.backend` builds from the **repo root** as context, not
`app/backend/`, because the application imports as `app.backend.*` and needs
`scripts/` (ingestion) and `data/source/` (the supplied pack) alongside it.
`app/frontend/Dockerfile` is self-contained, building from `app/frontend/`
alone. `docker-compose.yml` wires both together with one named volume mounted
at `/app/data/processed` on the backend container — matching
`REPO_ROOT / "data" / "processed" / "parcelpilot.db"` (§7.2), so no path
configuration is needed beyond the existing default.

**The database is never baked into the image.** `docker-entrypoint.sh` runs
`ingest_dataset.py` + `ingest_documents.py` on container start only when the
database file is not already present at the volume path. This is the
"persisted volume, initialized during deployment" branch of the two options
a production database can take here, chosen over "always rebuild on every
start" specifically so that a container restart does not wipe an
in-progress demo's confirmed actions — `agent_actions` / `ticket_escalations`
/ `ticket_notes` are untouched by either ingestion script (§9.6), but a
from-scratch SQLite file on an ephemeral filesystem would still lose them on
every restart if ingestion re-ran unconditionally.

**`NEXT_PUBLIC_API_BASE_URL` is a build-time value, not a runtime one** —
verified directly, not assumed: building the frontend image with an
overridden value produces that value verbatim inside the compiled
`.next/static/chunks/*.js`, and setting the same variable on `docker run`
against an already-built image changes nothing the browser receives. The
frontend must be rebuilt per target backend URL; this is standard Next.js
behaviour (`NEXT_PUBLIC_*` inlining), not a limitation specific to this
repository, but it is easy to assume otherwise and deploy a frontend that
silently points at the wrong backend.

Nothing above changes how the application decides anything — `LLM_PROVIDER`
still fails closed with no key (§10.4), CORS still governs which browser
origins may call the API, and the confirmation gate, authorization boundary,
and source-authority resolution are exactly the code paths documented in
§§8–10, run inside a container instead of a host process.

Not included: a CI workflow, and platform-specific configuration for any
particular host (Vercel/Render/Railway/Fly/...). Docker Compose is the
portable baseline any of those can build from; choosing one is left to
whoever hosts this. SQLite plus a single volume implies single-writer
semantics — correct for a demo, revisit before multiple backend instances.

## 12A. First-response SLA evaluation (Phase 7)

`app/backend/policies/sla.py` is the third deterministic calculator, built on
exactly the machinery §9 describes: it gathers `support_response` evidence
scoped to one account, layers it weakest-authority-first, and measures elapsed
time against the dataset snapshot rather than the wall clock.

**Target selection.** Two clause shapes are recovered by
`extract_response_targets`. The current policy states targets as a per-plan
table, which PDF extraction flattens into a header block followed by one
label-then-values run per plan; the parser locates the plan's own row and reads
the three values that follow, and yields nothing if that shape does not hold. A
customer agreement instead states targets inline (`P1: 15 minutes, 24x7`).
Because agreements layer last, an agreement overrides the severities it
actually names and leaves the rest at the plan default — the same field-by-field
overlay the cancellation and credit terms use, with no rule naming a customer.

**Severity is not computed.** The tool takes an optional `severity` and will
not derive one. This was not the first design: an earlier draft scored ticket
text against the policy's severity definitions and took the best match. On the
supplied corpus it rated a billing question P1 on a single shared word and then
reported a breach against a 15-minute target. Word overlap is not evidence of
business impact, and a breach verdict is exactly the kind of claim that must not
rest on a guess, so the classifier was deleted rather than tuned. Called without
a severity the tool reports the elapsed time and every target it read, and
asserts nothing further. The deterministic planner passes through a severity the
*request* states — reading a label the caller supplied, not judging one — and the
model-backed provider is instructed to classify against the definitions and pass
its answer explicitly.

**Business time is not converted.** The corpus states targets such as
"4 business hours" and defines no business calendar anywhere. Converting one
into a deadline would invent the calendar and the verdict together, so those
targets are reported with `target_minutes = None` and the breach question left
open.

**Escalation.** `requires_immediate_escalation` is set for P1 whenever the
governing documents carry the standing instruction to escalate P1 immediately,
independently of the arithmetic — a P1 inside its target still escalates. The
orchestrator's `_should_escalate` also now recommends escalation on a settled
breach, because the current policy directs that a breached target be stated and
escalated rather than reported quietly, and a settled decision carries no
uncertainty flag that would otherwise catch it.

### 12B. Scoping a known issue to what it documents

KI-211 documents a bounded, carrier-specific lag between collection and pickup
confirmation. The service-credit engine originally treated *any* unconfirmed
pickup as grounds for verification, citing that issue. Two problems: the
carrier and window it names were ignored, and by the time a credit threshold
(hours) is crossed, a confirmation lag (minutes) can no longer be the
explanation. The effect was that every failed-pickup credit a signed agreement
granted came back deferred.

`extract_pickup_confirmation_lag` now matches the *order's own recorded
carrier* against the known-issue text — so no carrier is named in code — and
returns the bound that text states. The lag is recorded on every credit
decision as an audit input, showing the engine considered the issue and found
it inapplicable, but it no longer defers a decision: at that point carrier
fault is either recorded (the carrier has accepted the collection did not
happen) or unknown (already deferred on its own terms).

Where the lag *does* change an outcome is cancellation, and that is where it is
now applied: cancelling on a stale BOOKED status would cancel a parcel that was
in fact collected, and that mistake is made inside the documented window rather
than hours later. A cancellation inside the window cites the issue by name; one
well past it gets the generic caution instead, because the known issue no
longer explains the missing confirmation.

## 13. Deferred decisions

Resolved in Phase 2: SQLite schema, timestamp handling, provenance design.
Resolved in Phase 3: chunking strategy, retrieval approach, index storage,
document authority model, account-scoped retrieval.
Resolved in Phase 4: orchestration loop, tool boundaries, policy engine,
generic agreement overrides, action confirmation, authorization context.
Resolved in Phase 5: API boundary, mock auth context, provider selection,
response contract, confirmation endpoint, error envelope.
Resolved in Phase 6: frontend boundary, generated API contract, recorded UI
fixtures, evidence and tool-activity presentation, session handling in the UI.
Resolved in Phase 7: deterministic first-response SLA targets and breach
detection (§12A), carrier-scoped known-issue application (§12B), and the
authority the model's prose does and does not carry (§10.12).

Still open, to resolve when the relevant phase starts:

- **The UI shows tool activity after the fact, not live.** The API answers a
  request in one response and does not stream, so `InvestigationSummary`
  renders a completed investigation. The component is shaped so a live
  indicator would fill in the same rows; implementing it needs a streaming
  endpoint first, and simulating progress in the meantime would misrepresent
  what the system does.
- **The frontend has no conversation memory, because the backend has none.**
  A `session_id` is issued and binds prepared actions, but no prior turn is
  replayed to the model. Each request is answered independently. Replaying
  history in the UI alone would create the *appearance* of memory the agent
  does not have, which is worse than not having it.
- **Agreement term dates are still stored but unused.** `term_start` /
  `term_end` are parsed from each agreement, but authority keys on the stated
  `Status: ACTIVE` alone. Checking that an agreement is in force *as of the
  snapshot* is a natural extension of the Phase 4 evaluation context and was
  left out deliberately: every supplied agreement is in term, so implementing
  it now would add an untested branch.
- ~~**SLA response targets are not yet computed.**~~ **Resolved in Phase 7**
  by `policies/sla.py` and the `evaluate_sla` tool, following the same pattern
  as the other two calculators — see §12A. Two bounded refusals remain by
  design: severity is never inferred, and a target stated in business hours
  yields no breach verdict because the corpus defines no business calendar.
- **Topic classification is keyword-based** over section headings. It maps
  the supplied corpus exactly, but a new document with an unfamiliar heading
  falls back to `general` and would not participate in topic-scoped
  overrides. Revisit if the corpus grows.
- **Term extraction is pattern-based.** It is written against the wording the
  supplied corpus uses and fails closed — an unparseable clause yields
  `None`, which surfaces as verification rather than a wrong figure. A corpus
  with more varied phrasing would need the patterns widened, with the same
  fail-closed guarantee.
- **Structured records are not yet authority-ranked.** Section 2's tiers 3–4
  (operational facts; historical tickets) exist in Phase 2's tables but are
  not expressed as tiers alongside document evidence.
  `tickets.historical_resolution` is flagged as non-authoritative when
  returned, which covers the immediate hazard; a unified ranking is later work.
- **Only two action types exist** (`create_escalation`, `add_ticket_note`),
  which is the smallest set that demonstrates the confirmation mechanism
  generalises. More types are additive.
- **Monthly service-credit caps are surfaced, not enforced.** A decision
  reports the cap and advises checking credits already issued; aggregating
  spend across a month needs issuance history the dataset does not contain.
- **The manager-approval threshold is computed, not enforced, and
  `SUPPORT_MANAGER` therefore grants nothing `SUPPORT_AGENT` does not.** The
  SOP's "any individual credit above INR 1,000 requires manager approval" is
  evaluated by `policies/service_credit.py` and reported on the decision, but
  no gate consumes it: `AgentContext.may_change_state` is the only role check
  the system enforces, and it admits both internal staff roles. This is a
  consequence of the action set, not an oversight — neither `create_escalation`
  nor `add_ticket_note` issues a credit, so there is nothing for the threshold
  to gate. The role is modelled so the capability has somewhere to attach; the
  moment a credit-issuing action exists, that is where the gate belongs. Adding
  the check now would mean enforcing a rule against an operation that cannot
  occur, and testing it would require inventing that operation.
- **Conversation memory is not implemented.** `POST /api/chat` issues and
  echoes a `session_id`, and prepared actions are bound to it, but no prior
  turn is replayed to the model: each request is investigated from scratch.
  This is honest rather than accidental — replaying a transcript into a
  tool-calling loop needs a retention and re-authorization story (a message
  from an earlier turn was answered under the scope in force *then*) that
  Phase 5 did not have a reason to settle.
- **Authentication is a mock.** `auth/principals.py` resolves an asserted
  identity against a fixed directory. The boundary it protects is real — the
  server, never the request, decides the scope — but there is no token, no
  signature and no session store. `AUTH_SECRET_KEY` / `AUTH_TOKEN_TTL_MINUTES`
  are reserved for the provider that replaces it.
- **Responses are not streamed.** A multi-step investigation returns as one
  body. A chat UI will want incremental tool-step events; that is a transport
  change (SSE or websocket) over the same orchestration loop, not a
  re-architecture.
- **The real provider targets OpenAI chat completions specifically.** The
  seam is vendor-neutral and `OPENAI_BASE_URL` covers compatible gateways, but
  a genuinely different vendor means a second `PlanningProvider`
  implementation — which is the shape the abstraction intends, not a gap in
  it.
