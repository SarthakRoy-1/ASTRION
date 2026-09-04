# Security

How ParcelPilot is defended, what it is defended against, and — the part that
matters most — what it is **not** defended against.

This document makes no claim that the system is secure in an absolute sense.
It describes a set of controls chosen for this architecture, this stack and
this deployment model, states the assumptions each one rests on, and lists the
residual risks in [Known limitations](#known-limitations).

---

## 1. Security architecture

### The load-bearing decision

The original design already had the right shape, and the security work
preserved it rather than replacing it:

> **The LLM reasons; deterministic code decides.**

Authorization, tenant scoping, policy arithmetic and state changes are all
enforced *below* the model layer, in code the model cannot reach or influence.
A prompt-injection payload in a document cannot widen scope, because scope is a
`WHERE` clause compiled before the model ever sees a result.

### Trust boundaries

```
┌───────── UNTRUSTED ─────────┐┌──────────── TRUSTED (server) ─────────────┐
                              ││
  User ──> Browser ──> Next.js││ API ──> Authentication ──> Authorization
  (frontend renders only;     ││  │       (session cookie     (membership +
   holds no secret, makes     ││  │        -> DB lookup)       permission)
   no authorization decision) ││  │                                │
                              ││  ▼                                ▼
                              ││ Rate limit / body limit /   AgentContext
                              ││ CSRF origin / headers       (org, role,
                              ││                              account scope)
                              ││                                   │
                              ││                                   ▼
                              ││  Orchestrator ──> Tool registry ──> Scoped
                              ││       │           (no execution     repos
                              ││       │            tool exists)     (SQL-level
                              ││       │                              tenancy)
                              ││       ▼
                              ││  LLM provider ── OpenAI  ← external boundary
                              ││  (output is UNTRUSTED INPUT)
                              ││
                              ││  Confirmation gate ──> execute_action
                              ││  (separate endpoint,    (the only writer)
                              ││   human decision)
└─────────────────────────────┘└───────────────────────────────────────────┘
```

**Boundary 1 — Browser → API.** Everything from the browser is hostile. The
frontend performs no authorization; it renders what the API returns. A body
field named `user_id`, `role` or `org_id` changes nothing.

**Boundary 2 — API → AgentContext.** The single trust transition. Below it,
`allowed_account_ids` can be believed. `api/authentication.py` is the only code
permitted to make that transition.

**Boundary 3 — Orchestrator → LLM provider.** Model output is untrusted input.
Tool names are looked up in a fixed registry; tool arguments are schema-checked
and screened for authorization-shaped names; scope is injected, never accepted.

**Boundary 4 — Retrieved content → the model.** Documents, tickets, customer
records and historical resolutions are **data**. They are never promoted to
instructions.

**Boundary 5 — Proposal → execution.** The confirmation gate. Crossed only by a
separate, authenticated, permission-checked HTTP request.

### External services

| Service | Trust | What crosses | Control |
| --- | --- | --- | --- |
| OpenAI API | Untrusted output; trusted with input | Message, tool schemas, tool results | Only reachable when `LLM_PROVIDER=real`; key server-side only; bounded steps, timeout, result-size cap. Output validated on return. |
| Hosting platform | Trusted with data at rest | Everything | Secrets via environment; nothing baked into images |
| npm / PyPI | Supply chain | Build-time code | Exact pins; `pip-audit` and `npm audit` clean |

There is **no vector database**. Retrieval is deterministic BM25 in Python over
chunks the caller is permitted to see (see §6).

---

## 2. Authentication

`AUTH_MODE=session` (the default) is real authentication.

| Property | Implementation |
| --- | --- |
| Password hashing | `hashlib.scrypt`, RFC 7914 — n=2¹⁵, r=8, p=1, 16-byte random salt, 32-byte key |
| Hash format | `scrypt$n$r$p$salt$hash` — self-describing, so parameters can be raised |
| Rehash on login | `needs_rehash` upgrades weaker hashes on next successful sign-in |
| Verification | `hmac.compare_digest` — constant time |
| Minimum length | 12 characters. No composition rules (they push users to predictable substitutions) |
| Session tokens | 256 bits from `secrets`; **only the SHA-256 digest is stored** |
| Cookie | `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/` |
| Session lifetime | 60-minute idle **and** 12-hour absolute, independently enforced |
| Session fixation | A new session id is minted on every login; a pre-set value is never elevated |
| Revocation | Immediate, by database write. Password change and reset revoke all sessions |
| MFA | TOTP, RFC 6238 (HMAC-SHA1, 30s step, ±1 window). Constant-time compare; spent time-step recorded so a code cannot be replayed |
| Email verification | Single-use token, 24h TTL, digest stored |
| Password reset | Single-use token, 30-minute TTL, digest stored; issuing a new one invalidates the old; completion revokes every session |
| Brute force | 5 failures per account **and** 20 per client address, over a 15-minute rolling window |
| Enumeration | Registration, login and reset-request are response- and timing-identical for existing and non-existing accounts |

**No custom cryptography.** scrypt, HMAC-SHA256, SHA-256 and RFC 6238 TOTP are
standard primitives from the Python standard library. TOTP is written out
because it is thirty lines of `hmac` and the alternative is a dependency; it
follows the published RFC exactly.

**No signing key.** Sessions are opaque random tokens with server-side state,
not signed stateless tokens. There is no `AUTH_SECRET_KEY` to leak, rotate or
commit, and revocation is a write rather than a blocklist.

### Why timing matters here

`passwords.waste_time()` performs a full scrypt derivation against a dummy hash
when no such user exists. Without it, "unknown account" returns in
microseconds and "wrong password" in tens of milliseconds — an enumeration
oracle that no amount of identical response text can close.

---

## 3. Authorization and tenant isolation

```
users ──< memberships >── organizations ──< organization_accounts
              │                                      │
             role                     the dataset account ids owned
```

| Role | Read | Run agent | Propose | Execute | Rules | Members | Delete org |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Owner | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Admin | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | — |
| Operations | ✔ | ✔ | ✔ | ✔ | — | — | — |
| Support | ✔ | ✔ | ✔ | **—** | — | — | — |
| Viewer | ✔ | ✔ | — | — | — | — | — |

**Propose and execute are separate permissions.** A Support member drafts an
escalation; someone with operational authority confirms it. Collapsing these
would hand every support user the very right the confirmation gate exists to
withhold.

### How tenancy is enforced

Every sensitive operation independently verifies, server-side:

1. **Authenticated user** — a live session resolved from the cookie digest.
2. **Organisation membership** — `get_membership(org_id, user_id)`, where
   `org_id` comes from the *session row*, never the request.
3. **Permission** — read from the role matrix, not from the request.
4. **Resource tenancy** — `allowed_account_ids` derived from
   `organization_accounts` and applied in SQL.
5. **Action-specific checks** — state, expiry, session binding, fingerprint.

The tenant boundary reuses the enforcement path that was already there and
already tested: `AgentContext.allowed_account_ids` is now *derived from
membership* instead of supplied by a mock directory. Nothing below the tool
layer changed.

Scoping is **compiled into SQL** (`services/documents.py::visibility_sql`), so
another tenant's rows are never loaded into the process at all — there is no
filtered-out object in memory for a later bug to leak.

### Refusal shape

An out-of-scope record is reported as **absent**, not forbidden. A 403 would
confirm that the record exists, turning the API into an existence oracle for
other tenants' data. This applies to records, documents, actions and
organisation ids alike.

---

## 4. Deterministic engine and the confirmation gate

The gate is a **persisted state machine**, not a prompt instruction and not
frontend state.

```
proposed ──> pending_confirmation ──> [separate authenticated request] ──> executed
```

| Attack | Control |
| --- | --- |
| Execute via chat | `POST /api/chat` has no code path to execution. No registry contains an execution tool |
| Model calls execution | `confirm_action` is not a tool and appears in no `registry.schemas()` |
| Confirm without permission | `EXECUTE_ACTION` checked before the state machine is consulted |
| Replay a confirmation | `UPDATE ... WHERE status = 'pending_confirmation'` — a replay changes 0 rows |
| Duplicate execution | Same guard; single-use even under concurrent requests |
| Alter parameters after review | `expected_fingerprint` — stored parameters must still digest to what was shown |
| Confirm from another conversation | Action bound to its conversation; conversation bound to its owner |
| Hijack a conversation id | `claim_conversation` — a conversation id is a claim about a row this user owns |
| Stale proposal | 30-minute TTL, re-checked at execution |
| Target moved or vanished | Target re-validated under the *confirming* caller's scope |
| Cross-tenant confirmation | Scope re-derived from the confirming session, not inherited |

Authorization is re-checked **at execution time**, under the confirming caller
— never inherited from whoever prepared the action.

---

## 5. AI security

The central rule:

> **Retrieved documents, customer records, tickets, contracts and uploaded
> files are DATA. They never become instructions.**

| Threat | Control | Structural? |
| --- | --- | --- |
| Direct prompt injection | Scope is a SQL predicate, not a prompt rule | ✔ |
| Indirect injection (documents) | Same; a malicious clause is returned as cited evidence and changes nothing | ✔ |
| Tool parameter manipulation | `RESERVED_ARGUMENT_NAMES` — `allowed_account_ids`, `user_id`, `role`, `context`, `conn` are *rejected*, not ignored, so the attempt is visible in the audit trail | ✔ |
| Unauthorized tool invocation | Fixed registry; unknown names refused | ✔ |
| Excessive agent permissions | The model's reachable surface is enumerable and contains no execution path | ✔ |
| Hallucinated authorization | Permissions read from the membership row; the model is never consulted | ✔ |
| Hallucinated policy | Every figure comes back from a deterministic tool with its arithmetic | ✔ |
| Agent loops | `AGENT_MAX_TOOL_STEPS` (default 8); truncation is *reported*, not hidden | ✔ |
| Token/resource exhaustion | Step budget, request timeout, per-result 12 KB cap, agent rate limit | ✔ |
| System-prompt extraction | The prompt contains no secret and no rule — it describes boundaries enforced elsewhere. Extracting it yields nothing | ✔ |
| Data exfiltration via the model | The model can only emit what a scoped tool returned | ✔ |
| Retrieval poisoning | Deprecated/non-authoritative sources cannot govern; precedence is computed from document metadata, not similarity | ✔ |
| Confirmation bypass | See §4 | ✔ |

"Structural" means the control does not depend on the model behaving. The
system prompt does describe these boundaries — a model that understands them
wastes fewer steps arguing with them — but **no boundary depends on being
described**.

---

## 6. Retrieval security

There is no vector database. Retrieval is BM25 computed in Python over
`fetch_searchable_evidence`, which loads **only chunks the caller may see** —
scoping is in the `WHERE` clause.

A consequence worth stating: because candidates are pre-filtered, another
tenant's documents cannot even influence corpus-wide scoring statistics (IDF,
average length). Cross-tenant retrieval is not merely filtered out; it is
never in the ranking.

