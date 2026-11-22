"""Profile the current token at each literal Parser._match call in clean tests."""

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
from match_inputs import shape
from profile_store import Store


def main(source, output):
    source, output = source.resolve(), output.resolve()
    os.chdir(source)
    sys.path.insert(0, str(source))
    logging.getLogger().addHandler(logging.NullHandler())
    concurrent.futures.ProcessPoolExecutor = InlinePool
    sites = []
    seen, imports = set(), set()
    importing = 0

    def observe(i, method):
        owner = method.__self__
        fn = getattr(method, "__func__", None)
        if (
            fn is not None
            and fn.__module__ == "sqlglot.parser"
            and fn.__qualname__ == "Parser._match"
        ):
            key = owner._curr.token_type.name
        else:
            key = None
        seen.add((i, key))
        if importing:
            imports.add((i, key))
        return method

    builtins._br_match_observe = observe

    class Rewrite(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            if not shape(node):
                return node
            i = len(sites)
            sites.append((self.path, (node.lineno, node.col_offset)))
            node.func = ast.copy_location(
                ast.Call(
                    ast.Name("_br_match_observe", ast.Load()),
                    [ast.Constant(i), node.func],
                    [],
                ),
                node.func,
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
    original_exec = importlib.machinery.SourceFileLoader.exec_module

    def execute(loader, module):
        nonlocal importing
        importing += 1
        try:
            return original_exec(loader, module)
        finally:
            importing -= 1

    importlib.machinery.SourceFileLoader.exec_module = execute
    groups = defaultdict(list)
    for i, name in enumerate(Store().meta["names"]):
        file, cls, method = name.split("::")
        groups[file, cls].append((1 << i, method))
    masks = defaultdict(dict)

    def save(items, bits):
        for i, key in items:
            table = masks[i]
            table[key] = table.get(key, 0) | bits

    for (file, cls), methods in groups.items():
        klass = getattr(importlib.import_module(file[:-3].replace("/", ".")), cls)
        seen.clear()
        klass.setUpClass()
        shared = seen.copy()
        valid = 0
        for bit, method in methods:
            seen.clear()
            r = unittest.TestResult()
            klass(method).run(r)
            if r.wasSuccessful() and not r.skipped:
                save(seen, bit)
                valid |= bit
        seen.clear()
        klass.tearDownClass()
        klass.doClassCleanups()
        save(shared | seen, valid)
    global_masks = {}
    for i, key in imports:
        if i not in global_masks:
            bits = 0
            for sample in masks[i].values():
                bits |= sample
            global_masks[i] = bits
        save([(i, key)], global_masks[i])
    files = defaultdict(dict)
    from sqlglot.tokens import TokenType

    members = {k: v.name for k, v in TokenType.__members__.items()}
    for i, entries in masks.items():
        file, site = sites[i]
        table = files[file].setdefault(site, {})
        for key, bits in entries.items():
            table[key] = table.get(key, 0) | bits
    with ZipFile(output, "w", ZIP_DEFLATED) as z:
        for file, table in files.items():
            z.writestr(file, marshal.dumps((table, members), 4))


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
