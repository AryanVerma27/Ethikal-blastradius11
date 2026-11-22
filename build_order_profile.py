"""Record integer operand ranges on each side of literal comparison thresholds."""

import ast
import builtins
import concurrent.futures
import importlib
import importlib.machinery
import logging
import marshal
import os
import sys
import unittest
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from build_map import InlinePool
from order_profile import integer, OPERATORS
from profile_store import Store


def main(source, output):
    source, output = source.resolve(), output.resolve()
    os.chdir(source)
    sys.path.insert(0, str(source))
    concurrent.futures.ProcessPoolExecutor = InlinePool
    logging.getLogger().addHandler(logging.NullHandler())
    sites = []
    seen = {}
    importing = 0
    import_observations = {}

    def update(table, value, index, threshold):
        if type(value) not in (int, bool):
            table[index, None] = None
            return value
        bucket = (value > threshold) - (value < threshold)
        key = index, bucket
        pair = table.get(key)
        if pair is None:
            table[key] = [value, value]
        elif value < pair[0]:
            pair[0] = value
        elif value > pair[1]:
            pair[1] = value
        return value

    def observe(value, index, threshold):
        update(seen, value, index, threshold)
        if importing:
            update(import_observations, value, index, threshold)
        return value

    builtins._br_integer_range = observe

    class Rewrite(ast.NodeTransformer):
        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1 or type(node.ops[0]) not in OPERATORS:
                return node
            threshold = integer(node.comparators[0])
            if threshold is None:
                return node
            index = len(sites)
            sites.append((self.path, (node.lineno, node.col_offset)))
            node.left = ast.copy_location(
                ast.Call(
                    ast.Name("_br_integer_range", ast.Load()),
                    [node.left, ast.Constant(index), ast.Constant(threshold)],
                    [],
                ),
                node.left,
            )
            return node

    pristine = importlib.machinery.SourceFileLoader.get_code

    def code(loader, name):
        if not loader.path.startswith(str(source / "sqlglot") + "/"):
            return pristine(loader, name)
        rewrite = Rewrite()
        rewrite.path = os.path.relpath(loader.path, source)
        return compile(
            ast.fix_missing_locations(
                rewrite.visit(ast.parse(loader.get_data(loader.path)))
            ),
            loader.path,
            "exec",
            dont_inherit=True,
        )

    importlib.machinery.SourceFileLoader.get_code = code
    original_exec = importlib.machinery.SourceFileLoader.exec_module

    def exec_module(loader, module):
        nonlocal importing
        importing += 1
        try:
            return original_exec(loader, module)
        finally:
            importing -= 1

    importlib.machinery.SourceFileLoader.exec_module = exec_module
    groups = defaultdict(list)
    for i, name in enumerate(Store().meta["names"]):
        path, cls, method = name.split("::")
        groups[path, cls].append((i, method))
    masks = defaultdict(dict)

    def save(obs, bits):
        for (index, bucket), pair in obs.items():
            domain = None if pair is None else tuple(pair)
            table = masks[index]
            table[domain] = table.get(domain, 0) | bits

    for (path, cls), methods in groups.items():
        klass = getattr(importlib.import_module(path[:-3].replace("/", ".")), cls)
        seen.clear()
        klass.setUpClass()
        shared = seen.copy()
        successful = 0
        for i, method in methods:
            seen.clear()
            result = unittest.TestResult()
            klass(method).run(result)
            if result.wasSuccessful() and not result.skipped:
                save(seen, 1 << i)
                successful |= 1 << i
        save(shared, successful)
        seen.clear()
        klass.tearDownClass()
        klass.doClassCleanups()
        save(seen, successful)
    # Import-time values constrain every test already observed at that site.
    for index in list(masks):
        combined = 0
        for bits in masks[index].values():
            combined |= bits
        save({k: v for k, v in import_observations.items() if k[0] == index}, combined)
    files = defaultdict(dict)
    for index, table in masks.items():
        path, key = sites[index]
        dest = files[path].setdefault(key, {})
        for domain, bits in table.items():
            dest[domain] = dest.get(domain, 0) | bits
    with ZipFile(output, "w", ZIP_DEFLATED) as z:
        for path, records in files.items():
            z.writestr(path, marshal.dumps(records, 4))


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
