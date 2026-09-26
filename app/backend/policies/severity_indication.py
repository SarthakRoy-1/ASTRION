"""Where a ticket's own words line up with a severity definition.

Severity is a judgement about business impact, and this system does not make it:
`evaluate_sla` sets a severity only when a person supplies one. An earlier draft
scored ticket text against the definitions and *chose* one; on the supplied corpus
a single shared word rated a billing question P1 and announced a breach. That is
the failure this module is written to avoid, so it does three things differently:

- **It only ever indicates.** The result names the clause of the policy the
  ticket resembles and says a person must verify it. Nothing downstream reads it
  as a severity, and no breach is asserted from it.
- **The clauses come from the policy, not from this file.** They are parsed out of
  the current support policy's own severity definitions (whichever document the
  authority layer says governs), so a revised policy changes what is matched. The
  only vocabulary held here is a small, general one: words that mean the same
  thing in any support ticket ("fails", "errors", "unable" are a failure; an API
  key, a password and a token are credentials).
- **It is conservative.** A clause needs at least two of its own concepts and half
  of them present, and a ticket that says something still works is not a complete
  outage. Unrecognised wording matches nothing, which surfaces as "severity not
  established" rather than as a wrong label.

Only P1 is indicated. P1 is the level the policy tells an agent to escalate
immediately, so it is the one where missing it costs the most; the P2 and P3
definitions are phrased as contrasts ("but core operations remain possible") that
a word match cannot read safely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.backend.models.documents import Evidence
from app.backend.models.policy import Severity

# "P1 - Critical: Complete production outage ..." — a definition, as opposed to a
# response target ("P1: 15 minutes"), which has no dash-name before its colon.
_DEFINITION = re.compile(r"\bP([123])\s*[-–—]\s*[A-Za-z][A-Za-z ]{0,20}:\s*")
_BULLETS = re.compile(r"[●•​·]+")

_CLAUSE_SPLIT = re.compile(r",\s*or\s+|,\s+|\s+or\s+", re.IGNORECASE)

_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bapi[\s-]*keys?\b"), " credential "),
    (re.compile(r"\b(?:access|auth|secret|private|login|ssh)[\s-]*(?:keys?|tokens?)\b"), " credential "),
    (re.compile(r"\bhttp\s*[45]\d\d\b|\b[45]\d\d\s+errors?\b"), " failure "),
    (re.compile(r"\bnot\s+working\b|\bdoes(?:n't| not)\s+work\b"), " failure "),
)

_STOPWORDS = frozenset(
    "a an the for of to in on at and or with is are was were be been being by as "
    "it its this that these those than when from into our their they them we you "
    "no not but if so can may will would could should has have had do does did".split()
)

# Words that mean the same thing in any support ticket. Deliberately short.
_CONCEPTS: dict[str, tuple[str, ...]] = {
    "FAILURE": (
        "fail", "fails", "failing", "failed", "failure", "failures", "error", "errors",
        "down", "outage", "unavailable", "unable", "cannot", "cant", "block", "blocked",
        "prevent", "preventing", "broken", "crash", "crashing",
    ),
    "ALL": ("all", "every", "any", "entire", "whole", "everyone", "nobody"),
    "SUSPECT": ("suspect", "suspected", "possible", "potential", "probable", "alleged"),
    "CREDENTIAL": ("credential", "credentials", "password", "passwords", "token", "tokens", "secret", "secrets"),
    "EXPOSURE": ("exposure", "exposed", "expose", "leak", "leaked", "leaks", "compromise", "compromised"),
    "INCIDENT": ("incident", "incidents", "breach", "breached", "attack", "intrusion"),
}

_SUFFIXES = ("ings", "ing", "ions", "ion", "ed", "es", "s")

# A ticket that says the affected thing still works is not a *complete* outage.
_ALTERNATIVE = re.compile(
    r"\bstill\s+(?:works?|working|possible)\b"
    r"|\bworks?\s+(?:fine|normally|as\s+expected)\b"
    r"|\bworkaround\b"
    r"|\bone[\s-]by[\s-]one\b",
    re.IGNORECASE,
)
_NO_WORKAROUND = re.compile(
    r"\bno\s+(?:known\s+)?workaround\b|\bwithout\s+a\s+workaround\b|\bnot?\s+any\s+workaround\b",
    re.IGNORECASE,
)


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


_CANON: dict[str, str] = {
    _stem(word): concept for concept, words in _CONCEPTS.items() for word in words
}


def _terms(text: str) -> dict[str, str]:
    """Concept-normalised terms in `text`, each with the word it first came from."""
    lowered = text.lower()
    for pattern, replacement in _PHRASES:
        lowered = pattern.sub(replacement, lowered)
    found: dict[str, str] = {}
    for word in re.findall(r"[a-z0-9]+", lowered):
        if word in _STOPWORDS or len(word) < 2:
            continue
        stem = _stem(word)
        term = _CANON.get(stem, stem)
        found.setdefault(term, word)
    return found


@dataclass(frozen=True)
class Criterion:
    severity: Severity
    text: str
    evidence: Evidence


@dataclass(frozen=True)
class Match:
    criterion: Criterion
    matched_terms: tuple[str, ...]


def extract_definition_criteria(evidence: list[Evidence]) -> list[Criterion]:
    """The clauses of each severity definition, from the policy's own text.

    Agreement-scoped evidence is skipped: a customer agreement states targets,
    not what counts as critical.
    """
    criteria: list[Criterion] = []
    for item in evidence:
        if item.account_id is not None:
            continue
        text = _BULLETS.sub(" ", item.text)
        matches = list(_DEFINITION.finditer(text))
        for index, match in enumerate(matches):
            severity = Severity.parse(f"P{match.group(1)}")
            if severity is None:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = " ".join(text[match.end() : end].split()).rstrip(" .;")
            for clause in _CLAUSE_SPLIT.split(body):
                clause = clause.strip(" ,;")
                if len(clause.split()) >= 2:
                    criteria.append(Criterion(severity, clause, item))
    return criteria


def indicate(
    ticket_text: str,
    criteria: list[Criterion],
    *,
    severities: tuple[Severity, ...] = (Severity.P1,),
) -> list[Match]:
    """The best-matching clause for each requested severity, if any clause matches."""
    if not ticket_text or not ticket_text.strip():
        return []
    ticket_terms = _terms(ticket_text)
    alternative = bool(_ALTERNATIVE.search(_NO_WORKAROUND.sub(" ", ticket_text)))

    best: dict[Severity, tuple[float, Match]] = {}
    for criterion in criteria:
        if criterion.severity not in severities:
            continue
        wanted = _terms(criterion.text)
        if not wanted:
            continue
        # "Complete outage preventing all ..." and "no workaround" are claims that
        # nothing else works; a ticket saying something does is evidence against.
        if alternative and ("ALL" in wanted or "workaround" in wanted):
            continue
        shared = sorted(set(wanted) & set(ticket_terms))
        coverage = len(shared) / len(wanted)
        if len(shared) < 2 or coverage < 0.5:
            continue
        match = Match(criterion, tuple(ticket_terms[term] for term in shared))
        if criterion.severity not in best or coverage > best[criterion.severity][0]:
            best[criterion.severity] = (coverage, match)
    return [match for _, match in best.values()]
