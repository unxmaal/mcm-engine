"""Gauntlet class 3 / general #10: a text file opened without an explicit
`encoding=` uses the platform default (cp1252 on Windows, locale-dependent
elsewhere), so a config or rule body with non-ASCII round-trips wrong or raises
off the developer's machine. This is a GATE: production code is at zero, so it
stays at zero. `tests/` is deliberately out of scope (lower stakes, and a gate
that also scanned fixtures would be an inventory).

The check is red-proofed below: it must fire on an unencoded text open and must
NOT fire on an encoded one or a binary open.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "mcm_engine"

# read_bytes/write_bytes are distinct method names, so matching these by name is
# safe: they never take an encoding and never need one.
_TEXT_PATH_METHODS = {"read_text", "write_text"}


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def _mode_is_binary(call: ast.Call) -> bool:
    """True only when the mode is a literal string containing 'b'. An absent or
    non-literal mode is treated as text (conservative: text needs encoding)."""
    mode = None
    if len(call.args) >= 2:
        mode = call.args[1]
    for k in call.keywords:
        if k.arg == "mode":
            mode = k.value
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value


def scan_source(code: str, filename: str) -> list[str]:
    """Return 'file:line' for every text open()/read_text/write_text lacking
    encoding=. Pure and dependency-free so it can be red-proofed on snippets."""
    hits: list[str] = []
    tree = ast.parse(code, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_builtin_open = isinstance(func, ast.Name) and func.id == "open"
        is_text_path = isinstance(func, ast.Attribute) and func.attr in _TEXT_PATH_METHODS
        if not (is_builtin_open or is_text_path):
            continue
        if is_builtin_open and _mode_is_binary(node):
            continue
        if not _has_kw(node, "encoding"):
            hits.append(f"{filename}:{node.lineno}")
    return hits


def test_production_code_opens_text_with_explicit_encoding():
    offenders: list[str] = []
    for py in sorted(SRC.rglob("*.py")):
        offenders += scan_source(py.read_text(encoding="utf-8"), str(py.relative_to(SRC.parent.parent)))
    assert not offenders, (
        "Text files opened without encoding= (platform-default encoding is a "
        "portability bug; pass encoding=\"utf-8\"):\n  " + "\n  ".join(offenders)
    )


def test_scanner_fires_on_unencoded_open():
    # positive fixtures: each MUST be caught, or the gate above is asleep.
    assert scan_source("open('x.txt')", "p.py")
    assert scan_source("open('x.txt', 'w')", "p.py")
    assert scan_source("from pathlib import Path\nPath('x').read_text()", "p.py")
    assert scan_source("from pathlib import Path\nPath('x').write_text(s)", "p.py")


def test_scanner_ignores_encoded_and_binary():
    # negative fixtures: innocent code MUST NOT be flagged.
    assert not scan_source("open('x.txt', encoding='utf-8')", "n.py")
    assert not scan_source("open('x.txt', 'w', encoding='utf-8')", "n.py")
    assert not scan_source("open('x.bin', 'rb')", "n.py")
    assert not scan_source("open('x.bin', 'wb')", "n.py")
    assert not scan_source("from pathlib import Path\nPath('x').read_bytes()", "n.py")
    assert not scan_source("from pathlib import Path\nPath('x').read_text(encoding='utf-8')", "n.py")
