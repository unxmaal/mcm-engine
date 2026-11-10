"""PreToolUse lesson injection (issue #210).

Builds a query from the pending action itself (program, subcommand, flags, path
basenames, module names), retrieves RULE/NEGATIVE hits with the server's
OR-ranked natural-mode search, re-scores them against the action's own terms,
and returns the top 1-3 as one advisory block. Fail-open: any error, timeout or
missing server yields nothing.
"""
from __future__ import annotations

import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ENABLE_ENV = "MCM_LESSONS"
BUDGET_ENV = "MCM_LESSONS_BUDGET_S"
DEFAULT_BUDGET_S = 0.6
MAX_INJECT = 3
SEARCH_LIMIT = 4
DEDUP_WINDOW_CALLS = 50
MIN_SCORE = 2.0
MIN_TERM_SCORE = 1.5  # flags only add to hits that already share the action's words
MAX_QUERY_TERMS = 6
FIX_MAX_CHARS = 220

LESSON_TOOLS = frozenset({"bash", "edit", "write", "multiedit"})

# Programs too common to identify a lesson on their own and that take no subcommand.
GENERIC_PROGRAMS = frozenset({
    "ls", "cd", "cat", "echo", "printf", "grep", "rg", "egrep", "head", "tail",
    "sed", "awk", "find", "mkdir", "rm", "cp", "mv", "touch", "chmod", "wc",
    "sort", "uniq", "xargs", "env", "export", "source", "test", "true", "false",
    "which", "pwd", "date", "kill", "tee", "cut", "tr", "diff", "less",
    "sh", "bash", "zsh", "python", "python3", "jq", "set", "unset", "read",
    "basename", "dirname", "realpath", "stat", "file", "du", "df", "ps", "open",
})
TWO_LEVEL = frozenset({"gh", "docker", "kubectl", "aws", "gcloud", "brew"})
# Subcommands too common to carry a lesson (read-only inspection).
GENERIC_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "fetch", "branch", "rev-parse", "view",
    "list", "help", "version", "run", "info", "ls", "get",
})
GENERIC_TERMS = GENERIC_PROGRAMS | GENERIC_SUBCOMMANDS | frozenset({
    "git", "uv", "gh", "src", "tests", "test", "tmp", "lib", "bin", "main",
    "init", "index", "utils", "util", "config", "readme", "make", "users",
    "home", "projects", "github", "pr", "issue",
})
_SHELL_PREFIX = frozenset({
    "while", "until", "if", "then", "else", "elif", "do", "!", "time", "sudo",
    "exec", "nohup", "command", "builtin", "{", "}", "for", "in", "case",
})
_SHELL_END = frozenset({"done", "fi", "esac", "}"})
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|&\n()`]|\$\(")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,40}$")
_SUB_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
_PATHISH_RE = re.compile(r"/|\.[A-Za-z0-9]{1,5}$")
_HEADER_RE = re.compile(r"^\[(RULE|NEGATIVE) #(\d+)\]\s*(.*)$")
_SCOPES = ("rules", "negative")


@dataclass
class ActionTerms:
    terms: list[str] = field(default_factory=list)  # ordered by priority, deduped
    strong: set[str] = field(default_factory=set)  # identifier-shaped path stems
    flags: set[str] = field(default_factory=set)

    def add(self, term: str) -> None:
        t = term.lower().strip()
        if len(t) >= 2 and t not in self.terms:
            self.terms.append(t)

    @property
    def query(self) -> str:
        return " ".join(self.terms[:MAX_QUERY_TERMS])


@dataclass
class Lesson:
    kind: str
    id: int
    title: str
    fix: str
    score: float
    order: int

    @property
    def key(self) -> str:
        return f"{self.kind.lower()}:{self.id}"


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "1").strip().lower() not in {"0", "false", "off", "no"}


def budget_s() -> float:
    try:
        return max(0.05, float(os.environ.get(BUDGET_ENV, DEFAULT_BUDGET_S)))
    except ValueError:
        return DEFAULT_BUDGET_S


_HOME_PARTS = frozenset(p.lower() for p in Path.home().parts if p not in ("/", ""))


def _add_path(at: ActionTerms, raw: str) -> None:
    p = Path(raw.strip("'\""))
    stem = p.stem if p.suffix else p.name
    if not stem or stem.startswith("-") or stem.lower() in _HOME_PARTS:
        return
    if len(stem) >= 6 and re.search(r"[_-]", stem):
        at.strong.add(stem.lower())
    at.add(stem)
    parent = p.parent.name
    if (parent and parent not in (".", "..") and parent.lower() not in GENERIC_TERMS
            and parent.lower() not in _HOME_PARTS):
        at.add(parent)


def _add_module(at: ActionTerms, dotted: str) -> None:
    for part in dotted.split("."):
        if len(part) >= 3:
            at.add(part)


def _segment_terms(at: ActionTerms, segment: str) -> None:
    try:
        toks = shlex.split(segment, comments=True)
    except ValueError:
        toks = segment.split()
    while toks and (toks[0] in _SHELL_PREFIX or _ASSIGN_RE.match(toks[0])):
        toks.pop(0)
    toks = [t for t in toks if t not in _SHELL_END]
    if not toks:
        return
    prog = Path(toks[0]).name.lower()
    if not _WORD_RE.match(prog):
        return
    at.add(prog)
    subs_left = 0 if prog in GENERIC_PROGRAMS else (2 if prog in TWO_LEVEL else 1)
    args = toks[1:]
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith("-"):
            name = a.split("=", 1)[0]
            at.flags.add(name)
            if name == "-m" and i < len(args) and any(
                    w.startswith("python") for w in [prog, *args[:i - 1]]):
                _add_module(at, args[i])
                i += 1
            continue
        if a.startswith(">") or a.startswith("<") or a.startswith("/dev/"):
            continue
        if subs_left and _SUB_RE.match(a):
            at.add(a)
            subs_left -= 1
            continue
        subs_left = 0
        if _PATHISH_RE.search(a) and " " not in a:
            _add_path(at, a)
        elif " " in a:
            # A quoted pattern (pgrep -f 'soh discover --loop'): keep its flags.
            for w in a.split():
                if w.startswith("-"):
                    at.flags.add(w.split("=", 1)[0])


def action_terms(tool_name: str, tool_input: dict) -> ActionTerms:
    at = ActionTerms()
    tool = (tool_name or "").lower()
    if tool == "bash":
        cmd = str(tool_input.get("command") or "")
        for seg in _SEGMENT_SPLIT.split(cmd):
            if seg.strip():
                _segment_terms(at, seg)
    elif tool in LESSON_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        if path:
            _add_path(at, str(path))
            mod = re.search(r"(?:^|/)src/(.+)\.py$", str(path))
            if mod:
                _add_module(at, mod.group(1).replace("/", "."))
    # Order: distinctive terms first so the query cap drops generic ones.
    at.terms.sort(key=lambda t: t in GENERIC_TERMS)
    return at


def parse_hits(text: str) -> list[tuple[str, int, str, str]]:
    """(kind, id, title, body) per hit from the search tool's summary render."""
    hits: list[tuple[str, int, str, str]] = []
    cur: Optional[list] = None
    for line in (text or "").splitlines():
        m = _HEADER_RE.match(line.strip())
        if m:
            if cur:
                hits.append((cur[0], cur[1], cur[2], "\n".join(cur[3])))
            cur = [m.group(1), int(m.group(2)), m.group(3).strip(), []]
        elif cur is not None and line.strip():
            if line.lstrip().startswith("(summary view"):
                continue
            cur[3].append(line.strip())
    if cur:
        hits.append((cur[0], cur[1], cur[2], "\n".join(cur[3])))
    return hits


def _term_hit(term: str, text: str) -> bool:
    pat = re.escape(term)
    if len(term) >= 4:
        return re.search(rf"(?<![A-Za-z0-9]){pat}", text, re.I) is not None
    return re.search(rf"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])", text, re.I) is not None


def _flag_hit(flag: str, text: str) -> bool:
    return re.search(rf"(?<!\S){re.escape(flag)}(?![\w-])", text) is not None


def score_hit(at: ActionTerms, title: str, body: str) -> float:
    """Weighted count of the action's terms in a hit. A term in the title
    counts 1.0, in the match snippet or Fix line 0.75; a generic term half
    that; an identifier-shaped path stem 2.0 anywhere in the summary. Zero
    unless a non-generic term is in the title (or a path stem matched): a
    lesson that only mentions the command in passing is not about it."""
    snippet = _scoring_text(body)
    score = 0.0
    anchored = False
    for t in at.terms:
        if t in at.strong and _term_hit(t, f"{title}\n{body}"):
            score += 2.0
            anchored = True
            continue
        if _term_hit(t, title):
            w = 1.0
            anchored = anchored or t not in GENERIC_TERMS
        elif _term_hit(t, snippet):
            w = 0.75
        else:
            continue
        score += w * (0.5 if t in GENERIC_TERMS else 1.0)
    if not anchored or score < MIN_TERM_SCORE:
        return 0.0
    text = f"{title}\n{snippet}"
    score += 0.5 * sum(1 for f in at.flags if _flag_hit(f, text))
    return score


def _one_line_fix(kind: str, body: str) -> str:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    content = [ln for ln in lines if not ln.startswith("Match:")]
    fix = ""
    for idx, s in enumerate(content):
        if kind == "NEGATIVE" and s.startswith("Fix:"):
            fix = s[4:]
            break
        m = re.search(r"\bFIX:\s*(.+)", s)
        if m:
            fix = m.group(1)
            # The summary render cuts a rule body mid-text; its last line may be partial.
            if kind == "RULE" and idx == len(content) - 1:
                fix = fix.rsplit(" ", 1)[0] + " ..."
            break
    fix = re.sub(r"…?\s*\(get_entry\([^)]*\) for full\)\s*$", "", fix).strip()
    if len(fix) > FIX_MAX_CHARS:
        fix = fix[:FIX_MAX_CHARS].rsplit(" ", 1)[0] + " ..."
    return fix


def _scoring_text(body: str) -> str:
    """Match snippet and Fix line only: a long body matches anything by volume."""
    keep = [ln for ln in body.splitlines()
            if ln.startswith("Match:") or ln.startswith("Fix:")]
    return "\n".join(keep)


def rank(at: ActionTerms, texts: list[str]) -> list[Lesson]:
    seen: set[str] = set()
    out: list[Lesson] = []
    order = 0
    for text in texts:
        for kind, eid, title, body in parse_hits(text):
            order += 1
            key = f"{kind.lower()}:{eid}"
            if key in seen:
                continue
            seen.add(key)
            sc = score_hit(at, title, body)
            if sc >= MIN_SCORE:
                out.append(Lesson(kind, eid, title, _one_line_fix(kind, body), sc, order))
    out.sort(key=lambda l: (-l.score, l.order))
    return out


def render(lessons: list[Lesson]) -> str:
    lines = ["[mcm-engine] Lessons matching this action (verify relevance):"]
    for l in lessons:
        title = re.sub(r"\s*\([^)]*\)\s*$", "", l.title) if l.kind == "RULE" else l.title
        if len(title) > 200:
            title = title[:200].rsplit(" ", 1)[0] + " ..."
        line = f"- {l.kind} #{l.id}: {title}"
        if l.fix:
            line += f" | Fix: {l.fix}"
        line += f" (get_entry('{l.kind.lower() if l.kind == 'NEGATIVE' else 'rule'}', {l.id}))"
        lines.append(line)
    return "\n".join(lines)


def http_fetch(query: str, cwd: Path, timeout: float) -> list[str]:
    """Search rules and negatives in one MCP session, concurrently."""
    from .mcp_enforcement import _mcp_http_endpoint

    url, headers = _mcp_http_endpoint(cwd)
    if not url:
        return []
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _run() -> list[str]:
        async with streamablehttp_client(url, headers=headers or None) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                results = await asyncio.gather(*[
                    session.call_tool("search", {
                        "query": query, "scope": scope,
                        "limit": SEARCH_LIMIT, "detail": "summary",
                    })
                    for scope in _SCOPES
                ])
                return ["\n".join(b.text for b in (res.content or [])
                                  if getattr(b, "type", None) == "text")
                        for res in results]

    return asyncio.run(asyncio.wait_for(_run(), timeout))


@dataclass
class Outcome:
    query: str
    status: str  # injected | none | dedup | timeout | error | skipped
    ids: list[str]
    ms: int
    text: str = ""


def lessons_for(
    tool_name: str,
    event: dict,
    state: dict,
    cwd: Path,
    *,
    fetch: Optional[Callable[[str, Path, float], list[str]]] = None,
    budget: Optional[float] = None,
) -> Optional[Outcome]:
    """Return an Outcome for an eligible call, None when the call is not
    eligible. Mutates ``state`` (dedup map, call counter). Never raises."""
    if not enabled() or (tool_name or "").lower() not in LESSON_TOOLS:
        return None
    at = action_terms(tool_name, event.get("tool_input") or {})
    if not at.terms:
        return None
    if fetch is None:
        from .mcp_enforcement import _mcp_http_endpoint
        if not _mcp_http_endpoint(cwd)[0]:
            return None
    calls = int(state.get("lesson_calls", 0)) + 1
    state["lesson_calls"] = calls
    budget = budget_s() if budget is None else budget
    run = fetch or http_fetch
    box: dict = {}
    t0 = time.perf_counter()

    def _work():
        try:
            box["r"] = run(at.query, cwd, budget)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(budget)
    ms = int((time.perf_counter() - t0) * 1000)
    if th.is_alive():
        return Outcome(at.query, "timeout", [], ms)
    if "e" in box:
        status = "timeout" if type(box["e"]).__name__ == "TimeoutError" else "error"
        return Outcome(at.query, status, [], ms)
    ranked = rank(at, box.get("r") or [])
    if not ranked:
        return Outcome(at.query, "none", [], ms)
    shown = state.setdefault("lessons_shown", {})
    for k in [k for k, c in shown.items() if calls - c >= DEDUP_WINDOW_CALLS]:
        del shown[k]
    fresh = [l for l in ranked if l.key not in shown][:MAX_INJECT]
    if not fresh:
        return Outcome(at.query, "dedup", [l.key for l in ranked[:MAX_INJECT]], ms)
    for l in fresh:
        shown[l.key] = calls
    return Outcome(at.query, "injected", [l.key for l in fresh], ms, render(fresh))
