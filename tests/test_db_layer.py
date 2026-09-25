"""The database layer: what both engines must do the same way.

Two halves. The first needs no server: placeholder translation, the row type,
the exception tuples. The second (`postgres_only`) needs a PostgreSQL to talk to
(`ASTRION_TEST_DATABASE_URL`, see tests/pg_backend.py) and holds the contract
the application relies on -- migrations that are versioned and idempotent,
transactions that commit and roll back, the constraints that make registration
and single-use tokens safe, and concurrency the SQLite write lock used to give
for free.

Each PostgreSQL test gets its own schema, dropped afterwards.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid

import pytest

from app.backend.db import IntegrityError, OperationalError, DatabaseError, serialized
from app.backend.db.postgres import PgConnection, PostgresDatabase, translate
from app.backend.db.rows import Row

pg = pytest.mark.postgres_only


# --- no server needed -------------------------------------------------------


class TestTranslate:
    def test_question_marks_become_psycopg_placeholders(self):
        assert translate("SELECT * FROM t WHERE a = ? AND b = ?") == (
            "SELECT * FROM t WHERE a = %s AND b = %s"
        )

    def test_a_question_mark_inside_a_string_literal_is_data(self):
        assert translate("SELECT 'what?' , ?") == "SELECT 'what?' , %s"

    def test_an_escaped_quote_does_not_end_the_literal(self):
        assert translate("SELECT 'it''s ?', ?") == "SELECT 'it''s ?', %s"

    def test_a_literal_percent_is_escaped_everywhere(self):
        assert translate("SELECT '100%', 5 % 2, ?") == "SELECT '100%%', 5 %% 2, %s"

    def test_a_statement_without_parameters_is_unchanged_but_for_percent(self):
        assert translate("SELECT 1") == "SELECT 1"


class TestRow:
    def make(self) -> Row:
        return Row(["id", "name"], [7, "ada"])

    def test_reads_by_name_and_position(self):
        row = self.make()
        assert row["name"] == "ada" and row[0] == 7 and row[-1] == "ada"

    def test_is_a_mapping_for_dict_and_keys(self):
        row = self.make()
        assert dict(row) == {"id": 7, "name": "ada"}
        assert row.keys() == ["id", "name"]

    def test_iterates_values_like_sqlite_row(self):
        assert list(self.make()) == [7, "ada"]
        assert len(self.make()) == 2

    def test_an_unknown_column_raises_indexerror_like_sqlite(self):
        with pytest.raises(IndexError):
            self.make()["missing"]

    def test_matches_what_sqlite_returns(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        sq = conn.execute("SELECT 7 AS id, 'ada' AS name").fetchone()
        mine = self.make()
        assert dict(sq) == dict(mine)
        assert list(sq) == list(mine)


class TestErrorTuples:
    def test_sqlite_integrity_errors_are_caught(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(IntegrityError):
            conn.execute("INSERT INTO t VALUES (1)")

    def test_a_missing_table_is_an_operational_error(self):
        conn = sqlite3.connect(":memory:")
        with pytest.raises(OperationalError):
            conn.execute("SELECT * FROM nope")

    def test_the_general_tuple_covers_both(self):
        assert sqlite3.Error in DatabaseError


def test_serialized_on_a_plain_sqlite_connection_commits_and_rolls_back():
    conn = sqlite3.connect(":memory:", isolation_level="")
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.commit()
    with serialized(conn, "k"):
        conn.execute("INSERT INTO t VALUES (1)")
    assert not conn.in_transaction
    with pytest.raises(RuntimeError):
        with serialized(conn, "k"):
            conn.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT n FROM t").fetchall() == [(1,)]


# --- PostgreSQL contract ----------------------------------------------------


@pytest.fixture
def pgdb():
    """A PostgreSQL database with its own empty schema, dropped afterwards."""
    import psycopg

    url = os.environ["ASTRION_TEST_DATABASE_URL"]
    schema = f"ct_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    database = PostgresDatabase(url, min_size=1, max_size=8, schema=schema)
    yield database
    database.close()
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def migrated(pgdb):
    pgdb.migrate()
    return pgdb


@pg
class TestMigrations:
    def test_creates_every_table_and_records_the_version(self, pgdb):
        applied = pgdb.migrate()
        assert applied == ["0001", "0002", "0003"]
        conn = pgdb.connect()
        try:
            tables = {
                r["table_name"]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                ).fetchall()
            }
            versions = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations")]
        finally:
            conn.close()
        assert {"users", "sessions", "audit_log", "documents", "agent_actions"} <= tables
        assert "schema_migrations" in tables
        assert len(tables) == 30  # the 29 application tables, and the ledger
        assert versions == ["0001", "0002", "0003"]

    def test_running_again_applies_nothing(self, pgdb):
        pgdb.migrate()
        assert pgdb.migrate() == []

    def test_an_edited_migration_stops_the_run(self, migrated):
        conn = migrated.connect()
        try:
            conn.execute("UPDATE schema_migrations SET checksum = 'tampered'")
            from app.backend.db.migrations import MigrationError, apply_migrations

            with pytest.raises(MigrationError, match="immutable"):
                apply_migrations(conn, "postgres")
        finally:
            conn.close()

    def test_a_database_ahead_of_the_code_stops_the_run(self, migrated):
        conn = migrated.connect()
        try:
            conn.execute(
                "INSERT INTO schema_migrations VALUES ('9999', 'future', 'x', 'now')"
            )
            from app.backend.db.migrations import MigrationError, apply_migrations

            with pytest.raises(MigrationError, match="does not contain"):
                apply_migrations(conn, "postgres")
        finally:
            conn.close()

    def test_an_unmigrated_database_is_reported_not_repaired(self, pgdb):
        from app.backend.db import SchemaNotReadyError

        with pytest.raises(SchemaNotReadyError, match="has not been migrated"):
            pgdb.ensure_ready()
        # And nothing was created by asking.
        conn = pgdb.connect()
        try:
            assert conn.execute(
                "SELECT count(*) AS n FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchone()["n"] == 0
        finally:
            conn.close()

    def test_two_runners_at_once_apply_it_once(self, pgdb):
        results: list[list[str]] = []
        errors: list[BaseException] = []

        def run():
            try:
                results.append(pgdb.migrate())
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert sorted(len(r) for r in results) == [0, 0, 0, 3]  # every migration, once

    def test_request_handling_never_runs_ddl(self, migrated):
        # ensure_ready on a migrated database only reads.
        migrated.ensure_ready()
        conn = migrated.connect()
        try:
            before = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"]
        finally:
            conn.close()
        migrated.ensure_ready()
        assert before == 3


@pg
class TestTransactions:
    def _table(self, db):
        conn = db.connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS scratch (id INTEGER PRIMARY KEY, v TEXT)")
        finally:
            conn.close()

    def _count(self, db) -> int:
        conn = db.connect()
        try:
            return conn.execute("SELECT count(*) AS n FROM scratch").fetchone()["n"]
        finally:
            conn.close()

    def test_a_with_block_commits_on_success(self, pgdb):
        self._table(pgdb)
        conn = pgdb.connect()
        with conn:
            conn.execute("INSERT INTO scratch VALUES (?, ?)", (1, "a"))
        conn.close()
        assert self._count(pgdb) == 1

    def test_a_with_block_rolls_back_on_an_exception(self, pgdb):
        self._table(pgdb)
        conn = pgdb.connect()
        with pytest.raises(RuntimeError):
            with conn:
                conn.execute("INSERT INTO scratch VALUES (?, ?)", (1, "a"))
                raise RuntimeError("no")
        conn.close()
        assert self._count(pgdb) == 0

    def test_work_is_invisible_to_others_until_commit(self, pgdb):
        self._table(pgdb)
        writer = pgdb.connect()
        with writer:
            writer.execute("INSERT INTO scratch VALUES (?, ?)", (1, "a"))
            assert self._count(pgdb) == 0
        assert self._count(pgdb) == 1
        writer.close()

    def test_an_inner_failure_the_caller_catches_does_not_poison_the_outer_transaction(
        self, pgdb
    ):
        self._table(pgdb)
        conn = pgdb.connect()
        with conn:
            conn.execute("INSERT INTO scratch VALUES (?, ?)", (1, "a"))
            try:
                with conn:
                    conn.execute("INSERT INTO scratch VALUES (?, ?)", (1, "dup"))
            except IntegrityError:
                pass
            conn.execute("INSERT INTO scratch VALUES (?, ?)", (2, "b"))
        conn.close()
        assert self._count(pgdb) == 2

    def test_a_statement_outside_a_with_block_is_committed_immediately(self, pgdb):
        self._table(pgdb)
        conn = pgdb.connect()
        conn.execute("INSERT INTO scratch VALUES (?, ?)", (1, "a"))
        assert not conn.in_transaction
        assert self._count(pgdb) == 1
        conn.close()

    def test_in_transaction_reflects_the_block(self, pgdb):
        conn = pgdb.connect()
        assert not conn.in_transaction
        with conn:
            assert conn.in_transaction
        assert not conn.in_transaction
        conn.close()


@pg
class TestStatements:
    def test_duplicate_keys_raise_the_shared_integrity_error(self, migrated):
        conn = migrated.connect()
        try:
            conn.execute(
                "INSERT INTO organizations (org_id, name, slug, created_at_utc) VALUES (?,?,?,?)",
                ("ORG-1", "A", "a", "t"),
            )
            with pytest.raises(IntegrityError):
                conn.execute(
                    "INSERT INTO organizations (org_id, name, slug, created_at_utc) VALUES (?,?,?,?)",
                    ("ORG-2", "B", "a", "t"),  # slug is UNIQUE
                )
        finally:
            conn.close()

    def test_returning_gives_the_generated_id_and_lastrowid_is_refused(self, migrated):
        conn = migrated.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO ingestion_runs (started_at_utc, source_workbook_path, "
                "source_workbook_sha256, ingestion_script_version) VALUES (?,?,?,?) RETURNING id",
                ("t", "p", "h", "v"),
            )
            assert isinstance(cursor.fetchall()[0]["id"], int)
            with pytest.raises(NotImplementedError):
                cursor.lastrowid
        finally:
            conn.close()

    def test_a_python_bool_is_stored_as_the_integer_sqlite_would(self, migrated):
        conn = migrated.connect()
        try:
            conn.execute(
                "INSERT INTO users (user_id, email, email_verified, display_name, password_hash, "
                "password_updated_at_utc, created_at_utc) VALUES (?,?,?,?,?,?,?)",
                ("USR-1", "a@b.c", True, "A", "h", "t", "t"),
            )
            assert conn.execute("SELECT email_verified FROM users").fetchone()[0] == 1
        finally:
            conn.close()

    def test_rows_read_by_name_and_position(self, migrated):
        conn = migrated.connect()
        try:
            row = conn.execute("SELECT 5 AS a, 'x' AS b").fetchone()
            assert row["a"] == 5 and row[1] == "x" and dict(row) == {"a": 5, "b": "x"}
        finally:
            conn.close()

    def test_a_statement_with_no_result_set_fetches_nothing(self, migrated):
        conn = migrated.connect()
        try:
            cursor = conn.execute("UPDATE users SET display_name = ? WHERE 1 = 0", ("x",))
            assert cursor.rowcount == 0
            assert cursor.fetchone() is None and cursor.fetchall() == []
        finally:
            conn.close()

    def test_text_sorts_bytewise_as_it_did_on_sqlite(self, migrated):
        conn = migrated.connect()
        try:
            for i, slug in enumerate(["b", "B", "a", "A", "_", "-"]):
                conn.execute(
                    "INSERT INTO organizations (org_id, name, slug, created_at_utc) VALUES (?,?,?,?)",
                    (f"ORG-{i}", slug, slug, "t"),
                )
            order = [r["slug"] for r in conn.execute("SELECT slug FROM organizations ORDER BY slug")]
        finally:
            conn.close()
        assert order == sorted(order)  # Python's order is bytewise for ASCII


@pg
class TestConcurrency:
    def test_serialized_makes_read_then_write_exact(self, migrated):
        conn = migrated.connect()
        conn.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, n INTEGER)")
        conn.execute("INSERT INTO counter VALUES (1, 0)")
        conn.close()

        def bump():
            c = migrated.connect()
            try:
                for _ in range(10):
                    with serialized(c, "counter"):
                        n = c.execute("SELECT n FROM counter WHERE id = 1").fetchone()["n"]
                        c.execute("UPDATE counter SET n = ? WHERE id = 1", (n + 1,))
            finally:
                c.close()

        threads = [threading.Thread(target=bump) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        c = migrated.connect()
        try:
            assert c.execute("SELECT n FROM counter WHERE id = 1").fetchone()["n"] == 80
        finally:
            c.close()

    def test_the_audit_chain_does_not_fork_under_concurrent_writers(self, migrated):
        from app.backend.services.audit import AuditEvent, record_event, verify_audit_chain

        def write():
            c = migrated.connect()
            try:
                for _ in range(6):
                    assert record_event(c, AuditEvent.LOGIN_SUCCEEDED, details={"n": 1})
            finally:
                c.close()

        threads = [threading.Thread(target=write) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        c = migrated.connect()
        try:
            assert c.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"] == 48
            assert verify_audit_chain(c) == (True, None)
        finally:
            c.close()

    def test_an_audit_failure_inside_a_caller_transaction_does_not_abort_it(self, migrated):
        from app.backend.services.audit import AuditEvent, record_event

        c = migrated.connect()
        try:
            with c:
                c.execute(
                    "INSERT INTO organizations (org_id, name, slug, created_at_utc) "
                    "VALUES ('ORG-A', 'A', 'a', 't')"
                )
                # A detail the JSON encoder cannot serialise is swallowed...
                record_event(c, AuditEvent.LOGIN_SUCCEEDED, details={"x": object()})
                # ...and the caller's own work is still there to commit.
                c.execute("SELECT 1")
            assert c.execute("SELECT count(*) AS n FROM organizations").fetchone()["n"] == 1
        finally:
            c.close()

    def test_an_audit_write_does_not_commit_a_callers_open_transaction(self, migrated):
        from app.backend.services.audit import AuditEvent, record_event

        c = migrated.connect()
        try:
            with pytest.raises(RuntimeError):
                with c:
                    c.execute(
                        "INSERT INTO organizations (org_id, name, slug, created_at_utc) "
                        "VALUES ('ORG-R', 'R', 'r', 't')"
                    )
                    record_event(c, AuditEvent.LOGIN_SUCCEEDED, actor_user_id="USR-x")
                    raise RuntimeError("the caller decides to roll back")
            # Neither the caller's work nor the audit entry it made survives.
            assert c.execute("SELECT count(*) AS n FROM organizations").fetchone()["n"] == 0
            assert c.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"] == 0
        finally:
            c.close()

    def test_a_single_use_token_is_redeemed_exactly_once_under_a_race(self, migrated):
        from app.backend.auth import repository as repo

        c = migrated.connect()
        user = repo.create_user(
            c, email="race@example.com", display_name="R", password_hash="h", email_verified=True
        )
        token = repo.issue_auth_token(c, user_id=user.user_id, purpose="reset", ttl_minutes=10)
        c.close()

        winners: list[str] = []
        barrier = threading.Barrier(8)

        def redeem():
            conn = migrated.connect()
            try:
                barrier.wait()
                got = repo.consume_auth_token(conn, token=token, purpose="reset")
                if got:
                    winners.append(got)
            finally:
                conn.close()

        threads = [threading.Thread(target=redeem) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert winners == [user.user_id]

    def test_registration_of_one_address_is_unique_under_a_race(self, migrated):
        from app.backend.auth import repository as repo

        outcomes: list[str] = []
        barrier = threading.Barrier(8)

        def register():
            conn = migrated.connect()
            try:
                barrier.wait()
                try:
                    repo.create_user(
                        conn, email="dup@example.com", display_name="D", password_hash="h"
                    )
                    outcomes.append("created")
                except IntegrityError:
                    outcomes.append("refused")
            finally:
                conn.close()

        threads = [threading.Thread(target=register) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert outcomes.count("created") == 1 and outcomes.count("refused") == 7


@pg
class TestPersistence:
    def test_a_session_survives_a_restart_of_the_application(self, pgdb):
        """The point of moving off a local file: a new process still knows you."""
        from app.backend.auth import repository as repo

        pgdb.migrate()
        conn = pgdb.connect()
        user = repo.create_user(
            conn, email="keep@example.com", display_name="K", password_hash="h", email_verified=True
        )
        _sid, token = repo.create_session(
            conn, user_id=user.user_id, org_id=None, mfa_satisfied=True
        )
        conn.close()

        pgdb.close()  # the process ends; its pool is gone

        restarted = PostgresDatabase(
            pgdb._url, min_size=1, max_size=2, schema=pgdb._schema
        )
        try:
            restarted.ensure_ready()
            again = restarted.connect()
            try:
                session = repo.lookup_session(again, token)
            finally:
                again.close()
            assert session is not None and session.user_id == user.user_id
        finally:
            restarted.close()

    def test_membership_is_unique_per_user_and_workspace(self, migrated):
        from app.backend.auth import repository as repo
        from app.backend.auth.permissions import OrgRole

        c = migrated.connect()
        try:
            user = repo.create_user(c, email="m@example.com", display_name="M", password_hash="h")
            org_id, _ = repo.create_organization(c, name="W", slug="w")
            repo.add_member(c, org_id=org_id, user_id=user.user_id, role=OrgRole.VIEWER)
            with pytest.raises(IntegrityError):
                repo.add_member(c, org_id=org_id, user_id=user.user_id, role=OrgRole.OWNER)
        finally:
            c.close()


@pg
def test_the_postgres_schema_matches_the_sqlite_schema(migrated, tmp_path):
    """Two engines, one schema. Drift between them is a bug this catches."""
    from app.backend.services.database import get_connection, initialize_schema

    # A real SQLite file, not the test seam, so this compares the two engines.
    import app.backend.services.database as layer

    saved, layer._connection_factory = layer._connection_factory, None
    try:
        lite = get_connection(tmp_path / "parity.db")
        initialize_schema(lite)
        lite_tables = {
            r["name"]
            for r in lite.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
            if not r["name"].startswith("sqlite_")
        }
        lite_cols = {}
        lite_pks = {}
        for table in lite_tables:
            info = lite.execute(f"PRAGMA table_info({table})").fetchall()
            lite_cols[table] = [
                (r["name"], bool(r["notnull"]) or bool(r["pk"])) for r in info
            ]
            lite_pks[table] = {r["name"] for r in info if r["pk"]}
        lite.close()
    finally:
        layer._connection_factory = saved

    conn = migrated.connect()
    try:
        pg_tables = {
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        } - {"schema_migrations"}
        assert pg_tables == lite_tables
        for table in sorted(pg_tables):
            cols = [
                (r["column_name"], r["is_nullable"] == "NO")
                for r in conn.execute(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = ? "
                    "ORDER BY ordinal_position",
                    (table,),
                ).fetchall()
            ]
            assert sorted(cols) == sorted(lite_cols[table]), table
            pk = {
                r["column_name"]
                for r in conn.execute(
                    "SELECT k.column_name FROM information_schema.table_constraints c "
                    "JOIN information_schema.key_column_usage k "
                    "  ON k.constraint_name = c.constraint_name AND k.table_schema = c.table_schema "
                    "WHERE c.table_schema = current_schema() AND c.table_name = ? "
                    "  AND c.constraint_type = 'PRIMARY KEY'",
                    (table,),
                ).fetchall()
            }
            assert pk == lite_pks[table], f"{table}: primary keys differ"
    finally:
        conn.close()


@pg
class TestOwnershipMigration:
    """0002 must be right on a database that already holds rows, not only an empty one."""

    def _legacy(self, db):
        """A database at 0001 holding what the global-account model stored."""
        db.migrate(up_to="0001")
        c = db.connect()
        try:
            for org, slug in (("ORG-A", "a"), ("ORG-B", "b")):
                c.execute(
                    "INSERT INTO organizations (org_id, name, slug, created_at_utc) VALUES (?,?,?,?)",
                    (org, org, slug, "t"),
                )
            for acct in ("ACCT-1", "ACCT-2", "ACCT-3"):
                c.execute("INSERT INTO accounts (account_id, account_name) VALUES (?, ?)", (acct, acct))
            # ACCT-1 was granted to ORG-A; ACCT-2 to ORG-B; ACCT-3 to nobody.
            c.execute("INSERT INTO organization_accounts VALUES ('ORG-A', 'ACCT-1', 't1')")
            c.execute("INSERT INTO organization_accounts VALUES ('ORG-B', 'ACCT-2', 't2')")
            c.execute("INSERT INTO orders (order_id, account_id) VALUES ('ORD-1', 'ACCT-1')")
            c.execute("INSERT INTO orders (order_id, account_id) VALUES ('ORD-2', 'ACCT-3')")
            c.execute("INSERT INTO tickets (ticket_id, account_id) VALUES ('TKT-1', 'ACCT-2')")
            c.execute(
                "INSERT INTO ingestion_runs (started_at_utc, source_workbook_path, "
                "source_workbook_sha256, ingestion_script_version) VALUES ('t','p','h','v')"
            )
            for table, ident in (
                ("accounts", "ACCT-1"),
                ("orders", "ORD-1"),
                ("tickets", "TKT-1"),
                ("orders", "ORD-2"),
            ):
                c.execute(
                    "INSERT INTO source_provenance (ingestion_run_id, target_table, target_id, "
                    "source_file, source_sheet, source_row_number, raw_row_json) "
                    "VALUES (1, ?, ?, 'f', 's', 1, '{}')",
                    (table, ident),
                )
            c.execute(
                "INSERT INTO dataset_metadata VALUES (1, 'snap', '2026-01-01T00:00:00+00:00', "
                "'UTC', 'INR', NULL, NULL, 'w.xlsx', 'h', '[]', 't', 'v')"
            )
            c.execute(
                "INSERT INTO document_ingestion_runs (started_at_utc, source_dir, "
                "ingestion_script_version) VALUES ('t', 'd', 'v')"
            )
            for doc, acct in (("doc-general", None), ("doc-acct1", "ACCT-1"), ("doc-orphan", "ACCT-99")):
                c.execute(
                    "INSERT INTO documents (document_id, source_file, source_sha256, title, "
                    "document_type, status, status_raw, is_current, is_deprecated, is_authoritative, "
                    "authority_tier, account_id, page_count, ingestion_run_id) "
                    "VALUES (?, ?, 'h', 't', 'policy', 'current', 'c', 1, 0, 1, 1, ?, 1, 1)",
                    (doc, f"{doc}.pdf", acct),
                )
            for aid, acct in (("ACT-1", "ACCT-1"), ("ACT-2", None)):
                c.execute(
                    "INSERT INTO agent_actions (action_id, action_type, status, account_id, "
                    "target_type, target_id, parameters_json, preview, evidence_chunk_ids_json, "
                    "requested_by, requested_by_role, prepared_at_utc, expires_at_utc) "
                    "VALUES (?, 'x', 'executed', ?, 'ticket', 'TKT-1', '{}', 'p', '[]', 'u', 'r', 't', 't')",
                    (aid, acct),
                )
            c.execute(
                "INSERT INTO ticket_notes (note_id, action_id, ticket_id, account_id, note, "
                "created_by, created_at_utc) VALUES ('N-1', 'ACT-1', 'TKT-1', 'ACCT-1', 'n', 'u', 't')"
            )
        finally:
            c.close()

    def _one(self, c, sql, params=()):
        return c.execute(sql, params).fetchone()[0]

    def test_existing_ownership_is_kept_and_unowned_rows_go_to_the_legacy_workspace(self, pgdb):
        self._legacy(pgdb)
        assert pgdb.migrate() == ["0002", "0003"]
        c = pgdb.connect()
        try:
            owner = {
                r["account_id"]: r["org_id"]
                for r in c.execute("SELECT account_id, org_id FROM accounts")
            }
            assert owner == {
                "ACCT-1": "ORG-A",
                "ACCT-2": "ORG-B",
                "ACCT-3": "ORG-legacy-assessment",
            }
            assert self._one(c, "SELECT org_id FROM orders WHERE order_id = 'ORD-1'") == "ORG-A"
            assert self._one(c, "SELECT org_id FROM orders WHERE order_id = 'ORD-2'") == "ORG-legacy-assessment"
            assert self._one(c, "SELECT org_id FROM tickets WHERE ticket_id = 'TKT-1'") == "ORG-B"
            # Provenance follows the record it explains.
            prov = {
                (r["target_table"], r["target_id"]): r["org_id"]
                for r in c.execute("SELECT target_table, target_id, org_id FROM source_provenance")
            }
            assert prov[("accounts", "ACCT-1")] == "ORG-A"
            assert prov[("orders", "ORD-2")] == "ORG-legacy-assessment"
            assert prov[("tickets", "TKT-1")] == "ORG-B"
            # The single snapshot row becomes the legacy workspace's.
            assert self._one(c, "SELECT org_id FROM dataset_metadata") == "ORG-legacy-assessment"
        finally:
            c.close()

    def test_documents_split_into_system_workspace_and_legacy(self, pgdb):
        self._legacy(pgdb)
        pgdb.migrate()
        c = pgdb.connect()
        try:
            docs = {
                r["document_id"]: r["org_id"]
                for r in c.execute("SELECT document_id, org_id FROM documents")
            }
            assert docs["doc-general"] is None  # a system document
            assert docs["doc-acct1"] == "ORG-A"
            assert docs["doc-orphan"] == "ORG-legacy-assessment"  # names an account nobody has
        finally:
            c.close()

    def test_actions_and_their_effects_follow_their_account(self, pgdb):
        self._legacy(pgdb)
        pgdb.migrate()
        c = pgdb.connect()
        try:
            assert self._one(c, "SELECT org_id FROM agent_actions WHERE action_id = 'ACT-1'") == "ORG-A"
            assert (
                self._one(c, "SELECT org_id FROM agent_actions WHERE action_id = 'ACT-2'")
                == "ORG-legacy-assessment"
            )
            assert self._one(c, "SELECT org_id FROM ticket_notes WHERE note_id = 'N-1'") == "ORG-A"
        finally:
            c.close()

    def test_the_composite_keys_and_foreign_keys_hold_afterwards(self, pgdb):
        self._legacy(pgdb)
        pgdb.migrate()
        c = pgdb.connect()
        try:
            # Two workspaces may now both have an ACCT-1...
            c.execute("INSERT INTO accounts (org_id, account_id) VALUES ('ORG-B', 'ACCT-1')")
            # ...but not twice within one workspace,
            with pytest.raises(IntegrityError):
                c.execute("INSERT INTO accounts (org_id, account_id) VALUES ('ORG-B', 'ACCT-1')")
            # nor an order for an account its workspace does not have.
            with pytest.raises(IntegrityError):
                c.execute(
                    "INSERT INTO orders (org_id, order_id, account_id) VALUES ('ORG-A', 'ORD-9', 'ACCT-2')"
                )
            # A system document may not name an account.
            with pytest.raises(IntegrityError):
                c.execute(
                    "INSERT INTO documents (document_id, org_id, source_file, source_sha256, title, "
                    "document_type, status, status_raw, is_current, is_deprecated, is_authoritative, "
                    "authority_tier, account_id, page_count, ingestion_run_id) "
                    "VALUES ('bad', NULL, 'bad.pdf', 'h', 't', 'policy', 'current', 'c', 1, 0, 1, 1, 'ACCT-1', 1, 1)"
                )
        finally:
            c.close()

    def test_organization_accounts_is_now_a_view_of_the_accounts(self, pgdb):
        self._legacy(pgdb)
        pgdb.migrate()
        c = pgdb.connect()
        try:
            rows = {
                (r["org_id"], r["account_id"])
                for r in c.execute("SELECT org_id, account_id FROM organization_accounts")
            }
            assert ("ORG-A", "ACCT-1") in rows and ("ORG-B", "ACCT-2") in rows
            kind = self._one(
                c,
                "SELECT table_type FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = 'organization_accounts'",
            )
            assert kind == "VIEW"
        finally:
            c.close()

    def test_an_empty_database_gets_no_legacy_workspace(self, pgdb):
        pgdb.migrate()
        c = pgdb.connect()
        try:
            assert self._one(c, "SELECT count(*) FROM organizations") == 0
        finally:
            c.close()
