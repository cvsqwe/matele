"""The code in the docs stays valid: every Python block parses, and every
name it imports from matele exists."""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = sorted((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]
BLOCK = re.compile(r"^```python\n(.*?)^```", re.S | re.M)


def blocks() -> list[tuple[str, str]]:
	found = []
	for page in PAGES:
		for i, match in enumerate(BLOCK.finditer(page.read_text(encoding="utf-8"))):
			found.append((f"{page.relative_to(ROOT).as_posix()}#{i + 1}", match.group(1)))
	return found


BLOCKS = blocks()


def test_docs_have_code():
	assert len(BLOCKS) > 50


@pytest.mark.parametrize(("where", "code"), BLOCKS, ids=[where for where, _ in BLOCKS])
def test_block(where: str, code: str):
	tree = compile(code, where, "exec", flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
	assert isinstance(tree, ast.Module)
	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "matele":
			module = importlib.import_module(node.module)
			for alias in node.names:
				assert hasattr(module, alias.name), f"{where}: {node.module} has no {alias.name}"
		elif isinstance(node, ast.Import):
			for alias in node.names:
				if alias.name.split(".")[0] == "matele":
					importlib.import_module(alias.name)
