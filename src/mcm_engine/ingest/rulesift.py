"""Mechanical rule-sift funnel — turn a raw candidate stream into a small
set of net-new, rule-shaped candidates WITHOUT a model (Slice 1).

The engine is model-free by design (see ``dedup.py``/``sanitize.py``). This
module keeps it that way for the cheap stages: every filter here is
deterministic regex + the existing MinHash machinery. Only what survives
this funnel is ever worth spending a cheap model on (Slice 2+).

Funnel:
  A. ``extract_spans``   pull rule-shaped spans (comment blocks, docstrings)
                         from raw code; treat already-curated prose (markdown,
                         extracted docstrings) as a single span.
  B. ``is_rule_like``    normative-language gate — a span has to read like a
                         rule (must/never/gotcha/...), not like plain code or
                         API boilerplate.
  C. ``classify_novelty`` band each surviving span against the existing rule
                         corpus via MinHash: KNOWN (drop), REFINE (same
                         subject, escalate later), NOVEL (genuinely new).
  D. ``sift``            run A->C over a candidate stream, then collapse
                         intra-run near-duplicates so the same gotcha copied
                         across files yields one survivor.

``sift`` is pure: it takes the ingester rows and the existing-rule corpus and
returns ``RuleCandidate`` objects. Reading the corpus from storage is
``load_existing_rules`` (thin, so the funnel stays unit-testable).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from ..backends import KnowledgeRow
from .. import dedup


# --- Novelty bands ----------------------------------------------------------

class Band(str, Enum):
    KNOWN = "known"     # near-identical to an existing rule — drop / reinforce
    REFINE = "refine"   # same subject, body diverges — a refinement candidate
    NOVEL = "novel"     # no close existing rule — genuinely new


# Thresholds mirror dedup.py's semantics: >=0.9 is the near-duplicate line it
# already uses for rule merges; 0.5 is its "same subject" topic threshold.
KNOWN_THRESHOLD = 0.9
NOVEL_THRESHOLD = 0.5

# A span has to carry at least this many normalized tokens to be worth judging
# (drops "# TODO", "# fixme", bare markers).
MIN_TOKENS = 3


def band_for(sim: float) -> Band:
    """Map a similarity score to a novelty band (boundaries inclusive upward)."""
    if sim >= KNOWN_THRESHOLD:
        return Band.KNOWN
    if sim >= NOVEL_THRESHOLD:
        return Band.REFINE
    return Band.NOVEL


# --- A. span extraction -----------------------------------------------------

# Prose ingesters hand us curated text already; only raw code needs scanning.
_LINE_COMMENT_RE = re.compile(r"^\s*(?:#|//|--)\s?(.*)$")
_BLOCK_COMMENT_RE = re.compile(r"/\*(.*?)\*/", re.DOTALL)
_TRIPLE_RE = re.compile(r"(?:\"\"\"|''')(.*?)(?:\"\"\"|''')", re.DOTALL)


def extract_spans(text: str, *, raw_code: bool) -> list[str]:
    """Pull candidate rule spans from ``text``.

    ``raw_code=False``: the text is already curated prose (a markdown note, an
    extracted docstring) — return it as a single span (empty -> nothing).

    ``raw_code=True``: scan source for the places rules actually live — block
    comments, triple-quoted docstrings, and runs of consecutive line comments
    (merged into one span so a multi-line warning stays whole). Code bodies are
    dropped entirely. Line comments must start the line (after whitespace) so a
    mid-line ``//`` inside a URL or string isn't mistaken for a comment.
    """
    if not text.strip():
        return []
    if not raw_code:
        return [text.strip()]

    spans: list[str] = []

    for m in _BLOCK_COMMENT_RE.finditer(text):
        s = _clean_block_comment(m.group(1))
        if s:
            spans.append(s)
    for m in _TRIPLE_RE.finditer(text):
        s = m.group(1).strip()
        if s:
            spans.append(s)

    # Runs of consecutive line comments merge into one span.
    run: list[str] = []
    for line in text.split("\n"):
        m = _LINE_COMMENT_RE.match(line)
        if m:
            run.append(m.group(1).rstrip())
        elif run:
            spans.append(" ".join(run).strip())
            run = []
    if run:
        spans.append(" ".join(run).strip())

    return [s for s in spans if s]


def _clean_block_comment(body: str) -> str:
    """Strip the leading ``*`` decoration common in ``/* ... */`` blocks."""
    lines = [re.sub(r"^\s*\*\s?", "", ln).strip() for ln in body.split("\n")]
    return " ".join(ln for ln in lines if ln).strip()


# --- B. rule-likeness gate --------------------------------------------------

# High-signal normative / warning language. Deliberately excludes weak words
# like "should"/"only" that pepper ordinary prose — over-admitting here just
# means more spans reach the (future) model stage, but for Slice 1 the agent
# eyeballs survivors, so we keep the gate tight.
_NORMATIVE_RE = re.compile(
    r"\b("
    r"must|never|always|avoid|ensure|require[sd]?|"
    r"do ?n['’]?t|does ?n['’]?t|won['’]?t|cannot|can['’]?t|"
    r"note|warning|caution|gotcha|hacks?|workarounds?|fixme|"
    r"careful|important|remember|make sure|be sure|watch out|"
    r"needs? to|has to|have to"
    r")\b",
    re.IGNORECASE,
)


def is_rule_like(text: str) -> bool:
    """True if ``text`` reads like a rule: enough content AND at least one
    normative/warning marker. Plain code descriptions and API-doc boilerplate
    ("Args:/Returns:") carry no marker and are dropped."""
    if len(dedup.normalize(text).split()) < MIN_TOKENS:
        return False
    return _NORMATIVE_RE.search(text) is not None


# --- C. novelty banding -----------------------------------------------------


def classify_novelty(
    text: str, existing: Iterable[tuple[int, str]]
) -> tuple[Band, Optional[int]]:
    """Band ``text`` against the existing rule corpus.

    ``existing`` is ``(rule_id, rule_text)`` pairs. Returns ``(band, rule_id)``
    where ``rule_id`` is the best match for KNOWN/REFINE (what to reinforce or
    refine) and ``None`` for NOVEL (nothing close enough to name).
    """
    best_id: Optional[int] = None
    best_sim = 0.0
    for rid, rtext in existing:
        sim = dedup.jaccard_estimate(text, rtext)
        if sim > best_sim:
            best_sim, best_id = sim, rid
    band = band_for(best_sim)
    return band, (best_id if band is not Band.NOVEL else None)


# --- D. the funnel ----------------------------------------------------------


@dataclass
class RuleCandidate:
    """A net-new (or refining) rule-shaped span that survived the funnel."""

    text: str
    source_topic: str            # ingester topic it came from (file / func)
    band: Band
    matched_rule_id: Optional[int] = None   # set for REFINE (the rule to refine)


def sift(
    rows: Iterable[KnowledgeRow],
    existing_rules: Iterable[tuple[int, str]],
    *,
    raw_code: bool,
) -> list[RuleCandidate]:
    """Run the mechanical funnel over an ingester's candidate rows.

    ``raw_code`` selects extraction mode (True for the text-dir catch-all which
    yields whole source files; False for curated ingesters like markdown-dir /
    python-ast). Returns rule-shaped, non-KNOWN survivors with intra-run
    near-duplicates collapsed.
    """
    existing = list(existing_rules)
    survivors: list[RuleCandidate] = []
    for row in rows:
        detail = getattr(row, "detail", "") or ""
        for span in extract_spans(detail, raw_code=raw_code):
            if not is_rule_like(span):
                continue
            band, matched = classify_novelty(span, existing)
            if band is Band.KNOWN:
                continue
            survivors.append(RuleCandidate(
                text=span,
                source_topic=getattr(row, "topic", ""),
                band=band,
                matched_rule_id=matched,
            ))
    return _collapse_duplicates(survivors)


def _collapse_duplicates(survivors: list[RuleCandidate]) -> list[RuleCandidate]:
    """Drop intra-run near-duplicate spans, keeping the first of each cluster
    (order preserved). Reuses dedup's deterministic MinHash clustering."""
    if len(survivors) < 2:
        return survivors
    clusters = dedup.find_near_duplicates(
        [(i, c.text) for i, c in enumerate(survivors)], threshold=KNOWN_THRESHOLD
    )
    drop: set[int] = set()
    for cluster in clusters:
        drop.update(cluster[1:])
    return [c for i, c in enumerate(survivors) if i not in drop]


def load_existing_rules(storage) -> list[tuple[int, str]]:
    """Read the ACTIVE rule corpus from storage as ``(id, text)`` pairs, where
    text is ``title + keywords + content`` — the same projection consolidate.py
    dedups over. Archived/superseded rules are excluded."""
    from ..backends import EntityType

    out: list[tuple[int, str]] = []
    for r in storage.iter_entries(EntityType.RULE):
        if getattr(r, "archived", False):
            continue
        if getattr(r, "status", "active") == "superseded":
            continue
        text = " ".join(part for part in (
            getattr(r, "title", "") or "",
            getattr(r, "keywords", "") or "",
            getattr(r, "content", "") or "",
        ) if part).strip()
        out.append((r.id, text))
    return out
