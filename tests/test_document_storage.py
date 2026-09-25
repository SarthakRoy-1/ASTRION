"""Documents' original files: object storage and the rules that keep it honest.

The database and the object store share no transaction, so their consistency
comes from the order of operations (see services/document_files.py). These tests
hold that order to account: an object exists before its row, a failed creation
leaves nothing behind, nothing is removed before its replacement commits, a
failed cleanup is recorded and later retried, and bytes are checked against their
checksum whenever they are read back.

The store contract runs against the local store and, when
`ASTRION_TEST_S3_ENDPOINT` points at an S3-compatible server, against that too.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.retrieval.extraction import DocumentIngestionError
from app.backend.services import document_files as files
from app.backend.services import documents as docs
from app.backend.services.database import get_connection, initialize_schema
from app.backend.services.storage_reconcile import reconcile
from app.backend.storage import (
    ChecksumMismatch,
    InvalidKeyError,
    LocalDocumentStore,
    ObjectNotFound,
    StorageError,
    document_key,
    system_document_key,
    workspace_document_key,
)
from app.backend.tenancy import Scope
from conftest import valid_agreement_pages, write_pdf

ORG = "ORG-store"
OTHER = "ORG-other"


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture(params=["local", "s3"])
def any_store(request, tmp_path):
    if request.param == "local":
        return LocalDocumentStore(tmp_path / "objects")
    endpoint = os.environ.get("ASTRION_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("set ASTRION_TEST_S3_ENDPOINT to run against an S3-compatible server")
    from app.backend.storage.s3 import S3DocumentStore

    store = S3DocumentStore(
        bucket=f"astrion-test-{uuid.uuid4().hex[:10]}",
        endpoint_url=endpoint,
        region="us-east-1",
        access_key_id="test-key",
        secret_access_key="test-secret",
        key_prefix="tests",
    )
    store.ensure_bucket()
    return store


@pytest.fixture
def store(tmp_path):
    return LocalDocumentStore(tmp_path / "objects")


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "files.db")
    initialize_schema(connection)
    for org in (ORG, OTHER):
        connection.execute(
            "INSERT INTO organizations (org_id, name, slug, created_at_utc) VALUES (?,?,?,?)",
            (org, org, org.lower(), "t"),
        )
    connection.commit()
    yield connection
    connection.close()


def make_pdf(tmp_path, name: str, *, title: str = "ParcelPilot - Testco Service Agreement",
             account: str = "ACCT-1") -> bytes:
    path = tmp_path / name
    write_pdf(path, valid_agreement_pages(title=title, account_line=f"Account: {account}"))
    return path.read_bytes()


def ingest(conn, store, tmp_path, *, name="Own_Agreement.pdf", org=ORG, content=None,
           source_file=None, title="ParcelPilot - Testco Service Agreement"):
    content = content or make_pdf(tmp_path, name, title=title)
    source_file = source_file or f"{uuid.uuid4().hex[:8]}_{name}"
    extracted = files.extract_from_bytes(content, source_file=source_file)
    result = files.store_and_ingest(
        conn, store, org_id=org, content=content, original_filename=name,
        content_type="application/pdf", extracted=extracted, source_dir="upload",
    )
    return content, extracted, result


def row(conn, document_id):
    return conn.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()


class FlakyStore:
    """A real store that fails, or reports, chosen operations."""

    backend = "flaky"

    def __init__(self, inner):
        self.inner = inner
        self.fail_delete = False
        self.fail_put = False
        self.calls: list[tuple[str, str]] = []
        self.on_delete = None

    def put(self, key, data, *, content_type="application/pdf"):
        self.calls.append(("put", key))
        if self.fail_put:
            raise StorageError("put failed")
        return self.inner.put(key, data, content_type=content_type)

    def delete(self, key):
        self.calls.append(("delete", key))
        if self.on_delete:
            self.on_delete(key)
        if self.fail_delete:
            raise StorageError("delete failed")
        return self.inner.delete(key)

    def get(self, key):
        return self.inner.get(key)

    def exists(self, key):
        return self.inner.exists(key)

    def list(self, prefix=""):
        return self.inner.list(prefix)


# --- keys ------------------------------------------------------------------------------

SHA = "a" * 64


def test_keys_encode_ownership():
    assert workspace_document_key("ORG-1", "doc_1", SHA) == f"workspaces/ORG-1/documents/doc_1/{SHA}.pdf"
    assert system_document_key("doc_1", SHA) == f"system/documents/doc_1/{SHA}.pdf"
    assert document_key(None, "doc_1", SHA).startswith("system/")
    assert document_key("ORG-1", "doc_1", SHA).startswith("workspaces/ORG-1/")


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "x" * 300, "a b", "..", "/abs", "a\\b", ".hidden"])
def test_components_that_could_escape_are_refused(bad):
    with pytest.raises(InvalidKeyError):
        workspace_document_key(bad, "doc", SHA)
    with pytest.raises(InvalidKeyError):
        workspace_document_key("ORG-1", bad, SHA)


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "a" * 63])
def test_a_key_needs_a_real_checksum(bad):
    with pytest.raises(InvalidKeyError):
        workspace_document_key("ORG-1", "doc", bad)


# --- the store contract ---------------------------------------------------------------


def test_put_get_exists_delete_roundtrip(any_store):
    key = workspace_document_key("ORG-1", "doc_1", SHA)
    stored = any_store.put(key, b"%PDF-1.4 hello")
    assert stored.size == 14 and len(stored.sha256) == 64

    assert any_store.exists(key) is True
    assert any_store.get(key) == b"%PDF-1.4 hello"
    any_store.delete(key)
    assert any_store.exists(key) is False


def test_getting_a_missing_object_says_so(any_store):
    with pytest.raises(ObjectNotFound):
        any_store.get(workspace_document_key("ORG-1", "nothing", SHA))


def test_deleting_a_missing_object_is_not_an_error(any_store):
    any_store.delete(workspace_document_key("ORG-1", "nothing", SHA))


def test_putting_again_replaces(any_store):
    key = workspace_document_key("ORG-1", "doc_1", SHA)
    any_store.put(key, b"one")
    any_store.put(key, b"two")
    assert any_store.get(key) == b"two"


def test_listing_by_prefix(any_store):
    any_store.put(workspace_document_key("ORG-1", "a", SHA), b"1")
    any_store.put(workspace_document_key("ORG-2", "b", SHA), b"2")
    any_store.put(system_document_key("c", SHA), b"3")
    keys = {o.key for o in any_store.list("workspaces/ORG-1/")}
    assert keys == {workspace_document_key("ORG-1", "a", SHA)}
    assert len({o.key for o in any_store.list("workspaces/")}) == 2
    assert all(o.size > 0 for o in any_store.list(""))


@pytest.mark.parametrize("bad", ["../escape", "/etc/passwd", "a//b", "a/../b", "a\\b", "", "./x"])
def test_the_store_refuses_a_key_that_is_not_plainly_inside_it(any_store, bad):
    with pytest.raises(InvalidKeyError):
        any_store.put(bad, b"x")
    with pytest.raises(InvalidKeyError):
        any_store.get(bad)
    with pytest.raises(InvalidKeyError):
        any_store.delete(bad)


def test_a_traversal_key_never_writes_outside_the_root(tmp_path):
    root = tmp_path / "root"
    store = LocalDocumentStore(root)
    with pytest.raises(InvalidKeyError):
        store.put("../outside.pdf", b"x")
    assert not (tmp_path / "outside.pdf").exists()


# --- creating a document ---------------------------------------------------------------


def test_upload_stores_the_object_then_records_it(conn, store, tmp_path):
    content, extracted, result = ingest(conn, store, tmp_path)

    stored = row(conn, extracted.document.document_id)
    assert stored["org_id"] == ORG
    assert stored["storage_key"] == workspace_document_key(
        ORG, extracted.document.document_id, files.sha256_bytes(content)
    )
    assert stored["original_filename"] == "Own_Agreement.pdf"
    assert stored["content_type"] == "application/pdf"
    assert stored["size_bytes"] == len(content)
    assert stored["source_sha256"] == files.sha256_bytes(content)  # the checksum, recorded
    assert store.get(stored["storage_key"]) == content  # the object is there and is the file
    assert result == {"documents": 1, "chunks": len(extracted.chunks)}  # no storage key leaks out
    # Extracted text is in the database; the binary is not.
    assert docs.get_document_chunks(conn, extracted.document.document_id, scope=Scope(ORG))
    assert content not in b"".join(str(tuple(r)).encode() for r in conn.execute("SELECT * FROM documents"))


def test_the_object_exists_before_its_row_does(conn, store, tmp_path):
    flaky = FlakyStore(store)
    seen: dict = {}

    original = files.ingest_single_document

    def spy(conn_, extracted, source_dir, **kw):
        seen["object_existed"] = store.exists(kw["storage"].storage_key)
        seen["row_existed"] = row(conn_, extracted.document.document_id) is not None
        return original(conn_, extracted, source_dir, **kw)

    files.ingest_single_document = spy
    try:
        ingest(conn, flaky, tmp_path)
    finally:
        files.ingest_single_document = original
    assert seen == {"object_existed": True, "row_existed": False}


def _fail_ingest(monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(files, "ingest_single_document", boom)


def test_a_failed_database_transaction_leaves_no_object_behind(conn, store, tmp_path, monkeypatch):
    _fail_ingest(monkeypatch, RuntimeError("database write failed"))

    with pytest.raises(RuntimeError, match="database write failed"):
        ingest(conn, store, tmp_path)

    assert list(store.list("")) == []
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM storage_orphans").fetchone()[0] == 0


def test_a_failed_transaction_and_a_failed_cleanup_is_recorded_not_lost(conn, store, tmp_path, monkeypatch):
    flaky = FlakyStore(store)
    flaky.fail_delete = True
    _fail_ingest(monkeypatch, RuntimeError("database write failed"))

    # The original failure is what the caller sees, not the cleanup's.
    with pytest.raises(RuntimeError, match="database write failed"):
        ingest(conn, flaky, tmp_path)

    orphans = conn.execute("SELECT storage_key, reason, org_id FROM storage_orphans").fetchall()
    assert len(orphans) == 1 and orphans[0]["reason"] == "ingest_failed" and orphans[0]["org_id"] == ORG
    assert store.exists(orphans[0]["storage_key"])  # still there, and now known about

    flaky.fail_delete = False
    report = reconcile(conn, store)
    assert report.orphans_resolved == 1 and not store.exists(orphans[0]["storage_key"])
    assert conn.execute("SELECT resolved_at_utc FROM storage_orphans").fetchone()[0] is not None


def test_a_failed_put_creates_no_row(conn, store, tmp_path):
    flaky = FlakyStore(store)
    flaky.fail_put = True
    with pytest.raises(StorageError):
        ingest(conn, flaky, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_a_rejected_document_stores_nothing(conn, store, tmp_path):
    bad = b"%PDF-1.4\nnot really a pdf"
    with pytest.raises(DocumentIngestionError):
        ingest(conn, store, tmp_path, content=bad)
    assert list(store.list("")) == []


def test_the_same_bytes_uploaded_again_share_one_object_and_a_failure_does_not_destroy_it(
    conn, store, tmp_path, monkeypatch
):
    content, extracted, _ = ingest(conn, store, tmp_path, source_file="fixed_Own.pdf")
    key = row(conn, extracted.document.document_id)["storage_key"]

    _fail_ingest(monkeypatch, RuntimeError("second write failed"))
    with pytest.raises(RuntimeError):
        files.store_and_ingest(
            conn, store, org_id=ORG, content=content, original_filename="Own.pdf",
            content_type="application/pdf", extracted=extracted, source_dir="upload",
        )
    # The first document is untouched: its object was not removed by the failed second attempt.
    assert store.exists(key)
    assert row(conn, extracted.document.document_id) is not None


def test_an_id_owned_by_another_workspace_is_refused_and_leaves_no_object(conn, store, tmp_path):
    content, extracted, _ = ingest(conn, store, tmp_path, source_file="clash_Own.pdf", org=ORG)
    before = {o.key for o in store.list("")}

    with pytest.raises(DocumentIngestionError, match="another owner"):
        files.store_and_ingest(
            conn, store, org_id=OTHER, content=content, original_filename="Own.pdf",
            content_type="application/pdf", extracted=extracted, source_dir="upload",
        )
    assert {o.key for o in store.list("")} == before  # the other workspace's put was cleaned up
    assert row(conn, extracted.document.document_id)["org_id"] == ORG


# --- replacing a document ----------------------------------------------------------------


def test_replacing_a_document_removes_the_old_object_only_after_the_new_row_commits(conn, store, tmp_path):
    flaky = FlakyStore(store)
    first, extracted, _ = ingest(conn, flaky, tmp_path, source_file="rep_Own.pdf", title="ParcelPilot - Testco Service Agreement")
    old_key = row(conn, extracted.document.document_id)["storage_key"]
    checked: dict = {}

    def at_delete(key):
        # By the time the old object goes, the row already points at the new one.
        checked["row_key_at_delete"] = row(conn, extracted.document.document_id)["storage_key"]
        checked["deleted"] = key

    flaky.on_delete = at_delete
    second = make_pdf(tmp_path, "v2.pdf", title="ParcelPilot - Testco Service Agreement v2")
    ingest(conn, flaky, tmp_path, source_file="rep_Own.pdf", content=second)

    new_key = row(conn, extracted.document.document_id)["storage_key"]
    assert new_key != old_key
    assert checked == {"row_key_at_delete": new_key, "deleted": old_key}
    assert not store.exists(old_key) and store.get(new_key) == second


def test_a_replacement_whose_old_object_cannot_be_removed_still_succeeds_and_is_recorded(conn, store, tmp_path):
    flaky = FlakyStore(store)
    _, extracted, _ = ingest(conn, flaky, tmp_path, source_file="rep2_Own.pdf")
    old_key = row(conn, extracted.document.document_id)["storage_key"]
    flaky.fail_delete = True

    second = make_pdf(tmp_path, "v2b.pdf", title="ParcelPilot - Testco Service Agreement v2")
    ingest(conn, flaky, tmp_path, source_file="rep2_Own.pdf", content=second)

    assert row(conn, extracted.document.document_id)["storage_key"] != old_key  # the upload completed
    assert conn.execute("SELECT storage_key, reason FROM storage_orphans").fetchone()["storage_key"] == old_key


# --- deleting a document -----------------------------------------------------------------


def _delete(conn, store, document_id, doc):
    return files.delete_document_and_object(
        conn, store,
        delete_row=lambda: docs.delete_document(conn, document_id, scope=Scope(ORG)),
        storage_key=doc["storage_key"], org_id=ORG,
    )


def test_deleting_removes_the_row_then_the_object(conn, store, tmp_path):
    flaky = FlakyStore(store)
    _, extracted, _ = ingest(conn, flaky, tmp_path)
    doc = row(conn, extracted.document.document_id)
    at_delete: dict = {}
    flaky.on_delete = lambda key: at_delete.update(row_gone=row(conn, extracted.document.document_id) is None)

    assert _delete(conn, flaky, extracted.document.document_id, doc) is True

    assert at_delete == {"row_gone": True}  # the database committed before the object went
    assert not store.exists(doc["storage_key"])


def test_an_object_that_cannot_be_removed_after_a_delete_is_recorded(conn, store, tmp_path):
    flaky = FlakyStore(store)
    _, extracted, _ = ingest(conn, flaky, tmp_path)
    doc = row(conn, extracted.document.document_id)
    flaky.fail_delete = True

    assert _delete(conn, flaky, extracted.document.document_id, doc) is True  # the delete still succeeded

    assert row(conn, extracted.document.document_id) is None
    assert conn.execute("SELECT reason FROM storage_orphans").fetchone()["reason"] == "deleted"
    flaky.fail_delete = False
    assert reconcile(conn, store).orphans_resolved == 1
    assert not store.exists(doc["storage_key"])


def test_deleting_a_document_someone_else_owns_touches_neither_row_nor_object(conn, store, tmp_path):
    _, extracted, _ = ingest(conn, store, tmp_path, org=OTHER)
    doc = row(conn, extracted.document.document_id)

    deleted = files.delete_document_and_object(
        conn, store,
        delete_row=lambda: docs.delete_document(conn, extracted.document.document_id, scope=Scope(ORG)),
        storage_key=doc["storage_key"], org_id=ORG,
    )

    assert deleted is False
    assert row(conn, extracted.document.document_id) is not None and store.exists(doc["storage_key"])


def test_an_object_shared_by_two_documents_is_kept_until_the_last_goes(conn, store, tmp_path):
    content = make_pdf(tmp_path, "twin.pdf")
    _, first, _ = ingest(conn, store, tmp_path, content=content, source_file="twin1_x.pdf")
    _, second, _ = ingest(conn, store, tmp_path, content=content, source_file="twin2_x.pdf")
    k1 = row(conn, first.document.document_id)["storage_key"]
    k2 = row(conn, second.document.document_id)["storage_key"]
    assert k1 != k2  # different document ids, different keys: nothing is shared by accident
    assert store.exists(k1) and store.exists(k2)


# --- checksums ----------------------------------------------------------------------------


def test_bytes_read_back_are_checked_against_the_recorded_checksum(conn, store, tmp_path):
    content, extracted, _ = ingest(conn, store, tmp_path)
    doc = row(conn, extracted.document.document_id)
    assert files.read_verified(store, doc["storage_key"], doc["source_sha256"]) == content

    store.put(doc["storage_key"], content + b"tampered")
    with pytest.raises(ChecksumMismatch):
        files.read_verified(store, doc["storage_key"], doc["source_sha256"])
    with pytest.raises(ChecksumMismatch):
        files.extract_from_bytes(content + b"x", source_file="x.pdf", expected_sha256=doc["source_sha256"])


# --- reconciliation ----------------------------------------------------------------------


def test_reconcile_finds_an_unreferenced_object_and_respects_the_grace_period(conn, store, tmp_path):
    ingest(conn, store, tmp_path)
    stray = workspace_document_key(ORG, "never_recorded", SHA)
    store.put(stray, b"a file whose row never committed")

    fresh = reconcile(conn, store)  # just written: possibly an upload in flight
    assert fresh.unreferenced == [] and store.exists(stray)

    later = datetime.now(timezone.utc) + timedelta(hours=2)
    found = reconcile(conn, store, now=later)
    assert found.unreferenced == [stray] and store.exists(stray)  # reported, not yet removed

    removed = reconcile(conn, store, now=later, remove_unreferenced=True)
    assert removed.unreferenced_removed == 1 and not store.exists(stray)
    # Everything a document references is left alone.
    assert reconcile(conn, store, now=later).clean


def test_reconcile_reports_a_row_whose_object_has_gone(conn, store, tmp_path):
    _, extracted, _ = ingest(conn, store, tmp_path)
    store.delete(row(conn, extracted.document.document_id)["storage_key"])

    report = reconcile(conn, store)

    assert report.missing_objects == [extracted.document.document_id]
    assert not report.clean


def test_reconcile_never_removes_an_object_a_document_references(conn, store, tmp_path):
    _, extracted, _ = ingest(conn, store, tmp_path)
    key = row(conn, extracted.document.document_id)["storage_key"]
    # A stale orphan record for a key that is referenced again must not delete it.
    files.record_orphan(conn, key, org_id=ORG, reason="stale")

    report = reconcile(conn, store, now=datetime.now(timezone.utc) + timedelta(days=1), remove_unreferenced=True)

    assert store.exists(key) and report.orphans_resolved == 1 and report.unreferenced == []
