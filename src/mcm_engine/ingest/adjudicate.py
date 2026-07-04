"""Adjudication layer — turn a verdict about a rule candidate into a KB write,
and provide the provider-agnostic harness-delegation seam (Slice 2).

Slice 1 (``rulesift``) narrows a codebase to net-new, rule-shaped candidates.
This module is the decision + write half:

  * ``Verdict`` / ``Action`` — the contract for "what to do with a candidate":
    ADD a new rule, REFINE (supersede) an existing one, REINFORCE an existing
    one, or REJECT. This is the SAME contract a standalone model adjudicator
    (Slice 3) produces, so nothing downstream cares who decided.

  * ``commit_verdicts`` — apply verdicts through the storage interface
    (``insert_rule`` / ``supersede_rule`` / reinforcement counters +
    ``insert_rule_event``). Backend-agnostic (sqlite or postgres), per-verdict
    atomic, and resilient: one bad verdict is counted and skipped, the rest
    commit. This is the single write path both adjudicator kinds feed.

  * ``render_request`` / ``parse_verdicts`` — the HARNESS-DELEGATION adjudicator.
    The engine is model-free and mcm serves non-Anthropic (opencode) users, so
    the default adjudicator is the *calling harness's own model*: the engine
    renders a decision request (candidates + the existing rule bodies to compare
    against + the verdict schema) and parses back the JSON the agent returns.
    Slice 3 swaps this two-phase delegation for an in-process, config-selected
    OpenAI-compatible client — same ``Verdict`` contract, same ``commit_verdicts``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Protocol, runtime_checkable

from ..backends import EntityType, RuleRow
from ..files.watcher import compute_content_hash
from .rulesift import Band, RuleCandidate


class Action(str, Enum):
    ADD = "add"              # brand-new rule
    REFINE = "refine"        # supersede an existing rule with a corrected version
    REINFORCE = "reinforce"  # existing rule is confirmed; bump its signal, no change
    REJECT = "reject"        # not a rule / not worth storing


@dataclass
class Verdict:
    """A decision about one rule candidate. Produced by an adjudicator (the
    calling harness's model, or a standalone model in Slice 3), consumed by
    ``commit_verdicts``."""

    action: Action
    source_topic: str = ""              # provenance: where the span came from
    title: str = ""                     # ADD / REFINE
    keywords: str = ""                  # ADD / REFINE
    category: str = ""                  # ADD / REFINE (optional)
    content: str = ""                   # ADD / REFINE — the rule body
    target_rule_id: Optional[int] = None  # REFINE (rule to supersede) / REINFORCE
    reason: str = ""                    # adjudicator rationale (audit only)

    def validate(self) -> Optional[str]:
        """Return an error string if the verdict is not applyable, else None."""
        if self.action in (Action.ADD, Action.REFINE):
            if not self.title.strip():
                return f"{self.action.value} verdict needs a title"
            if not self.content.strip():
                return f"{self.action.value} verdict needs content"
        if self.action is Action.REFINE and self.target_rule_id is None:
            return "refine verdict needs target_rule_id (the rule to supersede)"
        if self.action is Action.REINFORCE and self.target_rule_id is None:
            return "reinforce verdict needs target_rule_id"
        return None


@runtime_checkable
class Adjudicator(Protocol):
    """A synchronous, in-process adjudicator. The Slice 3 standalone (OpenAI-
    compatible) client satisfies this. The Slice 2 harness path is NOT
    synchronous — it delegates to the calling agent out of process via
    ``render_request`` / ``parse_verdicts`` — so it is not an ``Adjudicator``."""

    def adjudicate(
        self, candidates: list[RuleCandidate], existing: dict[int, str]
    ) -> list[Verdict]:
        ...


# --- commit -----------------------------------------------------------------


@dataclass
class CommitReport:
    created: int = 0
    superseded: int = 0
    reinforced: int = 0
    rejected: int = 0
    errors: int = 0
    details: list[dict] = field(default_factory=list)


def commit_verdicts(
    storage,
    counters,
    verdicts: Iterable[Verdict],
    *,
    actor: str = "ingest",
    source_repo: str = "",
    source_ref: str = "",
    source_commit: str = "",
) -> CommitReport:
    """Apply ``verdicts`` to the KB through the storage/counters interfaces.

    Each verdict is applied in its own ``storage.transaction()`` so it is
    all-or-nothing, and a failing verdict (bad shape, missing target) is counted
    as an error and skipped without aborting the batch — the right resilience
    model for automated ingest. Backend-agnostic: no sqlite/postgres specifics.
    """
    report = CommitReport()
    for v in verdicts:
        err = v.validate()
        if err is not None:
            report.errors += 1
            report.details.append({"action": v.action.value, "error": err,
                                   "source": v.source_topic})
            continue
        try:
            with storage.transaction():
                if v.action is Action.REJECT:
                    report.rejected += 1
                    report.details.append({"action": "reject", "source": v.source_topic})
                elif v.action is Action.ADD:
                    rid = _insert_rule(storage, v, actor, source_repo, source_ref, source_commit)
                    report.created += 1
                    report.details.append({"action": "add", "rule_id": rid, "title": v.title})
                elif v.action is Action.REFINE:
                    old = storage.find_by_id(EntityType.RULE, v.target_rule_id)
                    if old is None:
                        raise ValueError(f"refine target rule {v.target_rule_id} not found")
                    new_id = _insert_rule(storage, v, actor, source_repo, source_ref, source_commit)
                    storage.supersede_rule(v.target_rule_id, new_id, actor)
                    report.created += 1
                    report.superseded += 1
                    report.details.append({"action": "refine", "old_id": v.target_rule_id,
                                           "new_id": new_id})
                elif v.action is Action.REINFORCE:
                    row = storage.find_by_id(EntityType.RULE, v.target_rule_id)
                    if row is None:
                        raise ValueError(f"reinforce target rule {v.target_rule_id} not found")
                    counters.increment(EntityType.RULE, v.target_rule_id, "reinforcement_count")
                    counters.increment(EntityType.RULE, v.target_rule_id, "last_hit_at")
                    storage.insert_rule_event(v.target_rule_id, "reinforced", actor)
                    report.reinforced += 1
                    report.details.append({"action": "reinforce", "rule_id": v.target_rule_id})
        except Exception as e:  # noqa: BLE001 — per-verdict resilience by design
            report.errors += 1
            report.details.append({"action": v.action.value, "error": str(e),
                                   "source": v.source_topic})
    return report


def _insert_rule(storage, v: Verdict, actor, src_repo, src_ref, src_commit) -> int:
    """Insert a rule row + its 'created' provenance event. Mirrors add_rule /
    _apply_import_batch so ingest-created rules are indistinguishable from
    hand-added ones."""
    content_hash = compute_content_hash(v.content)
    rid = storage.insert_rule(RuleRow(
        id=0,
        title=v.title,
        keywords=v.keywords,
        description=v.content[:500],
        category=v.category or None,
        content_hash=content_hash,
        content=v.content,
        created_by=actor,
        updated_by=actor,
    ))
    storage.insert_rule_event(
        rid, "created", actor,
        content_hash=content_hash,
        source_repo=src_repo or None,
        source_ref=src_ref or None,
        source_commit=src_commit or None,
    )
    return rid


# --- harness delegation (provider-agnostic) ---------------------------------

_VERDICT_SCHEMA = """\
Return ONLY a JSON array, one object per candidate above, each shaped:
  {"action": "add|refine|reinforce|reject",
   "source_topic": "<the candidate's source>",
   "title": "<canonical rule title>",        // add/refine
   "keywords": "<comma-separated>",           // add/refine
   "category": "<optional category>",         // add/refine
   "content": "<the rule body as a clear imperative statement>",  // add/refine
   "target_rule_id": <id>,                    // refine (rule to supersede) / reinforce
   "reason": "<one line: why this action>"}
Guidance: `add` a genuinely new rule; `refine` when the candidate corrects/updates
the shown existing rule (it will be superseded); `reinforce` when the existing rule
is already right and the candidate just confirms it; `reject` when it is not a
durable rule.\
"""


def render_request(
    candidates: list[RuleCandidate], existing: dict[int, str]
) -> str:
    """Render an adjudication request for the calling harness's model. For
    REFINE candidates the matched existing rule body (from ``existing``: rule_id
    -> text) is included inline so the model can compare. The response is parsed
    back by ``parse_verdicts``."""
    lines: list[str] = []
    lines.append(
        "# Rule adjudication request\n"
        "Decide what to do with each candidate rule sifted from a codebase.\n"
    )
    for i, c in enumerate(candidates, 1):
        lines.append(f"--- candidate {i} [{c.band.value}] ---")
        lines.append(f"source: {c.source_topic}")
        lines.append("span:")
        lines.append(c.text.rstrip())
        if c.band is Band.REFINE and c.matched_rule_id is not None:
            body = existing.get(c.matched_rule_id, "(existing rule text unavailable)")
            lines.append(f"existing rule {c.matched_rule_id} to compare against:")
            lines.append(body.rstrip())
        lines.append("")
    lines.append(_VERDICT_SCHEMA)
    return "\n".join(lines)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_verdicts(text: str) -> list[Verdict]:
    """Parse the model's response into ``Verdict`` objects. Tolerates a JSON
    array wrapped in ``` fences or surrounded by prose. Raises ``ValueError`` if
    no JSON array can be found or a row is not an object."""
    raw = _extract_json_array(text)
    if raw is None:
        raise ValueError("no JSON array found in adjudication response")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"adjudication response is not valid JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError("adjudication response must be a JSON array")

    verdicts: list[Verdict] = []
    for row in data:
        if not isinstance(row, dict):
            raise ValueError(f"verdict must be an object, got {type(row).__name__}")
        try:
            action = Action(str(row.get("action", "")).lower())
        except ValueError:
            raise ValueError(f"unknown verdict action: {row.get('action')!r}")
        tid = row.get("target_rule_id")
        verdicts.append(Verdict(
            action=action,
            source_topic=str(row.get("source_topic", "")),
            title=str(row.get("title", "")),
            keywords=str(row.get("keywords", "")),
            category=str(row.get("category", "")),
            content=str(row.get("content", "")),
            target_rule_id=int(tid) if tid is not None else None,
            reason=str(row.get("reason", "")),
        ))
    return verdicts


def _extract_json_array(text: str) -> Optional[str]:
    """Pull the JSON array out of ``text`` — whole string, a ``` fence, or the
    first bracket-balanced ``[...]`` span."""
    stripped = text.strip()
    if stripped.startswith("["):
        return stripped
    m = _FENCE_RE.search(text)
    if m and m.group(1).strip().startswith("["):
        return m.group(1).strip()
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None
