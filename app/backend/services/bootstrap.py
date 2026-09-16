"""Bring the public demo environment into existence, and keep it there.

A hosted demo on an ephemeral filesystem has a problem no amount of careful
configuration solves: the database is a *build artifact*, and the machine that
holds it is recreated whenever the platform feels like it. The deployment this
module exists for sleeps when idle and wakes with an empty disk, so the first
visitor after a quiet hour arrived at a sign-in page whose only advice was to
run two Python scripts they have no way to run.

The fix is to make the environment something the application *converges on*
rather than something somebody built once:

    ensure_demo_environment(settings)

It is safe to call on every demo sign-in, on startup, and from two threads at
the same moment. What it is not is expensive: it looks at what the database
actually contains and does only the missing part, so the ordinary call — the
one where everything is already there — is a handful of `COUNT(*)` queries.

Four properties hold, and each is load-bearing:

- **State is read from the database, never remembered.** There is no
  `INITIALIZED = True` anywhere in this module. A process that ran this
  successfully an hour ago may be looking at a filesystem that has been wiped
  since, and a process that has never run it may be looking at a database that
  is already complete. Only the database knows.
- **Every step is idempotent, and each checks before it writes.** Ingestion is
  not re-run because a request arrived; it is re-run because the rows are not
  there. The schema's own uniqueness — `organizations.slug`, `users.email`,
  the unique index on `organization_accounts.account_id` — is what makes the
  seeding half safe to repeat, exactly as `scripts/seed_demo.py` documents.
- **Nothing here is a second implementation.** The dataset and document
  ingestion are the repository's existing scripts, called as functions; the
  demo tenant is `scripts/seed_demo.py`'s `seed()`; the session is issued by
  `auth.service.login`. This module decides *whether* each runs, and nothing
  else. A parallel ingestion path would be a second thing to keep correct.
- **It grants nothing.** Roles come from `ROLE_PERMISSIONS`, the demo accounts
  are ordinary users, and the workspace holds only dataset accounts no other
  workspace has claimed. There is no demo-only permission and no path here
  that could invent one.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.backend.auth import repository as repo
from app.backend.auth import service as auth_service
from app.backend.auth.passwords import hash_password, verify_password
from app.backend.core.config import REPO_ROOT, Settings
from app.backend.services.database import get_connection, initialize_schema

#: Stage-by-stage diagnostics. Deliberately never used to log the demo
#: password — a log is a second copy of a credential, kept longer and read by
#: more people than the credential itself.
logger = logging.getLogger("astrion.bootstrap")


class DemoEnvironmentError(RuntimeError):
    """The demo environment could not be brought to a usable state.

    Carries the stage that failed, so an operator reading the log knows which
    half of the boot is broken while the caller answers the visitor with
    something that names neither a filesystem nor a script.
    """

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.detail = message


@dataclass(frozen=True)
class DemoEnvironmentReport:
    """What this call actually did. Empty on the common path, by design.

    Returned rather than logged and discarded so a caller — a test, a startup
    hook, the endpoint's own log line — can tell a first boot from the
    thousandth without inferring it from timing.
    """

    #: True when the database already satisfied every check and nothing ran.
    already_ready: bool = False
    database_created: bool = False
    dataset_ingested: bool = False
    documents_ingested: bool = False
    workspace_created: bool = False
    users_created: tuple[str, ...] = ()
    memberships_created: int = 0
    accounts_attached: tuple[str, ...] = ()
    #: Accounts a *different* workspace already owns. Reported, never moved:
    #: that unique index is the tenant boundary.
    accounts_skipped: tuple[str, ...] = ()
    duration_seconds: float = 0.0

    @property
    def changed(self) -> bool:
        """Whether this call wrote anything at all."""
        return not self.already_ready and bool(
            self.database_created
            or self.dataset_ingested
            or self.documents_ingested
            or self.workspace_created
            or self.users_created
            or self.memberships_created
            or self.accounts_attached
        )


# --- mutual exclusion ---------------------------------------------------------
#
# Two visitors clicking at the same moment on a cold start must not each build
# half an environment. Two mechanisms, because they cover different failures:
#
# - `_LOCAL_LOCK` serialises threads inside one process, which is the case that
#   actually happens: FastAPI runs synchronous handlers in a threadpool, so two
#   concurrent requests are two real threads in one interpreter.
# - the lock *file* serialises processes, for a deployment running more than
#   one worker.
#
# Neither is what makes the result correct. Correctness rests on SQLite's own
# write serialisation, on each ingestion being a single transaction, and on the
# schema's uniqueness constraints — the locks exist so the second caller waits
# for the first rather than duplicating its work. A lock that could not be
# acquired is therefore a reason to re-check the database, not to fail.

_LOCAL_LOCK = threading.Lock()

#: How long to wait for another process to finish before giving up on the lock
#: and re-checking the database anyway. Ingesting the supplied source pack takes
#: well under a second, so this is generous by two orders of magnitude.
LOCK_WAIT_SECONDS = 60.0
#: A lock older than this belonged to a process that died holding it. Breaking
#: it is safe for the same reason waiting is: the work underneath is idempotent.
LOCK_STALE_SECONDS = 180.0
_LOCK_POLL_SECONDS = 0.05


def _lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".bootstrap.lock")


@contextmanager
def _cross_process_lock(path: Path) -> Iterator[bool]:
    """Hold an advisory lock file, yielding whether it was acquired.

    O_CREAT with O_EXCL is atomic on both POSIX and Windows, which is the whole
    mechanism — no dependency, no daemon, and nothing to clean up but a file.
    """
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    handle: int | None = None

    while True:
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                # Released between the failed open and the stat. Retry at once.
                continue
            if age > LOCK_STALE_SECONDS:
                logger.warning(
                    "breaking a demo bootstrap lock left behind %.0fs ago", age
                )
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                # The caller re-checks the database regardless, so a contended
                # lock costs a duplicated check, never a failed sign-in.
                logger.warning("gave up waiting for the demo bootstrap lock")
                yield False
                return
            time.sleep(_LOCK_POLL_SECONDS)
        except OSError as exc:
            # A read-only or missing directory is a real deployment fault, and
            # one the caller should hear about as a stage rather than as a
            # mysterious permission error from inside a context manager.
            raise DemoEnvironmentError("lock", str(exc)) from exc

    try:
        os.write(handle, str(os.getpid()).encode("ascii"))
        yield True
    finally:
        try:
            os.close(handle)
        finally:
            try:
                path.unlink()
            except OSError:
                pass


# --- what "ready" means -------------------------------------------------------


def _count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row else 0


def dataset_present(conn: sqlite3.Connection) -> bool:
    """Whether the assessment workbook has been loaded.

    All four conditions are required together: the metadata row is what the
    policy engine reads the dataset snapshot from, and rows in only some of the
    tables mean a run that died partway rather than a dataset.
    """
    try:
        metadata = conn.execute("SELECT 1 FROM dataset_metadata WHERE id = 1").fetchone()
        return bool(metadata) and all(
            _count(conn, table) > 0 for table in ("accounts", "orders", "tickets")
        )
    except sqlite3.Error:
        return False


def documents_present(conn: sqlite3.Connection) -> bool:
    """Whether the supplied PDFs have been extracted and chunked.

    Chunks are checked as well as documents because retrieval reads chunks; a
    document row with nothing under it would answer every question with silence.
    """
    try:
        return _count(conn, "documents") > 0 and _count(conn, "document_chunks") > 0
    except sqlite3.Error:
        return False


def demo_tenant_present(conn: sqlite3.Connection, settings: Settings) -> bool:
    """Whether the demo workspace, its sign-in user, and their link all exist."""
    from scripts.seed_demo import DEMO_WORKSPACE_SLUG

    row = conn.execute(
        "SELECT org_id FROM organizations WHERE slug = ?", (DEMO_WORKSPACE_SLUG,)
    ).fetchone()
    if row is None:
        return False
    org_id = row["org_id"]

    user = repo.get_user_by_email(conn, repo.normalize_email(settings.demo_email))
    if user is None or not user.is_active:
        return False
    if settings.require_verified_email and not user.email_verified:
        return False
    if repo.get_membership(conn, org_id=org_id, user_id=user.user_id) is None:
        return False

    # A workspace with no accounts can be signed into and then shows nothing,
    # which reads as a broken product rather than an unfinished boot.
    return len(repo.accounts_for_org(conn, org_id)) > 0


def demo_environment_ready(conn: sqlite3.Connection, settings: Settings) -> bool:
    """Every condition a demo sign-in depends on, checked against the database.

    Deliberately excludes whether the stored password matches the configured
    one. Verifying a scrypt hash costs about as much as the sign-in itself, and
    a hash that disagrees is repaired by `sign_in_demo_user` on the one attempt
    that discovers it rather than paid for on every attempt that does not.
    """
    return (
        dataset_present(conn)
        and documents_present(conn)
        and demo_tenant_present(conn, settings)
    )


def _import_ingestion():
    """Import the repository's own ingestion scripts.

    They are modules under `scripts/`, not part of the application package, and
    they are imported here rather than reimplemented: the assessment workbook
    and the supplied PDFs have exactly one loader each, and a second one that
    drifted would be worse than the cold start this module exists to fix.

    The path fix-up mirrors what each script does for itself when run directly,
    so an application started from somewhere other than the repository root
    still finds them.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts import ingest_dataset, ingest_documents

    return ingest_dataset, ingest_documents


