"""Record operand types at singleton comparisons while running the clean suite."""

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
from profile_store import Store


def main(source, output):
    source, output = source.resolve(), output.resolve()
    os.chdir(source)
    sys.path.insert(0, str(source))
    concurrent.futures.ProcessPoolExecutor = InlinePool
    logging.getLogger().addHandler(logging.NullHandler())
    sites = []
    seen = set()
    fixed = {}

    def record(v, site):
        cls = type(v)
        if cls in (int, float, complex):
            label = "zero" if v == 0 else "one" if v == 1 else "number"
        else:
            label = fixed.get(cls)
            if label is None:
                eq, ne = cls.__eq__, cls.__ne__
                if cls is bool:
                    label = "bool"
                elif cls in (type(None), bytes, str, tuple, list, set, dict, frozenset):
                    label = "distinct"
                elif eq is object.__eq__ and ne is object.__ne__:
                    label = "identity"
                elif (
                    getattr(eq, "__module__", None) == "sqlglot.expressions.core"
                    and getattr(eq, "__qualname__", "") == "Expression.__eq__"
                    and getattr(ne, "__qualname__", "") == "Expression.__ne__"
                ):
                    label = "typed-expression"
                elif (
                    getattr(eq, "__module__", None) == "sqlglot.expressions.core"
                    and getattr(eq, "__qualname__", "") == "Expr.__eq__"
                    and getattr(ne, "__qualname__", "") == "Expr.__ne__"
                ):
                    label = "typed-expression"
                else:
                    label = "other"
                fixed[cls] = label
        seen.add((site, label))
        return v

    builtins._br_operand_type = record

    class Rewrite(ast.NodeTransformer):
        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1 or not isinstance(
                node.ops[0], (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)
            ):
                return node
            a, b = node.left, node.comparators[0]
            if not (
                isinstance(b, ast.Constant) and type(b.value) in (bool, type(None))
            ):
                return node
            site = len(sites)
            sites.append((self.path, node.lineno, node.col_offset))
            node.left = ast.copy_location(
                ast.Call(
                    ast.Name("_br_operand_type", ast.Load()),
                    [a, ast.Constant(site)],
                    [],
                ),
                a,
            )
            return node

    original = importlib.machinery.SourceFileLoader.get_code

    def code(loader, name):
        if not loader.path.startswith(str(source / "sqlglot") + "/"):
            return original(loader, name)
        rewriter = Rewrite()
        rewriter.path = os.path.relpath(loader.path, source)
        return compile(
            ast.fix_missing_locations(
                rewriter.visit(ast.parse(loader.get_data(loader.path)))
            ),
            loader.path,
            "exec",
            dont_inherit=True,
        )

    importlib.machinery.SourceFileLoader.get_code = code
    groups = defaultdict(list)
    for i, name in enumerate(Store().meta["names"]):
        file, cls, method = name.split("::")
        groups[file, cls].append((1 << i, method))
    tables = defaultdict(dict)
    for (file, cls), methods in groups.items():
        klass = getattr(importlib.import_module(file[:-3].replace("/", ".")), cls)
        seen.clear()
        klass.setUpClass()
        shared = seen.copy()
        for bit, method in methods:
            seen.clear()
            res = unittest.TestResult()
            klass(method).run(res)
            if res.wasSuccessful() and not res.skipped:
                for site, label in shared | seen:
                    table = tables[site]
                    table[label] = table.get(label, 0) | bit
        klass.tearDownClass()
        klass.doClassCleanups()
        print(file, flush=True)
    files = defaultdict(dict)
    for site, table in tables.items():
        path, line, col = sites[site]
        key = (line, col)
        target = files[path].setdefault(key, {})
        for label, bits in table.items():
            target[label] = target.get(label, 0) | bits
    with ZipFile(output, "w", ZIP_DEFLATED) as z:
        for path, data in files.items():
            z.writestr(path, marshal.dumps(data, 4))


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
