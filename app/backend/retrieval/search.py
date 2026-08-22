"""Agent-facing document search and evidence retrieval.

Ranking is Okapi BM25 computed in Python over the chunks the caller is
allowed to see. That choice is deliberate for this corpus:

- It is six documents and a few dozen chunks. An embedding index, a vector
  store, or an FTS5 virtual table would all be more machinery for the same
  answers, and would add a build step that can silently drift from the source
  PDFs.
- It is fully deterministic and needs no API key, so retrieval behaviour is
  unit-testable exactly like the policy math will be.
- Clause and identifier lookup ("KI-208", "ACCT-001", "INR 250", "P1") is
  mostly *lexical*, which is precisely where BM25 is strong and where
  embedding similarity tends to blur distinctions that matter here.

**Scoring never decides authority.** `search_documents` ranks by textual
relevance only; a deprecated policy can and does score well. Deciding which
source governs is `app.backend.retrieval.authority.resolve_authority`, which
works from stated document metadata. `search_and_resolve` composes the two in
the order an agent tool should use them.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from collections.abc import Collection, Sequence

from app.backend.models.documents import AuthorityDecision, Evidence
from app.backend.retrieval.authority import resolve_authority
from app.backend.services.documents import (
    fetch_searchable_evidence,
    get_document_chunks,
    get_evidence_by_chunk_ids,
)

# Standard BM25 parameters; no tuning was performed against the assessment's
# example questions, which would amount to fitting the test set.
BM25_K1 = 1.2
BM25_B = 0.75

DEFAULT_LIMIT = 8

# Intentionally tiny. Words that carry real meaning in this corpus — "no"
# ("no fee"), "not", "may", "must", "after", "within", "before" — are kept.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "with",
    }
)

# "3,000" -> "3000" so a query for "3000 rows" matches "3,000 rows".
_THOUSANDS_SEPARATOR = re.compile(r"(?<=\d),(?=\d{3}\b)")
# Keeps hyphenated identifiers whole: "ki-208", "acct-001", "first-response".
_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, preserving hyphenated identifiers.

    A hyphenated token is emitted whole *and* split, so "KI-208" matches a
    query for either "KI-208" or "208", and "failed-pickup" matches "pickup".
    """
    normalized = _THOUSANDS_SEPARATOR.sub("", text.lower())
    tokens: list[str] = []
    for match in _TOKEN.finditer(normalized):
        token = match.group(0)
        if token not in _STOPWORDS:
            tokens.append(token)
        if "-" in token:
            tokens.extend(p for p in token.split("-") if p and p not in _STOPWORDS)
    return tokens


def _indexed_text(evidence: Evidence) -> str:
    """What gets scored: the chunk text plus its document title and section
    path, so a query naming a section ("failed-pickup credits") or a document
    ("Northstar agreement") reaches the right chunk."""
    parts = [evidence.document_title]
    if evidence.section_path:
        parts.append(evidence.section_path)
    if evidence.customer_name:
        parts.append(evidence.customer_name)
    parts.append(evidence.text)
    return "\n".join(parts)


def search_documents(
    conn: sqlite3.Connection,
    query: str,
    *,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    min_score: float = 0.0,
    include_non_authoritative: bool = True,
) -> list[Evidence]:
    """Rank visible chunks against `query` by BM25 relevance.

    `query` is untrusted free text. It is only ever tokenized — it never
    reaches SQL, which is parameterized against the scoping values alone.

    Scoring statistics (document frequency, average length) are computed over
    the *visible* candidate set, not the whole corpus, so a document the
    caller may not see cannot influence the scores of ones they can.

    `include_non_authoritative=True` by default: deprecated material stays
    retrievable, because explaining that a rule changed requires quoting the
    superseded rule. It is flagged, and `resolve_authority` will never let it
    govern. Pass False when only in-force sources are wanted.

    Results are sorted by descending score, ties broken by chunk_id, so the
    same inputs always produce the same ordering.
    """
    candidates = fetch_searchable_evidence(
        conn,
        account_id=account_id,
        allowed_account_ids=allowed_account_ids,
        include_non_authoritative=include_non_authoritative,
    )
    query_terms = set(tokenize(query))
    if not candidates or not query_terms:
        return []

    tokenized = [tokenize(_indexed_text(c)) for c in candidates]
    total_docs = len(candidates)
    lengths = [len(t) for t in tokenized]
    average_length = sum(lengths) / total_docs or 1.0

    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))

    scored: list[Evidence] = []
    for evidence, tokens, length in zip(candidates, tokenized, lengths, strict=True):
        frequencies = Counter(tokens)
        score = 0.0
        for term in query_terms:
            term_frequency = frequencies.get(term, 0)
            if not term_frequency:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            denominator = term_frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * length / average_length
            )
            score += idf * (term_frequency * (BM25_K1 + 1)) / denominator
        if score > min_score:
            scored.append(evidence.model_copy(update={"score": round(score, 6)}))

    scored.sort(key=lambda e: (-(e.score or 0.0), e.chunk_id))
    return scored[:limit]


def search_and_resolve(
    conn: sqlite3.Connection,
    query: str,
    *,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    min_score: float = 0.0,
    include_non_authoritative: bool = True,
) -> AuthorityDecision:
    """Search, then resolve precedence over the results.

    This is the call an agent tool should make: it returns governing evidence
    separated from context, with an explicit note for every override and for
    any conflict precedence could not settle.

    `account_id` does double duty here, and deliberately so — it is both the
    retrieval scope (whose agreements are visible) and the precedence scope
    (whose agreement is allowed to outrank general policy). Forwarding it to
    both is what makes the common path correct by default: an unscoped
    question can never be decided by one customer's agreement.
    """
    evidence = search_documents(
        conn,
        query,
        account_id=account_id,
        allowed_account_ids=allowed_account_ids,
        limit=limit,
        min_score=min_score,
        include_non_authoritative=include_non_authoritative,
    )
    return resolve_authority(evidence, account_id=account_id)


def get_document_evidence(
    conn: sqlite3.Connection,
    *,
    chunk_ids: Sequence[str] | None = None,
    document_id: str | None = None,
    account_id: str | None = None,
    allowed_account_ids: Collection[str] | None = None,
) -> list[Evidence]:
    """Fetch evidence by identity rather than by relevance.

    Give exactly one of `chunk_ids` (re-resolve specific citations) or
    `document_id` (read a whole document in order). Account scoping applies
    identically to search: ids outside the caller's scope simply do not come
    back.
    """
    if (chunk_ids is None) == (document_id is None):
        raise ValueError("provide exactly one of chunk_ids or document_id")

    if document_id is not None:
        return get_document_chunks(
            conn,
            document_id,
            account_id=account_id,
            allowed_account_ids=allowed_account_ids,
        )
    return get_evidence_by_chunk_ids(
        conn,
        chunk_ids or [],
        account_id=account_id,
        allowed_account_ids=allowed_account_ids,
    )
