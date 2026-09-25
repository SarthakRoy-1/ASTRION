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

## Environment

| Variable | Where | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Render (secret) | `postgresql://…` in production. Use the pooler host on Supabase: the direct host is IPv6-only. Never in Vercel, never in Git, never `NEXT_PUBLIC_*`. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | Render | Pool bounds; each in-flight request holds one connection |
| `DEMO_LOGIN_ENABLED` | Render | Must be `false` against PostgreSQL (refused otherwise) |