Should a vector database be adopted, the equivalent requirements are: per-tenant
namespaces, metadata filters applied server-side, deletion propagated on
document removal, and re-authorization of every retrieved id against the
caller's scope before it reaches the model.

---

## 7. File and document security

ParcelPilot has **no upload endpoint**. Ingestion is two offline scripts over a
fixed, checksummed source pack. `app/backend/ingestion/safety.py` implements
the validation anyway, so the day an upload route exists it is a call to
`validate_upload` rather than a pipeline written under time pressure.

```
bytes ──> size ──> magic-byte type ──> extension cross-check ──> content-type
      ──> filename rebuild ──> path containment ──> parse ──> expansion check
```

| Threat | Control |
| --- | --- |
| MIME / extension spoofing | Type decided from magic bytes; a disagreeing name or `Content-Type` is itself a rejection |
| Polyglot files | Same; a PDF header on a `.csv` is refused |
| Executables (`MZ`, ELF, Mach-O, shebang, PHP, HTML) | Refused on head bytes |
| Zip / decompression bombs | Bare archives refused; expansion ratio capped at 200×; absolute extracted-character ceiling |
| Oversized files | 25 MB pre-parse limit |
| Path traversal | `sanitize_filename` **constructs** a safe name from safe characters — it does not blocklist. Plus resolved-path containment, which catches symlinks that no string cleaning can |
| Dangerous filenames | Windows reserved device names renamed; control characters stripped; length capped |
| Malformed / hostile PDFs | Type, size, encryption and page count all checked **before** `pymupdf` is called |
| Password-protected PDFs | Refused explicitly |
| CSV / formula injection | `neutralize_formula` prefixes `= + - @ TAB CR` on **export**, so a cell cannot execute when opened in a spreadsheet |
| Prompt injection in documents | Content is evidence, never instruction (§5) |

