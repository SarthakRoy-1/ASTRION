"""Versioned schema migrations.

Migrations are plain SQL files, `sql/<dialect>/NNNN_name.sql`, applied in
order and recorded in `schema_migrations`. They run when an operator or the
deploy step asks (`python -m app.backend.db.migrate`), never from a request
handler: DDL on the hot path is how a slow deploy becomes an outage.

Rules that keep this boring:

- **Immutable.** A migration that has been applied is never edited; its
  checksum is stored, and a file that no longer matches stops the run rather
  than letting two databases quietly diverge. Change the schema by adding a
  new file.
- **One transaction per migration.** PostgreSQL DDL is transactional, so a
  migration either applies completely or not at all.
- **One runner at a time.** A transaction-scoped advisory lock serialises
  concurrent deploys (two instances starting together) without a session lock
  that a transaction-mode pooler would drop.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.backend.db.errors import SchemaNotReadyError

logger = logging.getLogger("astrion.db.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "sql"
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT COLLATE "C" PRIMARY KEY,
    name TEXT COLLATE "C" NOT NULL,
    checksum TEXT COLLATE "C" NOT NULL,
    applied_at_utc TEXT COLLATE "C" NOT NULL
)
"""


class MigrationError(RuntimeError):
    """The migrations on disk and the migrations in the database disagree."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str


def discover(dialect: str) -> list[Migration]:
    """Every migration file for `dialect`, in version order."""
    directory = MIGRATIONS_DIR / dialect
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name}: migration files are named NNNN_snake_case.sql"
            )
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        found.append(
            Migration(
                version=match.group(1),
                name=match.group(2),
                sql=text,
                checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration version in {directory}")
    return found


def _applied(conn) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {r["version"]: (r["name"], r["checksum"]) for r in rows}


def _verify(applied: dict[str, tuple[str, str]], available: list[Migration]) -> None:
    by_version = {m.version: m for m in available}
    for version, (name, checksum) in applied.items():
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"the database has migration {version}_{name}, which this code does not "
                "contain. Deploy a version that includes it before migrating."
            )
        if migration.checksum != checksum:
            raise MigrationError(
                f"migration {version}_{name} was edited after it was applied. "
                "Migrations are immutable; add a new one instead."
            )


def apply_migrations(conn, dialect: str, *, up_to: str | None = None) -> list[str]:
    """Apply pending migrations, in order, and return the versions applied.

    `up_to` stops after that version, so a test can build the schema as it stood
    at an earlier release, put data in it, and migrate the rest.
    """
    if dialect != "postgres":
        raise MigrationError(f"versioned migrations are not used for {dialect}")

    available = discover(dialect)
    applied_now: list[str] = []
    with conn:
        # Held until this transaction ends. A second runner waits here, then
        # sees the first one's work and applies nothing.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('astrion:migrations'))")
        conn.run_script(_CREATE_TABLE)
        applied = _applied(conn)
        _verify(applied, available)
        for migration in available:
            if migration.version in applied:
                continue
            if up_to is not None and migration.version > up_to:
                break
            logger.info("applying migration %s_%s", migration.version, migration.name)
            conn.run_script(migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at_utc) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            applied_now.append(migration.version)
    return applied_now


def check_current(conn, dialect: str) -> None:
    """Raise `SchemaNotReadyError` unless every migration has been applied."""
    available = discover(dialect)
    try:
        applied = _applied(conn)
    except Exception as exc:  # the table itself is missing
        raise SchemaNotReadyError(
            "the database has not been migrated; run "
            "`python -m app.backend.db.migrate`"
        ) from exc
    _verify(applied, available)
    missing = [m.version for m in available if m.version not in applied]
    if missing:
        raise SchemaNotReadyError(
            f"the database is behind: migrations {', '.join(missing)} are pending; "
            "run `python -m app.backend.db.migrate`"
        )
