# Persistence: PostgreSQL, tenancy and documents

Astrion is a multi-workspace product. The **workspace** (an `organizations` row)
is the tenant boundary; everything a workspace owns carries its `org_id`, and
every read and write is made *for* a workspace.

## Engines

| | Production | Local development and tests |
| --- | --- | --- |
| Database | PostgreSQL (Supabase), `DATABASE_URL=postgresql://…` | SQLite file, `DATABASE_URL=sqlite:///…` |
| Driver | psycopg 3 with a connection pool | `sqlite3` |
| Schema | versioned migrations, applied by the deploy step | built from the current schema |

`app/backend/db` presents both behind one interface, so the repositories are
plain SQL against a connection (no ORM). The PostgreSQL adapter translates `?`
placeholders, turns `with conn:` into a transaction (a savepoint when nested),
reads rows like `sqlite3.Row`, and offers `serialized(key)` -- mutual exclusion
around a read-then-write, `BEGIN IMMEDIATE` on SQLite and a transaction-scoped
advisory lock on PostgreSQL. Transaction-scoped on purpose: it survives a
transaction-mode pooler (Supabase port 6543); session advisory locks and
prepared statements do not, so neither is used.

### Migrations

`app/backend/db/sql/postgres/NNNN_name.sql`, recorded in `schema_migrations`
with a checksum. Applied migrations are immutable (a changed file stops the run);
a database behind the code is reported as unavailable, never repaired from a
request handler.

```
python -m app.backend.db.migrate     # apply pending migrations
```

`docker-entrypoint.sh` runs this, then loads the system documents, before the API
starts when `DATABASE_URL` is PostgreSQL. The migration runner holds an advisory
lock, so instances starting together cannot race.

`tests/test_db_layer.py` keeps the two engines honest: it compares the SQLite
schema with the migrated PostgreSQL one (tables, columns, nullability, primary
keys) and runs the constraint, transaction and concurrency contracts.

### Running the tests on PostgreSQL

```
docker run -d --name astrion-pg-test -e POSTGRES_USER=astrion \
    -e POSTGRES_PASSWORD=astrion_test_pw -e POSTGRES_DB=astrion_test \
    -p 55432:5432 postgres:17
ASTRION_TEST_DATABASE_URL=postgresql://astrion:astrion_test_pw@localhost:55432/astrion_test pytest
```

Every test's database path maps to its own schema (see `tests/pg_backend.py`), so
the whole suite runs unchanged on either engine.

## Tenancy

A `Scope` (`app/backend/tenancy.py`) is `(org_id, account_ids)`: the workspace,
and optionally the accounts within it a caller may see. It comes from the
authenticated session's membership, never from a request, a tool argument or
model output, and is compiled into every scoped query's `WHERE` clause. A scope
with no workspace matches nothing -- forgetting one reads no data, not all of it.

| Table | Ownership |
| --- | --- |
| `accounts` | `(org_id, account_id)` -- an account id names a customer *within* a workspace, so two workspaces may both have `ACCT-001` |
| `orders`, `tickets` | `(org_id, order_id / ticket_id)`, foreign key to the workspace's own account |
| `source_provenance`, `dataset_metadata`, `ingestion_runs` | per workspace |
| `agent_actions`, `ticket_escalations`, `service_credits`, `ticket_notes` | `org_id`; an action is found only within its workspace |
| `documents` | `org_id`, or **NULL for a system document** |
| `document_chunks` | no `org_id`; reached through the owning document |
| `organization_accounts` | a view over `accounts`, kept for readers of that name |

### System documents

`documents.org_id IS NULL` marks platform knowledge -- the support policy, the
cancellation SOP, the operations guide -- that every workspace's assistant may
cite. Workspaces cannot delete them and no API can create one. A `CHECK`
constraint forbids a system document from naming an account: an account id is
only meaningful inside a workspace, so a system document naming `ACCT-001` would
read as belonging to every workspace's `ACCT-001`. The customer agreements in the
supplied pack are therefore *workspace* documents (of whichever workspace has
those customers), not system ones.

Load them with `python scripts/ingest_documents.py --system-only`.

### A workspace starts empty

Creating a workspace creates no accounts, orders, tickets or documents. It has
the system documents and whatever its members add. Policy questions are judged
against the current time unless the workspace imported a dataset with its own
snapshot time (`dataset_metadata`), in which case that snapshot is the reference
clock so an imported historical dataset does not drift.

