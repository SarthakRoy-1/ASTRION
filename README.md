# ParcelPilot Support & Operations AI Agent

> **Status: Phase 6 — working chat UI on the Phase 5 API.** Source pack
> verified (Phase 1); SQLite structured-data layer (Phase 2); document
> ingestion and authority-ranked retrieval (Phase 3); agent orchestration,
> deterministic policy decisions, and confirmation-gated actions (Phase 4);
> FastAPI surface, mock auth context, and an OpenAI-backed provider behind the
> Phase 4 seam (Phase 5); Next.js chat interface with evidence, tool activity
> and the confirmation gate (Phase 6).
>
> The whole stack runs end to end with **no API key**:
> `LLM_PROVIDER=deterministic` is the default and exercises every safety
> boundary, which is also what both test suites run on. No production
> authentication provider yet. Nothing is hosted.

## Purpose

An internal assistant for authorised ParcelPilot support and operations staff.
Given a natural-language question, it retrieves the relevant policy and
agreement text, looks up the relevant account/order/ticket records, applies
ParcelPilot's rules deterministically, and answers with its sources shown — or
escalates when it cannot answer safely.

It is explicitly **not** a "chat with your PDFs" wrapper. See
[Architecture principle](#architecture-principle) below.

## Assessment context

Built for the ParcelPilot AI Engineer assessment. The system must support:

| Capability | Notes |
| --- | --- |
| Natural-language Q&A | Free-form questions from support staff |
| Document retrieval | Over the supplied policy/SOP/agreement pack |
| Account / order / ticket lookup | Structured operational records |
| Deterministic policy calculation | SLA, cancellation fees, service credits |
| Customer-agreement precedence | Signed agreements override general policy |
| SLA reasoning | Response/resolution targets, breach detection |
| Known-issue reasoning | Match a symptom to a documented known issue |
| Multi-step tool use | Chain retrieval → lookup → calculation |
| Role/account-based access control | Enforced in code, never by prompt |
| Action preparation + confirmation | Nothing mutates without explicit approval |
| Escalation | Declare uncertainty rather than guess |
| Proactive issue detection | Surface risks the user did not ask about |

### Required agent tools

At minimum three distinct tools — all implemented in Phase 4
(`app/backend/tools/`):

1. **Document search / retrieval** — `search_documents`, `get_document_evidence`;
   authority-ranked search over the source pack.
2. **Structured lookup + calculation** — `lookup_record` for records, plus
   `evaluate_cancellation` / `evaluate_service_credit` for deterministic rule
   evaluation.
3. **State-changing action** — `prepare_escalation` / `prepare_ticket_note`
   produce a proposal; execution happens only via an explicit confirmation
   call that no tool can reach — `POST /api/actions/{id}/confirm`.

## Architecture principle

A hard split, enforced by module boundaries:

**The LLM handles** natural-language understanding, tool selection, multi-step
orchestration, reasoning over retrieved evidence, explanation, and deciding when
it is uncertain enough to escalate.

**Deterministic Python handles** authorization, account scoping, source
precedence, cancellation calculation, service-credit calculation, validation,
and every state change.

Phase 5 put a real OpenAI-backed provider behind the `PlanningProvider` seam
Phase 4 defined, without changing the orchestrator, the tools, or the policy
engine. The deterministic planner remains a first-class supported mode — it is
what CI and the whole test suite run on, so every safety boundary stays
verifiable with no API key and no network. Swapping the provider changes which
tools get called, not what any of them are permitted to do; a test asserts
both implementations satisfy the same seam signature.

The model never computes a number that a customer would see on an invoice, and
never decides whether a user is allowed to see a record. Those paths are code,
and they are unit-testable without an API key.

## Source authority

The supplied pack deliberately contains contradictions: a current policy and a
deprecated one, customer-specific agreements that override general terms, and
historical ticket resolutions that may simply be wrong. Every retrieved chunk
carries an authority tier, and conflicts resolve top-down:

```text
1. Active signed customer agreement       (highest — customer-scoped)
2. Current support policy / SOP / product documentation
3. Structured operational facts           (accounts, orders, tickets, SLAs)
4. Historical tickets & internal notes    (context only — never authority)
```

Rules (implemented for document sources in Phase 3 —
`app/backend/retrieval/authority.py`):

- A document marked deprecated is **never** cited as current policy. It may be
  surfaced only to explain that a rule changed.
- A customer agreement outranks general policy **only for that customer's**
  accounts — and only when the question names that account.
- A past ticket resolution is evidence of what someone did, not of what is
  correct. It cannot, on its own, justify an answer.
- When tiers genuinely conflict and precedence does not settle it, the agent
  says so and escalates.

Design detail lives in [docs/architecture.md](docs/architecture.md).

## Planned technology stack

| Layer | Choice | Notes |
| --- | --- | --- |
| Frontend | Next.js 16 + React 19 + TypeScript | App Router; chat UI with citations, tool activity and confirmation cards — **built, Phase 6** |
| Backend | Python 3.13 + FastAPI | Typed request/response models — **built, Phase 5** |
| Database | SQLite (stdlib `sqlite3`) | Regenerated from the source pack; lives in `data/processed/` — **built, Phase 2** |
| PDF parsing | PyMuPDF | Font-size-aware section detection + page numbers for citation — **built, Phase 3** |
| Excel parsing | openpyxl | Loading `ParcelPilot_Assessment_Data.xlsx` — see Known environment notes on `pandas` |
| Validation | Pydantic v2 | Tool arguments, API contracts, config — **in use, Phase 2/3**: typed record and evidence models |
| Retrieval | BM25 over SQLite, in-process | **Built, Phase 3.** 26 chunks; no vector store or FTS5 index — see architecture §8.5 |
| LLM | Provider-neutral seam (`PlanningProvider`) | **Phase 4** deterministic planner + **Phase 5** OpenAI provider; selected by `LLM_PROVIDER` |
| Frontend styling | CSS Modules + custom properties | No UI framework; design tokens in `src/app/globals.css` — **built, Phase 6** |
| Tests | pytest + Vitest | Policy math, access control and UI rendering, all without live LLM calls |

Every row is implemented as of Phase 6. Backend versions are pinned in
[`requirements.txt`](requirements.txt), frontend versions in
[`app/frontend/package.json`](app/frontend/package.json).

## Local development prerequisites

Verified present on the development machine:

- **Python 3.13** — `py -3.13 --version`. (3.14 is the system default here but
  3.13 is the pinned target; see [Known environment notes](#known-environment-notes).)
- **Node.js 24.11.0** and **npm 11.6.1** — `node --version`
- **Git 2.51.2** — `git --version`

Optional / not currently installed:

- **GitHub CLI (`gh`)** — needed only to create the remote from the terminal.
- **Docker** — installed but the daemon is not running. Not required; the whole
  stack runs natively.

## Setup

### The short version

Two terminals, no API key, no network:

```powershell
# Terminal 1 — API
py -3.13 -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\ingest_dataset.py; python scripts\ingest_documents.py
uvicorn app.backend.main:app --reload            # http://127.0.0.1:8000

# Terminal 2 — UI
cd app/frontend; npm install; npm run dev        # http://localhost:3000
```

Open <http://localhost:3000>, pick a context in the header, and ask something.
The full walkthrough is below.

### Build the data and run the tests — Windows, PowerShell, Python 3.13

Steps 1–7 need **no API key and no network**. The agent runs on the
deterministic planner by default, so the whole stack works offline and every
test is reproducible.

```powershell
# 1. Create/activate the venv (Python 3.13 pinned; see Known environment notes)
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Place the supplied PDFs and XLSX in data/source/ — see data/source/README.md
#    Then verify the pack: presence, no unexpected files, readability, SHA-256
python scripts/verify_source_pack.py

# 4. Inspect PDF/XLSX structure; writes data/processed/source_inspection.json
python scripts/inspect_sources.py

# 5. Build the structured-data layer: creates/refreshes
#    data/processed/parcelpilot.db from the workbook. Safe to re-run --
#    each run wipes and reloads the data tables from the current workbook.
python scripts/ingest_dataset.py

# 6. Build the document/evidence layer from the six PDFs into the same
#    database. Also safe to re-run, and order-independent with step 5 --
#    the two scripts write disjoint tables and never clobber each other.
python scripts/ingest_documents.py

# 7. Run the full test suite (Phases 1-5)
python -m pytest tests/ -v

# 8. Start the API
uvicorn app.backend.main:app --reload --port 8000
```

`http://127.0.0.1:8000/docs` serves the generated OpenAPI documentation.

Both ingestion steps write into the same SQLite file and can be run in either
order, repeatedly. Step 6 is required before the agent can answer policy
questions, since precedence is resolved from the ingested documents.

Equivalent commands from Git Bash / WSL (`source .venv/Scripts/activate`
instead of `.venv\Scripts\Activate.ps1`) work the same way.

> Note: if console output includes non-ASCII characters (e.g. from PDF text),
> set `$env:PYTHONIOENCODING = "utf-8"` first to avoid a `UnicodeEncodeError`
> on Windows' default cp1252 terminal encoding. Scripts always write their
> file output (JSON reports, the SQLite database) as UTF-8 regardless of this
> setting.

The generated database lives at `data/processed/parcelpilot.db`. It is
git-ignored and fully regenerable — delete it and re-run steps 5 and 6 at any
time; nothing outside `data/processed/` depends on its prior contents.

- [Structured-data layer](docs/architecture.md#7-structured-data-layer-phase-2)
  — schema, timestamp handling, provenance.
- [Document / evidence layer](docs/architecture.md#8-document--evidence-layer-phase-3)
  — chunking, source authority, conflict resolution, account scoping.
- [Agent, tools and actions](docs/architecture.md#9-agent-tools-and-actions-phase-4)
  — orchestration, tool boundaries, the deterministic policy engine, and the
  confirmation gate on state-changing actions.
- [Application API and real agent integration](docs/architecture.md#10-application-api-and-real-agent-integration-phase-5)
  — the FastAPI boundary, the authorization context, the provider abstraction
  and its two implementations, the tool loop, the response contract, the
  confirmation API, and error handling.

### Running with a real LLM (optional)

The deterministic planner is the default and needs no configuration. To run
the agent on a live model instead:

```powershell
cp .env.example .env
# then edit .env:
#   LLM_PROVIDER=real
#   OPENAI_API_KEY=sk-...your real key...
#   OPENAI_MODEL=gpt-4o

uvicorn app.backend.main:app --reload --port 8000
```

Selection is explicit and there is **no fallback**. `LLM_PROVIDER=real`
without a usable `OPENAI_API_KEY` fails at startup with a clear message rather
than quietly running the rule-based planner — an operator who asked for a live
model and silently got a different one would be shipping a different system
than they think. Confirm which mode is live with `GET /health`.

Nothing else changes: the tools, the policy engine, account scoping and the
confirmation gate are identical in both modes. The `openai` SDK is imported
lazily, so the application and the test suite run on a machine that never
installed it.

### Frontend — the chat UI

The UI is a pure client of the API above. It holds no database connection, no
secrets and no business rules, so it needs no configuration beyond the address
of the backend.

```powershell
cd app/frontend
npm install
npm run dev          # http://localhost:3000
```

Run the backend in a second terminal (`uvicorn app.backend.main:app --reload`).
The UI defaults to `http://127.0.0.1:8000`; point it elsewhere by copying
`app/frontend/.env.example` to `app/frontend/.env.local` and setting
`NEXT_PUBLIC_API_BASE_URL`. The backend's `CORS_ALLOW_ORIGINS` must include the
UI's origin — `http://localhost:3000` is allowed by default.

No API key is involved. With `LLM_PROVIDER` unset the agent runs on the
deterministic planner, and the entire UI — evidence, policy decisions,
uncertainty, the confirmation gate — is fully exercisable offline.

#### Using the demo contexts

The header's **Context** selector switches which identity the conversation runs
as. Its options come from `GET /api/principals`, so the browser can only assert
an identity the server already knows, and the strip beneath it always shows the
active role and the accounts that identity may reach.

| Context | Role | Sees | Can confirm actions |
| --- | --- | --- | --- |
| ParcelPilot support agent | `support_agent` | all accounts | yes |
| ParcelPilot support manager | `support_manager` | all accounts | yes |
| ParcelPilot support (read-only) | `read_only` | all accounts | no |
| Northstar Logistics (customer) | `customer` | ACCT-001 only | no |
| LumenWorks (customer) | `customer` | ACCT-002 only | no |

Switching context starts a new conversation: a session binds prepared actions,
and carrying one across an identity change would leave a proposal made under
one scope sitting in a conversation running under another.

To see account isolation, select **Northstar Logistics (customer)** and ask for
a LumenWorks order — the refusal comes from the backend's SQL-level scoping,
not from the UI hiding anything. The browser never sends an account scope at
all.

#### Frontend tests

```powershell
cd app/frontend
npm test             # Vitest, jsdom, no network and no API key
npm run typecheck
```

The UI tests render real recorded API responses. `scripts/export_ui_fixtures.py`
captures them from the live application, and `tests/test_frontend_contract.py`
fails the **backend** suite if any fixture — or the OpenAPI document the
frontend generates its TypeScript from — goes stale. Regenerate both after
changing an API schema:

```powershell
python scripts\export_openapi.py
python scripts\export_ui_fixtures.py
cd app/frontend; npm run generate:api
```

## Deployment

Two containers, one persistent volume — no queue, no cache, no managed
database. The whole dataset is a handful of rows across six documents;
anything heavier would be infrastructure the application does not need.

```text
Next.js frontend  --->  FastAPI backend  --->  SQLite (persistent volume)
     :3000                  :8000                     |
                                              --->  OpenAI API (LLM_PROVIDER=real)
```

`Dockerfile.backend` (repo root, build context = repo root — the app imports
as `app.backend.*` and needs `scripts/` and `data/source/`),
`app/frontend/Dockerfile` (context = `app/frontend/`), and
`docker-compose.yml` wire this together.

```powershell
docker compose up --build
#   frontend  http://localhost:3000
#   backend   http://localhost:8000  (docs at /docs, health at /health)
```

For `LLM_PROVIDER=real`, put `OPENAI_API_KEY` in a git-ignored `.env` next to
`docker-compose.yml` — compose reads it automatically. Never in a committed
file.

### The database is never baked into the image

`docker-entrypoint.sh` runs `ingest_dataset.py` + `ingest_documents.py` on
container start *only if* `data/processed/parcelpilot.db` is not already
present at the mounted volume path — so a fresh deployment always builds
correctly from `data/source/` alone, and a restart of an existing deployment
does not silently wipe an in-progress demo's confirmed actions. The deployed
backend never depends on a database file that exists only on a developer's
machine.

### `NEXT_PUBLIC_API_BASE_URL` is a build-time value

Next.js inlines every `NEXT_PUBLIC_*` variable into the compiled client
JavaScript at `next build`. Setting it with `docker run -e` (or an
environment variable on an already-built image) has **no effect** — the
browser bundle was already written. Rebuild the frontend image for each
backend URL you deploy against:

```powershell
docker compose build --build-arg NEXT_PUBLIC_API_BASE_URL=https://api.example.com frontend
```

This was verified directly: building with an overridden value greps back out
of the compiled `.next/static/chunks/*.js`, and setting the same variable
afterward on a running container changes nothing.

### What is not included

No CI workflow and no platform-specific config (Vercel/Render/Railway/Fly)
exist in this repository — Docker Compose is the portable baseline any of
those can build from, but choosing and configuring one is a deliberate step
left to whoever hosts this, not assumed here. SQLite plus a single volume
implies single-writer semantics, which is correct for a demo and would need
reconsideration before scaling to multiple backend instances.

## API

Six endpoints. Full schemas at `/docs` once the server is running.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness, active provider mode, whether the data is built |
| `POST /api/chat` | One natural-language request; returns answer + evidence + decisions |
| `POST /api/actions/{action_id}/confirm` | Execute or reject a prepared action |
| `GET /api/actions/pending` | Proposals awaiting confirmation, within your scope |
| `GET /api/actions/{action_id}` | Audit record: state, timeline, effect |
| `GET /api/principals` | The mock identities this deployment accepts |

### Identity

Every request asserts an identity — `user_id` in the body, or the
`X-ParcelPilot-User` header. The **server** resolves it to a role and an
account scope; the request never states its own permissions, and nothing in
the message can widen them.

```text
customer.northstar    customer         ACCT-001 only
customer.lumenworks   customer         ACCT-002 only
support.agent         support_agent    every account; may prepare and confirm
support.manager       support_manager  every account; may approve escalations
support.readonly      read_only        every account; may not change any state
```

Authentication itself is a **mock** in this phase: there is no token and no
signature. What is real is the boundary — the server, never the request,
decides the scope, and enforcement lives in SQL below the model.

### Ask a question

```bash
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "X-ParcelPilot-User: support.agent" \
  -d '{"message": "Can Northstar cancel ORD-1001 without a cancellation fee?"}'
```

```jsonc
{
  "outcome": "answered",
  "answer": "Order ORD-1001 can be cancelled with no cancellation fee. Rule applied: ...",
  "sources": [
    { "source_file": "05_Northstar_Logistics_Enterprise_Agreement.pdf",
      "page": 1, "section": "2. Shipment cancellation",
      "is_authoritative": true, "excerpt": "..." }
  ],
  "tools_used": [
    { "step": 1, "tool_name": "lookup_record",         "status": "ok" },
    { "step": 2, "tool_name": "evaluate_cancellation", "status": "ok" },
    { "step": 3, "tool_name": "search_documents",      "status": "ok" }
  ],
  "policy_decisions": [
    { "decision_type": "cancellation", "outcome": "allowed",
      "amount": "0.00", "currency": "INR",
      "calculation": "fee waived by customer agreement -> 0",
      "controlling_rule": "...", "overrides": ["... outranks ..."] }
  ],
  "uncertainties": [],
  "action_status": "none",
  "reference_time": "2026-08-16T11:00:00+05:30",  // dataset snapshot, not today
  "session_id": "SES-...", "account_scope": ["ACCT-001", "..."]
}
```

Cross-account access is refused below the model. A customer scoped to
ACCT-001 asking about ACCT-002 gets the same "not found within your scope"
they would get for a record that does not exist — the API is not an existence
oracle for other customers' data.

### Prepare and confirm an action

`POST /api/chat` can prepare a state change and nothing more; the response
comes back `action_status: "pending_confirmation"` with a preview and an
`action_id`. Execution is a separate call with a closed vocabulary
(`approve` / `reject`) — typing "okay" into the chat endpoint confirms
nothing, because that endpoint has no execution path at all.

```bash
curl -X POST http://127.0.0.1:8000/api/actions/ACT-abc123/confirm \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve",
       "user_id": "support.manager",
       "session_id": "SES-...",
       "expected_fingerprint": "..."}'
```

Confirmation re-validates under the *confirming* caller: the action must exist
in their account scope, belong to that conversation, still be pending, not
have expired, still have a live target, and — when `expected_fingerprint` is
supplied — still describe exactly what was reviewed. It executes exactly once;
a replay returns `409 action_not_pending`.

### Errors

Every failure returns one envelope with a stable code — never a stack trace,
a SQL fragment, or a configuration value.

```json
{"error": {"code": "action_not_pending", "message": "...", "details": {}, "request_id": "REQ-..."}}
```

`validation_error` (422) · `unauthenticated` (401) · `forbidden` (403) ·
`not_found` (404) · `action_not_pending` / `action_session_mismatch` (409) ·
`provider_not_configured` / `data_unavailable` (503) · `provider_error` (502) ·
`provider_timeout` (504) · `internal_error` (500).

## Environment variables

Full template with defaults: [`.env.example`](.env.example). Copy it to `.env`;
`.env` is git-ignored and must never be committed. Every value has a working
default — **no `.env` is required** to run in deterministic mode.

| Variable | Purpose |
| --- | --- |
| `LLM_PROVIDER` | `deterministic` (default, no key) or `real`. No fallback between them. |
| `OPENAI_API_KEY` | Required only when `LLM_PROVIDER=real`. |
| `OPENAI_MODEL` / `OPENAI_BASE_URL` | Chat model; base URL for a compatible gateway |
| `AGENT_MAX_TOOL_STEPS` | Caps the orchestration loop |
| `AGENT_REQUEST_TIMEOUT_SECONDS` / `LLM_TEMPERATURE` | Provider call tuning |
| `DATABASE_URL` | SQLite path (under `data/processed/`) |
| `APP_ENV` / `CORS_ALLOW_ORIGINS` | Environment label; browser origins allowed to call the API |
| `AUTH_SECRET_KEY` / `AUTH_TOKEN_TTL_MINUTES` | Reserved for the real identity provider; unused today |
| `ENABLE_STATE_CHANGING_ACTIONS` | Kill switch: unregisters the preparation tools *and* closes the confirm endpoint |
| `NEXT_PUBLIC_API_BASE_URL` | Frontend → backend base URL. Set in `app/frontend/.env.local`, not in the backend `.env` — it is a browser-visible value and must never hold a secret. |

## Project layout

```text
.
├── app/
│   ├── frontend/            # Next.js + TypeScript chat UI
│   │   ├── openapi.json     # Exported backend contract (generated)
│   │   └── src/
│   │       ├── app/         # App Router: layout, chat page, design tokens
│   │       ├── components/  # Header, composer, evidence, decisions, action card
│   │       ├── hooks/       # useConversation — session, turns, confirmation
│   │       ├── lib/         # client.ts, types.ts, presentation.ts, api-schema.d.ts
│   │       └── test/        # Recorded API fixtures + fetch stub
│   └── backend/
│       ├── main.py          # ASGI entry point: uvicorn app.backend.main:app
│       ├── api/             # app.py, routes.py, schemas.py, dependencies.py, errors.py
│       ├── agent/           # orchestrator.py, provider.py, openai_provider.py,
│       │                    #   factory.py, prompts.py, composer.py
│       ├── tools/           # Tool definitions exposed to the LLM
│       ├── policies/        # Deterministic rules: cancellation, credits, terms
│       ├── retrieval/       # extraction.py, authority.py, search.py
│       ├── auth/            # principals.py — mock identity → AgentContext
│       ├── services/        # database.py, records.py, documents.py, actions.py
│       ├── models/          # records, documents, policy, actions, agent (Pydantic)
│       └── core/            # config.py, errors.py
├── data/
│   ├── source/              # Supplied assessment pack (inputs — see its README)
│   ├── processed/           # Generated SQLite + extracted text (git-ignored)
│   └── index/               # Generated retrieval index (git-ignored)
├── scripts/                 # Ingestion, verification, DB build, contract export
├── tests/                   # pytest suite
├── docs/
│   ├── architecture.md      # Technical design
│   └── product.md           # Product behaviour and scope
├── .env.example
└── README.md
```

`data/processed/` and `data/index/` are fully regenerable from `data/source/`;
nothing in them is a source of truth.

## Implementation phases

| Phase | Scope | State |
| --- | --- | --- |
| **0** | Repo init, structure, tooling recon | **Done** |
| **1** | Source-pack verification; PDF/XLSX structural inspection | **Done** |
| **2** | SQLite schema + ingestion from source; deterministic record access | **Done** |
| **3** | Document ingestion, chunking, authority ranking, evidence retrieval | **Done** |
| **4** | Agent orchestration, tools, policy engine, confirmed actions | **Done** |
| **5** | FastAPI surface; mock auth context; real LLM provider; confirmation API | **Done** |
| **6** | Next.js UI: chat, citations, tool activity, confirmation cards | **Done** |
| **7** | Proactive issue detection; further action types; evaluation | Next |
| **8** | Deployment configuration (Docker Compose, entrypoint ingestion, prod build) | **Done** — hosting itself pending an actual platform/account |

Phase 5 absorbed what earlier planning had split across phases 5, 6 and 8:
the authorization hook the Phase 2/3 repositories already carried needed a
caller to supply it, and that caller is the API — so building the API,
the auth context and the live provider separately would have meant three
passes over the same seam.

## Known environment notes

- **Python 3.13 is pinned**, not the system-default 3.14. Both resolve the core
  dependencies, but 3.13 has broader wheel coverage across the retrieval stack.
  Create the venv with `py -3.13 -m venv .venv`.
- **This repository lives inside a OneDrive-synced folder.** Filesystem writes
  are noticeably slow and sync can interfere with `node_modules/` and open
  SQLite files. Consider excluding the folder from OneDrive sync, or relocating
  the repo outside OneDrive, before Phase 1.
- **`pandas` is deliberately not a dependency**, despite appearing in earlier
  planning. In this environment, `import pandas` hangs indefinitely (tested
  90s+ with no return) in the project's `.venv` — reproducible, not a fluke,
  suspected `pandas`/`numpy` version mismatch or OneDrive interference on
  first import. `openpyxl` alone fully covers XLSX reading for Phase 1 and 2
  (Phase 2 was also asked to avoid `pandas` explicitly), so it was dropped
  from `requirements.txt` rather than debugged. Revisit if a future phase
  has a concrete reason to need it.
