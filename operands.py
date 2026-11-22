"""Per-test operand types observed in the pristine test suite."""

import ast
import marshal
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile


@lru_cache(maxsize=16)
def sites(path):
    with ZipFile(Path(__file__).with_name("operands.zip")) as z:
        try:
            return marshal.loads(z.read(path))
        except KeyError:
            return {}


def exclusions(tree, modified, path, removed, same):
    pairs = {(ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)}
    for old, new in zip(ast.walk(tree), ast.walk(modified)):
        if not isinstance(old, ast.Compare) or not isinstance(new, ast.Compare):
            continue
        if len(old.ops) != 1 or len(new.ops) != 1 or old.lineno not in removed:
            continue
        before, after = type(old.ops[0]), type(new.ops[0])
        if (before, after) not in pairs and (after, before) not in pairs:
            continue
        right = old.comparators[0]
        if not isinstance(right, ast.Constant) or type(right.value) not in (
            type(None),
            bool,
        ):
            continue
        original = new.ops
        try:
            new.ops = old.ops
            if not same(tree, modified):
                continue
        finally:
            new.ops = original
        seen = unknown = 0
        for kind, bits in sites(path).get((old.lineno, old.col_offset), {}).items():
            seen |= bits
            if (
                kind == "other"
                or (kind == "one" and right.value is True)
                or (kind == "zero" and right.value is False)
            ):
                unknown |= bits
        return seen & ~unknown
    return 0