The assessment workbook is a legacy import: `scripts/ingest_dataset.py --org-id
<workspace>` loads it into a named workspace (default `ORG-legacy-assessment`),
replacing only that workspace's rows.

## Documents and object storage

A document's **metadata and extracted text** are in PostgreSQL; its **original
file** is an object in a store (`app/backend/storage`), found through
`documents.storage_key`. The store is an interface -- `put`, `get`, `exists`,
`delete` (and `list` for reconciliation) -- with a local-directory
implementation for tests and an S3-compatible one (Supabase Storage in
production, via boto3, imported nowhere else). No binary is ever in the database.

Keys carry ownership and the checksum, so a file is immutable at its key:

```
workspaces/{org_id}/documents/{document_id}/{sha256}.pdf
system/documents/{document_id}/{sha256}.pdf
```

Every component is validated against a strict pattern and every store re-checks
every key, so nothing an upload can name reaches a path as a separator or `..`.

The two systems share no transaction, so their consistency comes from the order
of operations (`services/document_files.py`), each rule tested:

1. The object is written **before** the row, so a row never points at nothing.
2. A failed creation removes the object it just wrote (unless another row uses
   that key).
3. Nothing is removed before its replacement commits: a delete removes the row,
   then the object; a replacement removes the old object only after the new row
   commits.
4. A cleanup that fails is **recorded** in `storage_orphans` and the request still
   succeeds; `python scripts/reconcile_storage.py` retries them, and finds
   objects nothing references (past a one-hour grace period, so an upload in
   flight is left alone) and rows whose object has gone.
5. Bytes are checked against the recorded sha256 whenever they are read back
   (reindex skips a tampered original rather than trusting it).

Extraction happens from a private temporary file that exists only for the call;
nothing on the host's disk outlives the request.

### Documents that state no title or status

The controlled pack states its own title and `Status:`, and a document that claims
a type (a policy, an SOP, an agreement naming an account) must state its status,
because authority is read from the document and never guessed.

A workspace may also upload an ordinary PDF (a letter, notes, a scan). Such a
file is kept, indexed and retrievable, but as a **reference**: `document_type =
reference`, `status = UNSTATED`, tier 4 (non-authoritative), so it can inform an
answer and can never govern one. Its title comes from the page's own heading, then
the PDF's Title field (when it reads like a title, not "Untitled" or a file name),
then the original file name with its extension and upload prefix removed. A file
name never classifies a document (`*_SOP_*.pdf` is not an SOP). Anything that does
claim authority, a known type, an `Account:` or an unrecognised `Status:`, is still
refused with the reason. The platform's own loader stays strict.

### Upload size

`MAX_REQUEST_BYTES` (256 KB) is for ordinary API requests. The upload route
alone has its own ceiling, `MAX_UPLOAD_BYTES` (26 MB), refused from the declared
length before the body is read; the 25 MB document validation still applies
inside it. Every other route keeps the small limit.

### Production refuses ephemeral storage

`create_app()` with no arguments (the process entry point) calls
`Settings.validate_persistence()`: in production the database must be PostgreSQL
and the store must be `s3`. A deployment configured otherwise does not start.

## Environment

| Variable | Where | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Render (secret) | `postgresql://…` in production. Use the pooler host on Supabase: the direct host is IPv6-only. Never in Vercel, never in Git, never `NEXT_PUBLIC_*`. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | Render | Pool bounds; each in-flight request holds one connection |
| `DEMO_LOGIN_ENABLED` | Render | Must be `false` against PostgreSQL (refused otherwise) |
| `STORAGE_BACKEND` | Render | `s3` (or `supabase`) in production; `local` only in development |
| `STORAGE_BUCKET`, `STORAGE_ENDPOINT_URL`, `STORAGE_REGION` | Render | The S3-compatible endpoint; for Supabase, `https://<ref>.storage.supabase.co/storage/v1/s3` |
| `STORAGE_ACCESS_KEY_ID`, `STORAGE_SECRET_ACCESS_KEY` | Render (secrets) | Created in the Supabase dashboard. Never Vercel, Git or `NEXT_PUBLIC_*` |
| `STORAGE_KEY_PREFIX` | Render | Optional prefix inside the bucket |
| `MAX_UPLOAD_BYTES` | Render | Upload-route body ceiling (default 26 MB) |

The frontend needs only `NEXT_PUBLIC_API_BASE_URL`.
