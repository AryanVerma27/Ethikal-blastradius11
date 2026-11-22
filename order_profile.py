"""Prove integer comparisons unchanged for the input ranges observed in each test."""

import ast
import marshal
import operator
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile

OPERATORS = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def integer(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    return value if type(value) is int else None


@lru_cache(maxsize=16)
def tables(path):
    with ZipFile(Path(__file__).with_name("order-profiles.zip")) as archive:
        try:
            return marshal.loads(archive.read(path))
        except KeyError:
            return {}


def exclusions(path, tree, modified, removed, same):
    for old, new in zip(ast.walk(tree), ast.walk(modified)):
        if (
            not isinstance(old, ast.Compare)
            or not isinstance(new, ast.Compare)
            or old.lineno not in removed
        ):
            continue
        if len(old.ops) != 1 or len(new.ops) != 1:
            continue
        before = OPERATORS.get(type(old.ops[0]))
        after = OPERATORS.get(type(new.ops[0]))
        left = integer(old.comparators[0])
        right = integer(new.comparators[0])
        if (
            before is None
            or after is None
            or left is None
            or right is None
            or (before is after and left == right)
        ):
            continue
        ops, rhs = new.ops, new.comparators
        try:
            new.ops, new.comparators = old.ops, old.comparators
            if not same(tree, modified):
                continue
        finally:
            new.ops, new.comparators = ops, rhs
        seen = unsafe = 0
        for domain, bits in tables(path).get((old.lineno, old.col_offset), {}).items():
            if domain is None:
                unsafe |= bits
                continue
            seen |= bits
            lo, hi = domain
            # Between consecutive thresholds, both relations are constant.
            if any(
                before(i, left) != after(i, right)
                for i in (lo, hi, left - 1, left, left + 1, right - 1, right, right + 1)
                if lo <= i <= hi
            ):
                unsafe |= bits
        return seen & ~unsafe
    return 0