Parser limits are applied *before* the parser runs. "Hand it to pymupdf and
catch the exception" is not a control — by the time an exception exists, the
native parser has already read the input.

---

## 8. API security

Every endpoint, with what it requires:

| Endpoint | Auth | Permission | Tenant scope | Rate bucket |
| --- | --- | --- | --- | --- |
| `GET /health` | none | — | — | exempt |
| `POST /api/auth/register` | none | — | — | auth (10/min) |
| `POST /api/auth/verify-email` | none | — | — | auth |
| `POST /api/auth/login` | none | — | — | auth |
| `POST /api/auth/mfa/challenge` | half-session | — | — | auth |
| `POST /api/auth/logout` | none (idempotent) | — | own session | auth |
| `POST /api/auth/logout-all` | session | — | own sessions | auth |
| `GET /api/auth/me` | session | — | own | auth |
| `POST /api/auth/select-organization` | session | membership | own memberships | auth |
| `POST /api/auth/mfa/enrol` `/confirm` `/disable` | session | — | own | auth |
| `POST /api/auth/password/reset-request` `/reset` | none | — | — | auth |
| `POST /api/auth/password/change` | session | — | own | auth |
| `GET /api/auth/sessions` | session | — | own | auth |
| `GET /api/auth/organization/members` | session | `manage_members` | session org | auth |
| `POST /api/auth/organization/members/role` | session | `manage_members` (+owner for owner) | session org | auth |
| `GET /api/auth/audit` | session | `read_audit_log` | session org | auth |
| `POST /api/chat` | session | `run_agent` | session org | agent (15/min) |
| `POST /api/actions/{id}/confirm` | session | `execute_action` | session org | default |
| `GET /api/actions/pending` | session | — | session org | default |
| `GET /api/actions/{id}` | session | — | session org | default |
| `GET /api/principals` | none | — | — | default (empty under session auth) |

