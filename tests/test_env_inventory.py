"""Every MCM_* setting the code reads is documented, and every documented or deployed one has a reader (#226)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NAME = re.compile(r"MCM_[A-Z0-9_]+")
DEPLOY_FILES = (
    "examples/.env.example",
    "examples/docker-compose.yml",
    "deploy/helm/mcm-engine/values.yaml",
    "deploy/helm/mcm-engine/README.md",
    *(str(p.relative_to(REPO)) for p in sorted((REPO / "deploy/helm/mcm-engine/templates").glob("*"))),
)


def read_names() -> dict[str, str]:
    """Each MCM_* name the product reads, with one place that reads it: a whole
    string literal in src/ or scripts/, or a name in the Dockerfile."""
    out: dict[str, str] = {}
    for path in sorted([*(REPO / "src").rglob("*.py"), *(REPO / "scripts").rglob("*.py")]):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and NAME.fullmatch(node.value):
                out.setdefault(node.value, f"{path.relative_to(REPO)}:{node.lineno}")
    for name in NAME.findall((REPO / "Dockerfile").read_text(encoding="utf-8")):
        out.setdefault(name, "Dockerfile")
    return out


def names_in(rel: str) -> set[str]:
    return set(NAME.findall((REPO / rel).read_text(encoding="utf-8")))


def has_reader(name: str, readers: dict[str, str]) -> bool:
    # A documented prefix such as MCM_BACKENDS_ stands for the names under it.
    return name in readers or (name.endswith("_") and any(r.startswith(name) for r in readers))


def test_every_setting_the_code_reads_is_in_the_readme():
    readers = read_names()
    documented = names_in("README.md")
    missing = {n: where for n, where in readers.items() if n not in documented}
    assert not missing, f"read but not in README.md: {missing}"


def test_every_setting_in_the_readme_has_a_reader():
    readers = read_names()
    dead = sorted(n for n in names_in("README.md") if not has_reader(n, readers))
    assert not dead, f"documented in README.md but nothing reads them: {dead}"


@pytest.mark.parametrize("rel", DEPLOY_FILES)
def test_every_setting_a_deploy_file_sets_has_a_reader(rel):
    readers = read_names()
    dead = sorted(n for n in names_in(rel) if not has_reader(n, readers))
    assert not dead, f"{rel} sets names nothing reads: {dead}"