# --- the bootstrap ------------------------------------------------------------


def ensure_demo_environment(settings: Settings) -> DemoEnvironmentReport:
    """Converge on a usable demo environment. Safe to call repeatedly.

    Returns what it had to do. Raises `DemoEnvironmentError`, naming the stage,
    when it cannot finish — the caller is expected to log that and tell the
    visitor something that mentions neither a script nor a path.
    """
    started = time.perf_counter()
    db_path = Path(settings.database_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DemoEnvironmentError("filesystem", str(exc)) from exc

    # The cheap check first, and without either lock: the overwhelmingly common
    # case is a warm process whose database is already complete, and making that
    # case wait on a mutex would serialise every demo sign-in behind every other.
    if db_path.exists():
        conn = get_connection(db_path)
        try:
            if demo_environment_ready(conn, settings):
                return DemoEnvironmentReport(
                    already_ready=True,
                    duration_seconds=time.perf_counter() - started,
                )
        except sqlite3.Error:
            # A database that cannot even be read is one to rebuild, not one to
            # refuse over. Fall through to the guarded path.
            pass
        finally:
            conn.close()

    with _LOCAL_LOCK, _cross_process_lock(_lock_path(db_path)):
        return _build(settings, db_path, started)


def _build(settings: Settings, db_path: Path, started: float) -> DemoEnvironmentReport:
    """The guarded half.

    Re-checks everything first: the winner of the lock may have finished the
    whole job while this caller was waiting for it, and the point of waiting was
    to be able to skip the work rather than repeat it.
    """
    database_created = not db_path.exists()

    # --- schema --------------------------------------------------------------
    try:
        conn = get_connection(db_path)
        initialize_schema(conn)
    except Exception as exc:  # sqlite3.Error, and the exclusivity RuntimeError
        raise DemoEnvironmentError("schema", str(exc)) from exc

    try:
        if demo_environment_ready(conn, settings):
            return DemoEnvironmentReport(
                already_ready=True,
                database_created=database_created,
                duration_seconds=time.perf_counter() - started,
            )
        needs_dataset = not dataset_present(conn)
        needs_documents = not documents_present(conn)
    finally:
        # Ingestion opens its own connections and takes exclusive transactions.
        # Holding an idle reader across that is not a deadlock — SQLite blocks
        # only on a concurrent *write* — but closing first keeps the window in
        # which two connections address one file as small as it can be.
        conn.close()

    ingest_dataset, ingest_documents = _import_ingestion()

    # --- dataset -------------------------------------------------------------
    if needs_dataset:
        logger.info("demo bootstrap: ingesting the dataset")
        try:
            ingest_dataset.ingest(db_path=db_path)
        except Exception as exc:
            raise DemoEnvironmentError("dataset", str(exc)) from exc

    # --- documents -----------------------------------------------------------
    if needs_documents:
        logger.info("demo bootstrap: ingesting the documents")
        try:
            ingest_documents.ingest(db_path=db_path)
        except Exception as exc:
            raise DemoEnvironmentError("documents", str(exc)) from exc

    # --- the demo tenant -----------------------------------------------------
    from scripts import seed_demo

    conn = get_connection(db_path)
    try:
        logger.info("demo bootstrap: ensuring the demo workspace")
        summary = seed_demo.seed(conn, password=settings.demo_password)
    except Exception as exc:
        conn.close()
        raise DemoEnvironmentError("workspace", str(exc)) from exc

    # --- prove it is usable --------------------------------------------------
    try:
        if not demo_environment_ready(conn, settings):
            raise DemoEnvironmentError(
                "verify",
                "the demo environment is still incomplete after initialisation",
            )
    finally:
        conn.close()

    report = DemoEnvironmentReport(
        database_created=database_created,
        dataset_ingested=needs_dataset,
        documents_ingested=needs_documents,
        workspace_created=bool(summary["workspace_created"]),
        users_created=tuple(summary["users_created"]),
        memberships_created=len(summary["memberships_created"]),
        accounts_attached=tuple(summary["accounts_attached"]),
        accounts_skipped=tuple(account for account, _owner in summary["accounts_skipped"]),
        duration_seconds=time.perf_counter() - started,
    )
    logger.info(
        "demo bootstrap complete in %.2fs (database_created=%s dataset=%s "
        "documents=%s workspace=%s users=%d memberships=%d accounts=%d)",
        report.duration_seconds,
        report.database_created,
        report.dataset_ingested,
        report.documents_ingested,
        report.workspace_created,
        len(report.users_created),
        report.memberships_created,
        len(report.accounts_attached),
    )
    return report


# --- signing the visitor in ----------------------------------------------------


def repair_demo_credential(conn: sqlite3.Connection, settings: Settings) -> bool:
    """Make the stored demo password agree with the configured one.

    Reachable only when a sign-in with the server's own credential has just been
    refused, which means the row predates the current configuration — a database
    carried across a password change, or an address someone registered before
    the seed ran. Returns whether anything was written.

    This is not a password reset facility. It writes one address, the one the
    server itself authenticates as, and it is never reachable with a
    caller-supplied address: `settings.demo_email` is the only address it has.
    """
    user = repo.get_user_by_email(conn, repo.normalize_email(settings.demo_email))
    if user is None:
        return False

    repaired = False
    if not verify_password(settings.demo_password, user.password_hash):
        repo.set_password_hash(
            conn, user.user_id, hash_password(settings.demo_password)
        )
        repaired = True
    if settings.require_verified_email and not user.email_verified:
        repo.mark_email_verified(conn, user.user_id)
        repaired = True
    if repaired:
        logger.warning(
            "demo bootstrap: repaired the stored credential for the configured "
            "demo address"
        )
    return repaired


def sign_in_demo_user(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    client_ip: str | None,
    user_agent: str | None,
    request_id: str | None = None,
) -> auth_service.LoginResult:
    """Sign in as the demo account through the ordinary login path.

    Not a shortcut around authentication: this calls the same
    `auth.service.login` the typed form reaches, so the session that comes back
    passed the same password verification, produced the same audit entry, and
    carries the same workspace scoping as anybody else's. What differs is only
    where the credential came from — the server's own configuration, never the
    request.

    One retry, and only after a repair. A credential the server supplied to
    itself being refused means the stored hash is stale, not that a visitor
    guessed wrong; repairing it and trying once more is what keeps a changed
    password from bricking the demo. A second refusal is a real failure and is
    raised.
    """
    try:
        return auth_service.login(
            conn,
            email=settings.demo_email,
            password=settings.demo_password,
            client_ip=client_ip,
            user_agent=user_agent,
            request_id=request_id,
            require_verified_email=settings.require_verified_email,
        )
    except auth_service.AccountLocked:
        # Lockout is a real control doing its job, and clearing it here would
        # hand an attacker a way to clear it too. Raised for the caller to
        # translate into something a visitor can act on.
        raise
    except auth_service.InvalidCredentials:
        if not repair_demo_credential(conn, settings):
            raise
        return auth_service.login(
            conn,
            email=settings.demo_email,
            password=settings.demo_password,
            client_ip=client_ip,
            user_agent=user_agent,
            request_id=request_id,
            require_verified_email=settings.require_verified_email,
        )
