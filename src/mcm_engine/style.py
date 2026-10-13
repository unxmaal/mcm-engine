"""House-voice support (issue #63).

Two layers, deliberately split:

- HARD, mechanical: `count_style_tics(text)` counts the tics that CAN be
  regexed — emoji and em-dashes. A count, never a gate (this project's hooks are
  fail-open; you cannot regex your way to "warm", but you can count "no emoji"
  and get a real number instead of eyeballing leakage).
- SOFT, un-mechanizable: `VOICE_EXEMPLAR` is a positive voice target phrased as
  before/after pairs, not a longer denylist. Naming a tic primes the tic, and an
  all-prohibition rule pulls toward flat, clinical prose because flat prose
  violates nothing. An exemplar gives the model something to pattern-match.
"""
from __future__ import annotations

import re

# Em-dash (U+2014) and the em-dash-as-double-hyphen a model reaches for when told
# "no em-dashes". Both are the tic.
_EM_DASH = re.compile(r"—|(?<!-)--(?!-)")

# The main emoji blocks: Misc Symbols & Pictographs, Emoticons, Transport & Map,
# Supplemental Symbols, Dingbats, and the regional/variation selectors that trail
# them. Deliberately a fixed character class — no dependency, pure, testable.
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U00002B00-\U00002BFF"   # arrows/stars commonly used decoratively
    "\U0000FE0F"              # variation selector-16 (emoji presentation)
    "]"
)


def count_style_tics(text: str) -> dict[str, int]:
    """Count the mechanically-detectable house-style tics in `text`.

    Returns {'emoji': n, 'em_dash': n}. Pure and dependency-free so it can be
    run anywhere a metric is wanted (and red-proofed directly)."""
    if not text:
        return {"emoji": 0, "em_dash": 0}
    return {
        "emoji": len(_EMOJI.findall(text)),
        "em_dash": len(_EM_DASH.findall(text)),
    }


# The positive target, re-surfaced at session start to fight instruction decay.
# Terse on purpose: over-nudging produces self-conscious, trying-too-hard prose.
VOICE_EXEMPLAR = (
    "[mcm-engine] House voice: read the situation, take a position, be brief, "
    "say the dry true thing. Directness and judgment read as warmth once the "
    "decorative warmth is gone. Two before/after pairs to pattern-match:\n"
    "  BEFORE: Great question! At its core, the registry is powerful, flexible, "
    "and easy to extend, seamlessly matching the right one. Hope this helps!\n"
    "  AFTER: The registry walks each ingester in registration order and takes "
    "the first that matches. To add one, implement `matches` and `stream`.\n"
    "  BEFORE: This isn't just a bug fix, it's a fundamental rethink that "
    "leverages content hashing for a truly seamless deduplication experience.\n"
    "  AFTER: Title-only dedup missed the same rule pasted under a new title. "
    "Now the body is hashed, so a duplicate is caught whatever it is called."
)
