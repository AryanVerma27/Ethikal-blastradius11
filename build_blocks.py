"""Trace independent assertion groups of the pinned parser and scope stress tests."""

import importlib
import marshal
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch
import unittest
import zlib
from test_blocks import parts, method_for


def main(source, output):
    source, output = source.resolve(), output.resolve()
    os.chdir(source)
    sys.path.insert(0, str(source))
    names = [
        "tests/test_optimizer.py::TestOptimizer::test_scope",
        "tests/test_parser.py::TestParser::test_max_nodes",
    ]
    hits = {}
    prefix = str(source) + "/"
    paths = {}
    mon = sys.monitoring

    def line(code, number):
        path = code.co_filename
        if path not in paths:
            paths[path] = (
                path[len(prefix) :] if path.startswith(prefix + "sqlglot/") else None
            )
        p = paths[path]
        if p:
            hits.setdefault(p, set()).add(number)
        return mon.DISABLE

    mon.use_tool_id(3, "test block coverage")
    mon.register_callback(3, mon.events.LINE, line)
    mon.set_events(3, mon.events.LINE)

    def reset():
        hits.clear()
        mon.restart_events()

    data = {}
    for name in names:
        file, cls, method = name.split("::")
        klass = getattr(importlib.import_module(file[:-3].replace("/", ".")), cls)
        original = getattr(klass, method)
        clusters, tree = parts(original)
        reset()
        klass.setUpClass()
        setup = hits.copy()
        blocks = []
        try:
            for indices in clusters:
                fn = method_for(original, tree, indices)
                with patch.object(klass, method, fn):
                    reset()
                    start = time.process_time()
                    result = unittest.TestResult()
                    klass(method).run(result)
                    assert result.wasSuccessful() and not result.skipped, (
                        name,
                        indices,
                        result.errors,
                        result.failures,
                    )
                    blocks.append((indices, hits.copy(), time.process_time() - start))
        finally:
            reset()
            klass.tearDownClass()
            klass.doClassCleanups()
            for p, lines in hits.items():
                setup.setdefault(p, set()).update(lines)
        data[name] = (blocks, setup)
    mon.set_events(3, 0)
    output.write_bytes(zlib.compress(marshal.dumps(data, 4), 9))


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
