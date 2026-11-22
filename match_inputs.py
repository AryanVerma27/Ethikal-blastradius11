"""Use observed parser inputs to price isolated token-matching edits."""

import ast
import marshal
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile


def token(node):
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "TokenType"
    ):
        return node.attr


def shape(node):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if (
        not isinstance(node.func.value, ast.Name)
        or node.func.value.id != "self"
        or node.func.attr != "_match"
        or not node.args
    ):
        return None
    return token(node.args[0])


@lru_cache(maxsize=16)
def load(path):
    with ZipFile(Path(__file__).with_name("match-inputs.zip")) as archive:
        if path in archive.namelist():
            return marshal.loads(archive.read(path))
    return {}, ()


def exclusions(path, original, patched, removed, same):
    for old, new in zip(ast.walk(original), ast.walk(patched)):
        before, after = shape(old), shape(new)
        if not before or not after or old.lineno not in removed:
            continue

        # Changing the requested token or the advance flag has no effect on
        # a call which returns False in both versions. Only literal flags
        # can be ignored: evaluating other arguments could have side effects.
        def flag(call):
            if len(call.args) > 2 or any(k.arg != "advance" for k in call.keywords):
                return None
            args = call.args[1:] + [k.value for k in call.keywords]
            if not args:
                return True
            if (
                len(args) == 1
                and isinstance(args[0], ast.Constant)
                and type(args[0].value) is bool
            ):
                return args[0].value
            return None

        old_flag, new_flag = flag(old), flag(new)
        if (
            old_flag is None
            or new_flag is None
            or (before == after and old_flag == new_flag)
        ):
            continue
        args, keywords = new.args, new.keywords
        new.args, new.keywords = old.args, old.keywords
        try:
            if not same(original, patched):
                continue
        finally:
            new.args, new.keywords = args, keywords
        sites, members = load(path)
        if after not in members or before not in members:
            return 0
        records = sites.get((old.lineno, old.col_offset), {})
        # An overridden matcher or either matching token can change the result.
        unsafe = (
            records.get(None, 0)
            | records.get(members[before], 0)
            | records.get(members[after], 0)
        )
        seen = 0
        for bits in records.values():
            seen |= bits
        return seen & ~unsafe
    return 0
