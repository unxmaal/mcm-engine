"""Known-bad Bash commands the PreToolUse hook denies outright (issue #208).

Each entry carries the reason and the replacement, both shown to the agent in
the deny. There is no override: the agent can set any env var or marker a tool
call can reach, so the owner edits DENY_TABLE instead.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

_OPS = frozenset({";", "&&", "||", "|", "|&", "&", "(", ")", "((", "))", ";;"})
_WRAPPERS = frozenset({
    "sudo", "env", "rtk", "time", "nohup", "command", "exec", "nice", "builtin",
    "caffeinate", "xargs", "!",
})
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
_FD_DUP_RE = re.compile(r"\d*[<>]&(?:\d+|-)")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_GIT_GLOBAL_WITH_ARG = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_LOOP_OPEN = frozenset({"while", "until", "for", "select"})
_PGREP_F_RE = re.compile(r"\bpgrep\s+(?:-[A-Za-z]+\s+|--\S+\s+)*-[A-Za-z]*f")
_N_AUTO_RE = re.compile(r"(?:^|\s)(?:-n\s*auto|--numprocesses[=\s]auto)\b")
_TEST_MAKE_TARGETS = frozenset({"test", "tests", "check", "ci"})


@dataclass(frozen=True)
class Context:
    cwd: Path
    edited_files: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DenyPattern:
    id: str
    kb: str
    reason: str
    replacement: str
    check: Callable[[list[tuple[list[str], str]], Context], bool]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _strip_heredocs(cmd: str) -> str:
    lines = cmd.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        delims = [m.group(2) for m in _HEREDOC_RE.finditer(line)]
        i += 1
        for d in delims:
            while i < len(lines) and lines[i].strip() != d:
                i += 1
            i += 1
    return "\n".join(out)


def _tokens(cmd: str) -> list[str]:
    text = _strip_heredocs(cmd).replace("\\\n", " ").replace("\n", " ; ")
    text = _FD_DUP_RE.sub(" ", text).replace("&>", ">")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return re.findall(r"&&|\|\||[;&|()]|[^\s;&|()]+", text)


def _segments(tokens: list[str]) -> list[tuple[list[str], str]]:
    """Split tokens into simple commands, each with the operator that follows it."""
    segs: list[tuple[list[str], str]] = []
    cur: list[str] = []
    for t in tokens:
        if t in _OPS:
            segs.append((cur, t))
            cur = []
        else:
            cur.append(t)
    segs.append((cur, ""))
    return [(s, op) for s, op in segs if s or op in ("|", "&&", "||")]


def _head(seg: list[str]) -> list[str]:
    """The segment from its command word on: leading assignments and wrappers
    (sudo, env, rtk, timeout N, ...) dropped."""
    i = 0
    while i < len(seg):
        t = seg[i]
        if _ASSIGN_RE.match(t):
            i += 1
        elif os.path.basename(t) in _WRAPPERS:
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
        elif os.path.basename(t) == "timeout":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
            i += 1
        else:
            break
    return seg[i:]


def _base(tok: str) -> str:
    return os.path.basename(tok)


def _git_sub(seg: list[str]) -> tuple[str, list[str], Optional[str]]:
    """(subcommand, args, -C dir) for a git segment, else ("", [], None)."""
    h = _head(seg)
    if not h or _base(h[0]) != "git":
        return "", [], None
    i, cdir = 1, None
    while i < len(h) and h[i].startswith("-"):
        if h[i] in _GIT_GLOBAL_WITH_ARG:
            if h[i] == "-C" and i + 1 < len(h):
                cdir = h[i + 1]
            i += 2
        else:
            i += 1
    if i >= len(h):
        return "", [], cdir
    return h[i], h[i + 1:], cdir


def _is_test_runner(seg: list[str]) -> bool:
    h = _head(seg)
    if not h:
        return False
    names = [_base(t) for t in h]
    if any(n in ("pytest", "py.test") for n in names):
        return True
    first = names[0]
    if first in ("make", "gmake"):
        return any(t in _TEST_MAKE_TARGETS for t in h[1:])
    if first in ("tox", "nox"):
        return True
    if first in ("npm", "yarn", "pnpm", "go", "cargo"):
        return len(h) > 1 and h[1] == "test"
    return False


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _pgrep_wait_loop(segs, ctx) -> bool:
    flat: list[str] = []
    for seg, op in segs:
        flat.extend(seg)
        flat.append(op or ";")
    depth = 0
    for i, t in enumerate(flat):
        if t in _LOOP_OPEN:
            depth += 1
        elif t == "done" and depth:
            depth -= 1
        elif depth and t == "pgrep":
            j = i + 1
            while j < len(flat) and flat[j] not in _OPS:
                if re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", flat[j]):
                    return True
                j += 1
        elif depth and _PGREP_F_RE.search(t):
            return True
    return False


def _git_add_all(segs, ctx) -> bool:
    for seg, _ in segs:
        sub, args, _ = _git_sub(seg)
        if sub != "add":
            continue
        for a in args:
            if a == "--":
                continue
            if a in (".", "./", "--all", "-A", ":/", ":/."):
                return True
            if re.fullmatch(r"-[a-zA-Z]*A[a-zA-Z]*", a):
                return True
    return False


def _piped_test_then_commit(segs, ctx) -> bool:
    pipefail = False
    for i, (seg, op) in enumerate(segs):
        h = _head(seg)
        if h and h[0] == "set" and "pipefail" in h:
            pipefail = True
        if pipefail or op != "|" or not _is_test_runner(seg):
            continue
        j = i
        while j < len(segs) and segs[j][1] in ("|", "|&"):
            j += 1
        if j < len(segs) and segs[j][1] == "&&":
            for later, _ in segs[j + 1:]:
                if _git_sub(later)[0] == "commit":
                    return True
    return False


def _launchctl_harness(segs, ctx) -> bool:
    for seg, _ in segs:
        h = _head(seg)
        if len(h) < 2 or _base(h[0]) != "launchctl":
            continue
        if h[1] in ("disable", "unload", "bootout") and any(
                "com.unxmaal.localharness" in t for t in h[2:]):
            return True
    return False


def _pytest_n_auto(segs, ctx) -> bool:
    for seg, _ in segs:
        if not _is_test_runner(seg):
            continue
        h = _head(seg)
        for k, t in enumerate(h):
            if t in ("-n", "--numprocesses") and k + 1 < len(h) and h[k + 1] == "auto":
                return True
            if t in ("-nauto", "--numprocesses=auto") or _N_AUTO_RE.search(t.split("=", 1)[-1]):
                return True
    return False


def _discard_paths(sub: str, args: list[str]) -> list[str]:
    if sub == "checkout":
        if "--" in args:
            return args[args.index("--") + 1:]
        return [a for a in args if a in (".", "./")]
    if sub == "restore":
        if "--staged" in args or "-S" in args:
            if "--worktree" not in args and "-W" not in args:
                return []
        paths, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in ("-s", "--source"):
                skip = True
            elif a == "--" or a.startswith("-"):
                continue
            else:
                paths.append(a)
        return paths
    return []


def _dirty_files(cwd: Path, paths: list[str]) -> list[str]:
    """Absolute paths of tracked files under ``paths`` with uncommitted changes.
    Any git failure yields [] (fail-open)."""
    try:
        top = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=1.0,
        )
        if top.returncode != 0:
            return []
        st = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain=v1", "-z", "--", *paths],
            capture_output=True, text=True, timeout=1.0,
        )
        if st.returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []
    root = Path(top.stdout.strip())
    out = []
    for entry in st.stdout.split("\0"):
        if len(entry) > 3 and entry[:2] not in ("??", "!!"):
            out.append(str((root / entry[3:]).resolve()))
    return out


def _discard_unowned_changes(segs, ctx) -> bool:
    cwd = ctx.cwd
    owned = {str(Path(p).resolve()) for p in ctx.edited_files}
    for seg, _ in segs:
        h = _head(seg)
        if len(h) >= 2 and h[0] == "cd":
            cwd = (cwd / os.path.expanduser(h[1])).resolve()
            continue
        sub, args, cdir = _git_sub(seg)
        paths = _discard_paths(sub, args)
        if not paths:
            continue
        where = (cwd / os.path.expanduser(cdir)) if cdir else cwd
        if any(f not in owned for f in _dirty_files(where, paths)):
            return True
    return False


def _gh_pr_merge(segs, ctx) -> bool:
    for seg, _ in segs:
        h = _head(seg)
        if len(h) >= 3 and _base(h[0]) == "gh" and h[1] == "pr" and h[2] == "merge":
            if "--auto" not in h and "--disable-auto" not in h:
                return True
    return False


DENY_TABLE: tuple[DenyPattern, ...] = (
    DenyPattern(
        "pgrep-wait-loop", "RULE #906",
        "a loop waiting on `pgrep -f` matches its own command line and never exits",
        "wait on a sentinel file, a captured PID (`kill -0 $pid`), or use run_in_background",
        _pgrep_wait_loop,
    ),
    DenyPattern(
        "git-add-all", "NEGATIVE #7, #31, #40",
        "`git add -A` / `git add .` / `git add --all` sweeps in untracked files",
        "stage the named files: `git add path/one path/two`",
        _git_add_all,
    ),
    DenyPattern(
        "piped-test-then-commit", "NEGATIVE #41",
        "piping tests into another command hides their exit status, so `&& git commit` commits on red",
        "`set -o pipefail` first, or run the tests, check the exit code, then commit",
        _piped_test_then_commit,
    ),
    DenyPattern(
        "launchctl-localharness", "NEGATIVE #51",
        "`launchctl disable/unload/bootout` of a com.unxmaal.localharness unit takes the harness down behind its own back",
        "use the harness hold/pause verbs",
        _launchctl_harness,
    ),
    DenyPattern(
        "pytest-n-auto", "NEGATIVE #53",
        "`-n auto` starts one worker per core and starves the machine",
        "use `-n 6`",
        _pytest_n_auto,
    ),
    DenyPattern(
        "discard-unowned-changes", "NEGATIVE #33, #46, #47",
        "`git checkout -- <file>` / `git restore <file>` would discard uncommitted changes this agent did not make",
        "save them first: `git stash push -m <tag> -- <file>` or `git diff -- <file> > patch`",
        _discard_unowned_changes,
    ),
    DenyPattern(
        "gh-pr-merge", "owner rule",
        "agents never merge pull requests",
        "arm it instead: `gh pr merge <n> --auto --squash`",
        _gh_pr_merge,
    ),
)


def _nested_scripts(segs) -> Iterable[str]:
    for seg, _ in segs:
        h = _head(seg)
        if not h or _base(h[0]) not in _SHELLS:
            continue
        for k, t in enumerate(h[1:-1], start=1):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", t):
                yield h[k + 1]
                break


def find_denial(
    command: str, ctx: Context, table: Iterable[DenyPattern] = DENY_TABLE,
    _depth: int = 0,
) -> Optional[DenyPattern]:
    """The first pattern ``command`` matches, or None. Recurses into `bash -c`."""
    table = tuple(table)
    segs = _segments(_tokens(command))
    for p in table:
        if p.check(segs, ctx):
            return p
    if _depth < 3:
        for script in _nested_scripts(segs):
            hit = find_denial(script, ctx, table, _depth + 1)
            if hit:
                return hit
    return None


def deny_message(p: DenyPattern) -> str:
    return (
        f"[mcm-engine] DENIED ({p.id}, {p.kb}): {p.reason}. "
        f"Instead: {p.replacement}."
    )