Request validation: every model is `extra="forbid"`, so an unexpected field is
a 422 rather than a silently ignored mass-assignment attempt. Nothing is
trusted from query parameters, path parameters, bodies, headers,
client-supplied organisation ids, client-supplied role fields, or LLM-generated
tool arguments.

### Browser security headers

**API** (JSON only, so the strictest policy that can be correct):
`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`,
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, `Permissions-Policy` (geolocation, camera,
microphone, payment, usb all denied), `Cache-Control: no-store`,
plus HSTS when `HSTS_ENABLED=true`.

**Frontend** (`next.config.ts`): `script-src 'self'` with no `unsafe-inline`
and no `unsafe-eval` in production; `connect-src` pinned to the configured API
origin so exfiltration fails at the browser; `frame-ancestors 'none'`;
`object-src 'none'`; COOP/CORP `same-origin`; `poweredByHeader: false`.

`style-src` retains `'unsafe-inline'` because Next injects component styles as
inline `<style>` in every build mode. Inline *style* cannot execute; the
alternative is a per-request nonce, which a statically exported app cannot
produce.

### CSRF

Two layers: `SameSite=Lax` (blocks the cookie on cross-site POST) and an
`Origin` check against the CORS allow-list for every unsafe method. A request
with no `Origin` is allowed — browsers always send it on cross-origin state
changes, so its absence means a non-browser client, for which CSRF is not a
meaningful threat.

`CORS_ALLOW_ORIGINS=*` is **refused at startup**, because it cannot be combined
with cookie authentication.

---

## 9. Abuse prevention

| Control | Default |
| --- | --- |
| Body size | 256 KB, checked before parsing (and while streaming when no length is declared) |
| Default rate | 120/min per client |
| Agent rate | 15/min — each request fans out into model calls |
| Auth rate | 10/min — these are the endpoints attackers guess against |
| Login lockout | 5/account and 20/client per 15 min |
| Agent steps | 8 per request |
| Provider timeout | 60s |
| Tool result size | 12 KB fed back to the model |
| Health | Never rate-limited — a limiter that hides an unhealthy deployment is worse than none |

Rate-limit keys prefer the authenticated user over the address: addresses are
shared by whole offices and rotated freely by attackers.
`X-Forwarded-For` is honoured **only** when `app.state.trust_forwarded_for` is
set, which is off by default — a client that can choose its own rate-limit key
is not rate-limited.

