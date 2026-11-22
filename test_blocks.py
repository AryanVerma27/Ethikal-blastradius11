"""Execute connected statements from long upstream tests on their original inputs."""

import ast
import copy
import inspect
import marshal
from functools import lru_cache
from pathlib import Path
import textwrap
import unittest
from unittest.mock import patch
import zlib


def parts(fn):
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    func = tree.body[0]
    owners = list(range(len(func.body)))

    def root(i):
        while i != owners[i]:
            owners[i] = owners[owners[i]]
            i = owners[i]
        return i

    assigned = {}
    for i, statement in enumerate(func.body):
        uses = {
            n.id
            for n in ast.walk(statement)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        writes = {
            n.id
            for n in ast.walk(statement)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        definite = (
            {t.id for t in statement.targets if isinstance(t, ast.Name)}
            if isinstance(statement, ast.Assign)
            else set()
        )
        # A conditional write can leave the previous binding live.
        for key in uses | (writes - definite):
            if key in assigned:
                owners[root(i)] = root(assigned[key])
        for key in writes:
            assigned[key] = i
    clusters = {}
    for i in range(len(owners)):
        clusters.setdefault(root(i), []).append(i)
    return list(clusters.values()), tree


def method_for(fn, tree, indices):
    new = copy.deepcopy(tree)
    function = new.body[0]
    function.decorator_list = []
    function.body = [function.body[i] for i in sorted(indices)] or [
        ast.copy_location(ast.Pass(), function)
    ]
    ns = {}
    exec(
        compile(new, fn.__code__.co_filename, "exec", dont_inherit=True),
        fn.__globals__,
        ns,
    )
    return ns[fn.__name__]


@lru_cache(maxsize=1)
def catalog():
    return marshal.loads(
        zlib.decompress(Path(__file__).with_name("test-blocks.z").read_bytes())
    )


def plan(name, locations):
    record = catalog().get(name)
    if record is None:
        return None
    blocks, setup = record

    def touches(hits):
        return any(
            not lines.isdisjoint(hits.get(path, ()))
            for path, lines in locations.items()
        )

    if touches(setup):
        return None
    indices = []
    cpu = 0
    for statements, hits, seconds in blocks:
        if touches(hits):
            indices.extend(statements)
            cpu += seconds
    if len(indices) == sum(len(b[0]) for b in blocks):
        return None
    return indices, cpu


def run(klass, name, indices):
    method = name.split("::")[-1]
    original = getattr(klass, method)
    _, tree = parts(original)
    with patch.object(klass, method, method_for(original, tree, indices)):
        result = unittest.TestResult()
        result.failfast = True
        klass(method).run(result)
    return result.wasSuccessful() and not result.skipped
