"""Replay the affected rules within one optimizer call."""

import hashlib
import importlib
import json
import marshal
import pickle
import time
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile

from stages import decode_expr, encode_expr


@lru_cache(maxsize=1)
def archive():
    return ZipFile(Path(__file__).with_name("optimizer-steps.zip"))


@lru_cache(maxsize=32)
def record_for(test, number):
    try:
        return marshal.loads(archive().read(f"records/{test}/{number}"))
    except KeyError:
        return None


def encode(value):
    return json.dumps(encode_expr(value), separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=256)
def matches(module, function, expression, kwargs, expected, expected_kwargs):
    rule = getattr(importlib.import_module(module), function)
    tree = decode_expr(json.loads(archive().read(expression)))
    arguments = pickle.loads(archive().read(kwargs))
    value = rule(tree, **arguments)
    if digest(pickle.dumps(arguments, protocol=4)) != expected_kwargs:
        return False
    if digest(encode(value)) != expected:
        return False
    for parent in value.walk():
        for key, arg in parent.args.items():
            children = enumerate(arg) if isinstance(arg, list) else ((None, arg),)
            for i, child in children:
                if hasattr(child, "arg_key") and (
                    child.parent is not parent
                    or child.arg_key != key
                    or child.index != i
                ):
                    return False
    return True


def unchanged(test, number, locations, deadline=None, expected=None):
    if not locations:
        return False
    record = record_for(test, number)
    if record is None:
        return False
    outside, steps, baseline = record
    if expected is not None and expected != baseline:
        return False

    def touches(coverage):
        return any(
            not lines.isdisjoint(coverage.get(path, ()))
            for path, lines in locations.items()
        )

    if touches(outside):
        return False
    for coverage, call in steps:
        if touches(coverage) and (
            (deadline is not None and time.process_time() >= deadline)
            or not matches(*call)
        ):
            return False
    return True


def residual_rows(name, locations, deadline):
    """Return fixture rows whose optimizer calls still need the upstream assertion."""
    record = record_for(name, -1)
    if record is None:
        return None
    outside, steps, _ = record

    def touches(coverage):
        return any(
            not lines.isdisjoint(coverage.get(path, ()))
            for path, lines in locations.items()
        )

    if touches(outside):
        return None
    try:
        rows, owners = marshal.loads(archive().read(f"rows/{name}"))
    except KeyError:
        return None
    if len(owners) != len(steps):
        return None
    known = {tuple(row) for row in rows}
    remaining = {path: set() for path, _ in known}
    for row, (coverage, call) in zip(owners, steps):
        if not touches(coverage):
            continue
        if row is not None and row[1] in remaining.get(row[0], set()):
            continue
        if time.process_time() < deadline and matches(*call):
            continue
        if row not in known:
            return None
        remaining[row[0]].add(row[1])
    return remaining
