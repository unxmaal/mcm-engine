"""Verified and unverified claims carried across a handoff or compaction (issue #206)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

NO_EVIDENCE_CHECK = "marked verified with no evidence; find its source and re-read it"
DEFAULT_CHECK = "find its source and re-read it"


def _items(value: Any) -> list:
    """Accept a list, a JSON string of a list, a single item, or nothing."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    if isinstance(value, dict):
        return [value]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def normalize(verified: Any = None, unverified: Any = None) -> dict:
    """Return {"verified": [...], "unverified": [...]}, demoting evidence-less verified claims."""
    out: dict = {"verified": [], "unverified": []}
    for item in _items(verified):
        d = item if isinstance(item, dict) else {"claim": item}
        claim, evidence = _text(d.get("claim")), _text(d.get("evidence"))
        if not claim:
            continue
        if not evidence:
            out["unverified"].append({"claim": claim, "check": NO_EVIDENCE_CHECK})
            continue
        entry = {"claim": claim, "evidence": evidence}
        if _text(d.get("at")):
            entry["at"] = _text(d.get("at"))
        out["verified"].append(entry)
    for item in _items(unverified):
        d = item if isinstance(item, dict) else {"claim": item}
        claim = _text(d.get("claim"))
        if claim:
            out["unverified"].append({"claim": claim, "check": _text(d.get("check")) or DEFAULT_CHECK})
    return out


def encode(claims: dict) -> Optional[str]:
    """Serialize for storage; None when both lists are empty."""
    if not claims.get("verified") and not claims.get("unverified"):
        return None
    return json.dumps(claims, sort_keys=True)


def decode(raw: Optional[str]) -> dict:
    """Parse a stored claims column; anything unreadable yields empty lists."""
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    return {"verified": list(d.get("verified") or []), "unverified": list(d.get("unverified") or [])}


def _parse_time(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age(then: Optional[datetime], now: datetime) -> str:
    if then is None:
        return "age unknown"
    secs = max(0, int((now - then).total_seconds()))
    if secs < 3600:
        return f"age {secs // 60}m"
    if secs < 86400:
        return f"age {secs // 3600}h"
    return f"age {secs // 86400}d"


def render(claims: dict, recorded_at: Any = None, now: Optional[datetime] = None) -> str:
    """Render UNVERIFIED (with checks) then VERIFIED (with evidence and age); empty when none."""
    if not claims.get("verified") and not claims.get("unverified"):
        return ""
    now = now or datetime.now(timezone.utc)
    lines: list[str] = []
    if claims.get("unverified"):
        lines.append("=== UNVERIFIED: run each check before acting on or repeating the claim ===")
        for c in claims["unverified"]:
            lines.append(f"  - {c.get('claim', '')}")
            lines.append(f"    check: {c.get('check') or DEFAULT_CHECK}")
    if claims.get("verified"):
        lines.append("=== VERIFIED: evidence and age ===")
        for c in claims["verified"]:
            when = _parse_time(c.get("at")) or _parse_time(recorded_at)
            lines.append(f"  - {c.get('claim', '')}")
            lines.append(f"    evidence: {c.get('evidence', '')} ({_age(when, now)})")
    return "\n".join(lines)
