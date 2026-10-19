"""The code under test never imports the code that computes the answer key.

`close`, `forecast`, `memo`, `explain` and `controls` must not reach `c2c.truth_reference` through any
chain of imports, so "the result equals the reference" means the reference was computed independently.
The generator's truth writer and the tests may import it."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
FORBIDDEN = "c2c.truth_reference"
ROOTS = ("c2c.close", "c2c.forecast", "c2c.memo", "c2c.explain", "c2c.controls")
ALLOWED = ("c2c.gen.truth",)


def module_file(name):
    """The source file of the module `name` inside `src/`, or None when it is not ours."""
    base = SRC.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def package_of(name, path):
    return name if path.name == "__init__.py" else name.rpartition(".")[0]


def imports(name, path):
    """Every module `path` imports, anywhere in the file (function bodies and `TYPE_CHECKING` included)."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package_of(name, path).split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)  # `from pkg import module`
    return found


def reachable(root):
    """module -> the module it was first reached from, for everything `root` imports in this project."""
    seen = {root: None}
    queue = [root]
    while queue:
        name = queue.pop()
        path = module_file(name)
        if path is None:
            continue
        for target in sorted(imports(name, path)):
            parts = target.split(".")
            # importing `a.b.c` also runs `a` and `a.b`
            for i in range(1, len(parts) + 1):
                prefix = ".".join(parts[:i])
                if prefix.startswith("c2c") and prefix not in seen:
                    seen[prefix] = name
                    queue.append(prefix)
    return seen


def chain(seen, end):
    out = [end]
    while seen[out[-1]] is not None:
        out.append(seen[out[-1]])
    return " -> ".join(reversed(out))


@pytest.mark.parametrize("root", ROOTS)
def test_the_reference_is_never_reachable(root):
    if module_file(root) is None:
        pytest.skip(f"{root} is not present yet")
    seen = reachable(root)
    assert FORBIDDEN not in seen, chain(seen, FORBIDDEN)


def test_the_walk_sees_the_reference_where_it_is_imported():
    """Guard against a walk that finds nothing: the truth writer does import the reference."""
    for importer in ALLOWED:
        assert FORBIDDEN in reachable(importer)


def test_the_walk_follows_relative_and_from_imports(tmp_path, monkeypatch):
    pkg = tmp_path / "c2c"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text("from . import b\n", encoding="utf-8")
    (pkg / "b.py").write_text("def f():\n    from .truth_reference import x\n", encoding="utf-8")
    (pkg / "truth_reference.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "SRC", tmp_path)
    assert chain(reachable("c2c.a"), FORBIDDEN) == "c2c.a -> c2c.b -> c2c.truth_reference"