---

## 10. Logging and audit

`audit_log` is append-only and hash-chained:

```
entry_hash = SHA256(prev_hash ‖ canonical-json(entry))
```

`verify_audit_chain()` recomputes the chain and reports the **exact sequence
number** where an alteration, deletion or reordering begins.

**Recorded:** login success/failure/lockout, logout, registration, email
verification, password reset requested/completed, password change, MFA
enrolment/enable/disable/challenge-failure, session revocation, organisation
creation, membership and role changes, authorization denials, tenant-isolation
denials, agent invocations, action proposed/executed/rejected/refused, rate
limiting, payload rejection.

**Never recorded:** passwords, session tokens, API keys, reset tokens, MFA
codes. `_redact` drops any key whose name contains a credential marker and
truncates long values *before* the row is written — a belt-and-braces control
over callers being careful, because the careless caller is the one that
matters. Chat message content is deliberately not logged: an audit trail is
metadata about access, not a transcript of everything anyone typed.

IP addresses and emails are stored as truncated SHA-256 handles, so the log can
answer "was this the same client" without accumulating a record of who signed
in from where.

Audit writes **never raise**. A logging failure must not turn a successful
login into a 500 or, far worse, abort the transaction a security check runs in.

---

## 11. Secrets management

- No secret is hard-coded. `git grep` for key/password/token patterns across
  all tracked files returns nothing.
- `.gitignore` covers `.env`, `.env.*`, `*.key`, `*.pem`, `credentials.json`,
  `service-account*.json`, `*_secret*`, `*apikey*`.
- The `OPENAI_API_KEY` placeholder in `.env.example` is explicitly treated as
  absent by `load_settings`, so a placeholder can never read as a credential.
- `Settings.openai_api_key` is `repr=False` and never serialised.
  `/health` reports the provider *mode*, never its configuration.
- Only `NEXT_PUBLIC_*` variables reach the browser bundle, and the only one is
  the API base URL.
- The Docker image bakes in no secret, runs as a non-root user (uid 1000), and
  copies nothing from `data/processed/`.
- **There is no session signing key** (§2), so the largest category of
  authentication secret does not exist here.

---

## 12. Dependencies

| Ecosystem | Tool | Result |
| --- | --- | --- |
| Python | `pip-audit` | **0 known vulnerabilities** |
| Node | `npm audit` (prod and dev) | **0 vulnerabilities** |

One upgrade was made for security and is justified in `requirements.txt`:
`fastapi 0.121.2 → 0.141.1` and `starlette 0.49.3 → 1.6.0`. The old FastAPI
pinned `starlette <0.50`, and every Starlette in that range carries
PYSEC-2026-161 — see the finding in §14, which was **exploitable in this
codebase**. Starlette is pinned explicitly so a fresh install cannot resolve
back to a vulnerable version.

---

## 13. Testing

| Suite | Tests |
| --- | --- |
| Pre-existing (unchanged in intent) | 572 |
| `test_security_auth.py` | 39 |
| `test_security_adversarial.py` | 55 |
| `test_security_files.py` | 44 |
| **Backend total** | **710** |
| Frontend (`vitest`) | 105 |

Security tests run against the **default** configuration (`AuthMode.SESSION`),
not a loosened one. The pre-existing suite declares `AuthMode.DEMO_HEADER`
explicitly in its fixture, which is what keeps the default genuinely secure
rather than being weakened to suit the tests.

---

## 14. Known limitations

Stated plainly, because a security document that lists only strengths is
marketing.

**Not eliminated, and not eliminable at this scope:**

1. **The audit log is tamper-evident, not tamper-proof.** Anyone with write
   access to the SQLite file can recompute the whole chain. Tamper-proof
   requires an off-box witness — an append-only log service, or periodic
   publication of the head hash.

2. **Rate limiting is per-process.** Two workers each allow the configured
   rate. Correct for this single-container deployment; a multi-replica
   deployment needs a shared counter (Redis).

3. **Fixed-window rate limiting admits a boundary burst** of up to 2× the limit
   across two adjacent windows. Sliding windows avoid this at the cost of
   storing every timestamp.

