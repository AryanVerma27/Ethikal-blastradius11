"""Replay the SQL inputs to the upstream DuckDB equivalence assertion."""

import marshal
import os
from pathlib import Path
import sys
import unittest
from functools import lru_cache
from unittest.mock import patch
import zlib

NAME = "tests/test_executor.py::TestExecutor::test_optimized_tpch"


def build(source, output):
    source = source.resolve()
    output = output.resolve()
    sys.path.insert(0, str(source))
    os.chdir(source)
    root = str(source) + "/"
    outside = {}
    hits = outside
    cases = []
    mon = sys.monitoring

    def visit(code, line):
        path = code.co_filename
        if path.startswith(root + "sqlglot/"):
            hits.setdefault(path[len(root) :], set()).add(line)
        return mon.DISABLE

    mon.use_tool_id(3, "DuckDB reference SQL")
    mon.register_callback(3, mon.events.LINE, visit)
    mon.set_events(3, mon.events.LINE)
    from tests import test_executor as suite

    transpile = suite.transpile

    def traced(sql, **kwargs):
        nonlocal hits
        if kwargs != {"write": "duckdb"}:
            raise ValueError("unexpected transpile options")
        hits = {}
        mon.restart_events()
        try:
            result = transpile(sql, **kwargs)
            cases.append((sql, result[0], hits))
            return result
        finally:
            hits = outside
            mon.restart_events()

    klass = suite.TestExecutor
    klass.setUpClass()
    try:
        with patch.object(suite, "transpile", traced):
            result = unittest.TestResult()
            klass("test_optimized_tpch").run(result)
            if not result.wasSuccessful() or result.skipped or not cases:
                raise ValueError("reference SQL capture did not pass")
    finally:
        klass.tearDownClass()
        klass.doClassCleanups()
        mon.set_events(3, 0)
    output.write_bytes(zlib.compress(marshal.dumps((outside, cases), 4), 6))


@lru_cache(maxsize=1)
def profile():
    return marshal.loads(
        zlib.decompress(Path(__file__).with_name("reference-sql.z").read_bytes())
    )


def plan(path, lines):
    outside, cases = profile()
    if not lines.isdisjoint(outside.get(path, ())):
        return None
    return [
        i
        for i, (_, _, hits) in enumerate(cases)
        if not lines.isdisjoint(hits.get(path, ()))
    ]


def unchanged(indices):
    from sqlglot import transpile

    _, cases = profile()
    for i in indices:
        sql, expected, _ = cases[i]
        if transpile(sql, write="duckdb")[0] != expected:
            return False
    return True


if __name__ == "__main__":
    build(*map(Path, sys.argv[1:]))
