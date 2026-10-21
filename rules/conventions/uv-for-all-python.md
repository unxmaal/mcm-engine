# Always use uv for ALL Python — never system python3 / pip / venv

## Rule (invariant)
Use `uv` for every Python operation, including throwaway and scratch work. Never
invoke system `python3`, `pip`, or `python3 -m venv` directly — they run the
wrong interpreter (e.g. an ancient system python3.9 with a stale pip) and
pollute the system Python.

| Instead of | Use |
|---|---|
| `python3 script.py` / `python script.py` | `uv run python script.py` |
| `pytest` | `uv run pytest` |
| `pip install X` | `uv pip install X` (in a uv venv) |
| `python3 -m venv .v` | `uv venv --python 3.11 .v` |
| `pip install --python .v/bin/python X` | `uv pip install --python .v/bin/python X` |

This applies to one-off checks too: to test a package version in isolation,
`uv venv --python 3.XX <dir>` then `uv pip install --python <dir>/bin/python ...`,
never `python3 -m venv` + the system `pip`.

## Why
System `python3` on macOS/CI is often an old minor (3.9) with an old pip that
can't even resolve modern wheels, and any `pip install` against it mutates the
system environment. uv pins the interpreter and keeps every install isolated and
reproducible.

Reinforces the existing uv invariants (use uv for all Python invocations; never
call `.venv/bin/python` or bare `python` directly).