4. **No email transport.** Verification and reset links are returned in the API
   response when `APP_ENV` is not production, and withheld when it is. Until a
   mail sender exists, production email verification and password reset are not
   operable end to end.

5. **SQLite has no row-level security.** Tenancy is enforced in the application's
   query layer — consistently, and in SQL rather than in Python, but there is no
   database-level backstop. PostgreSQL RLS would add one. Relatedly, database
   access is not least-privilege: SQLite has no roles.

6. **No encryption at rest** beyond what the host filesystem provides, and no
   field-level encryption. Passwords and tokens are hashed, so the highest-value
   secrets are protected regardless.

7. **No backup, retention or deletion policy** is implemented. Right-to-erasure
   would need a documented cascade across `users`, `memberships`, `sessions`,
   `conversations`, `agent_actions` and `audit_log` — with the audit trail's
   retention requirement deliberately in tension with erasure.

8. **TOTP has no recovery codes.** A user who loses their authenticator needs
   operator intervention. Recovery codes are the standard remedy and are not
   implemented.

9. **No account-takeover detection** — no notification on password change, new
   device, or sign-in from an unfamiliar location.

10. **The LLM provider sees message content and tool results.** That is inherent
    to using a hosted model. It is bounded by tenant scoping — the provider only
    ever sees what the caller was authorized to see — but it is a real data-flow
    to a third party.

11. **Prompt injection is contained, not prevented.** Injected text cannot widen
    scope, invoke a tool, or execute an action. It *can* influence the model's
    prose. The defence is that no consequential decision is made in prose:
    figures come from deterministic tools and actions require human
    confirmation.

12. **`AUTH_MODE=demo_header` still exists.** It is refused in production and
    is not the default, but a non-production deployment reachable from the
    internet with it enabled has no authentication at all.

13. **Dependency audits are point-in-time.** They were clean when this was
    written. Automated scanning in CI is not configured.

14. **No penetration test.** The adversarial suite encodes the attacks
    considered here; it is not a substitute for an adversary who thinks of
    something else.

---

## 15. Responsible disclosure

ParcelPilot is a personal project and operates no bug-bounty programme.

If you find a vulnerability, please report it **privately** — open a GitHub
security advisory on the repository, or contact the maintainer directly. Please
do not open a public issue for an unpatched vulnerability.

Helpful reports include: what the issue is, how to reproduce it, what an
attacker gains, and any suggested remediation. Please do not access, modify or
retain data belonging to anyone else while investigating, and do not run
denial-of-service or automated scanning against a deployed instance.

Expect an acknowledgement within a few days. Fixes are prioritised by impact,
and credit is offered unless you would rather not have it.

---

## 16. Security checklist

| Area | Status |
| --- | --- |
| Authentication | ✅ scrypt, opaque sessions, TOTP MFA, verification, reset, lockout, enumeration resistance |
| Session management | ✅ HttpOnly/Secure/SameSite, dual expiry, revocation, fixation-resistant |
| Authorization | ✅ Five roles, explicit permission matrix, server-side only, propose/execute split |
| Tenant isolation | ✅ Session-derived scope, SQL-level enforcement, absence-not-forbidden refusals |
| Database security | ⚠️ Parameterized, FK-enforced, STRICT tables, tenancy in SQL — but no RLS, no least-privilege, no encryption at rest |
| Vector security | ➖ Not applicable — no vector DB; BM25 over pre-scoped candidates |
| File upload security | ✅ Validator implemented and wired into ingestion — but no upload endpoint exists yet |
| API security | ✅ Strict schemas, `extra="forbid"`, body limits, rate limits, CSRF origin check, headers |
| AI security | ✅ Structural, not prompt-based |
| Deterministic engine | ✅ Persisted state machine, permission-gated, replay-proof, fingerprinted |
| Action security | ✅ Single-use, re-validated, session-bound, conversation-owned, audited |
| Infrastructure | ⚠️ Non-root container, no baked secrets, fail-closed config — but single-node, no WAF, no shared rate limiter |
| Logging / audit | ✅ Hash-chained, redacting, denial-inclusive |
| Dependencies | ✅ `pip-audit` and `npm audit` both clean |

**Explicitly out of scope:** email deliverability and anti-phishing, DDoS
absorption, host and network hardening, physical security, formal compliance
(SOC 2, ISO 27001, GDPR processes), and third-party penetration testing.
