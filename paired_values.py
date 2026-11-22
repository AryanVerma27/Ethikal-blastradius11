"""Evaluate isolated comparisons against the operand pairs observed by each test."""

import ast
import marshal
import operator
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile
from order_profile import OPERATORS

RELATIONS = {**OPERATORS, ast.Is: operator.is_, ast.IsNot: operator.is_not}


@lru_cache(maxsize=16)
def records(path):
    with ZipFile(Path(__file__).with_name("pairs.zip")) as z:
        try:
            return marshal.loads(z.read(path))
        except KeyError:
            return {}, {}


def offset(original, changed, same):
    if same(original, changed):
        return 0
    if (
        isinstance(changed, ast.BinOp)
        and isinstance(changed.right, ast.Constant)
        and type(changed.right.value) is int
        and same(original, changed.left)
    ):
        if isinstance(changed.op, ast.Add):
            return changed.right.value
        if isinstance(changed.op, ast.Sub):
            return -changed.right.value
    return None


def exclusions(path, tree, modified, removed, same):
    for a, b in zip(ast.walk(tree), ast.walk(modified)):
        if (
            not isinstance(a, ast.Compare)
            or not isinstance(b, ast.Compare)
            or a.lineno not in removed
            or len(a.ops) != 1
            or len(b.ops) != 1
        ):
            continue
        before, after = type(a.ops[0]), type(b.ops[0])
        if before not in RELATIONS or after not in RELATIONS or same(a, b):
            continue
        old, new = a.comparators[0], b.comparators[0]
        dl, dr = offset(a.left, b.left, same), offset(old, new, same)
        member = (
            isinstance(old, ast.Attribute)
            and isinstance(new, ast.Attribute)
            and same(old.value, new.value)
            and old.attr != new.attr
        )
        if dl is None or (dr is None and not member):
            continue
        original = b.left, b.ops, b.comparators
        try:
            b.left, b.ops, b.comparators = a.left, a.ops, a.comparators
            if not same(tree, modified):
                continue
        finally:
            b.left, b.ops, b.comparators = original
        sites, classes = records(path)
        seen = unsafe = 0
        for domain, bits in sites.get((a.lineno, a.col_offset), {}).items():
            seen |= bits
            equal = False
            if (
                isinstance(domain, int)
                and dr is not None
                and before in OPERATORS
                and after in OPERATORS
                and abs(dl) + abs(dr) <= 2
            ):
                # The end buckets contain distances of magnitude three or more.
                equal = OPERATORS[before](domain, 0) == OPERATORS[after](
                    domain + dl, dr
                )
            elif (
                isinstance(domain, tuple)
                and dl == 0
                and before in (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)
                and after in (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)
            ):
                cls, l, r = domain
                names = classes[cls]
                if l not in names or r not in names or names[l] != l or names[r] != r:
                    unsafe |= bits
                    continue
                if dr == 0:
                    changed = r
                elif (
                    member
                    and classes[cls].get(old.attr) == r
                    and new.attr in classes[cls]
                ):
                    changed = classes[cls][new.attr]
                else:
                    unsafe |= bits
                    continue
                equal = ((l == r) == (before in (ast.Is, ast.Eq))) == (
                    (l == changed) == (after in (ast.Is, ast.Eq))
                )
            if not equal:
                unsafe |= bits
        return seen & ~unsafe
    return 0
