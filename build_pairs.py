"""Measure relational distances and enum identities on the pristine test catalog."""

import ast
import builtins
import concurrent.futures
import enum
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
from paired_values import RELATIONS
from profile_store import Store


def main(source, output):
    source, output = source.resolve(), output.resolve()
    os.chdir(source)
    sys.path.insert(0, str(source))
    logging.getLogger().addHandler(logging.NullHandler())
    concurrent.futures.ProcessPoolExecutor = InlinePool
    sites, functions = [], []
    seen, imports = set(), set()
    importing = 0
    categories = {}
    members = {}

    def compare(i, left, right):
        l, r = type(left), type(right)
        if l in (bool, int) and r in (bool, int):
            delta = left - right
            key = 3 if delta >= 3 else -3 if delta <= -3 else delta
        elif l is r:
            if l not in categories:
                valid = (
                    issubclass(l, enum.Enum)
                    and l.__eq__ in (int.__eq__, str.__eq__, object.__eq__)
                    and l.__ne__ in (int.__ne__, str.__ne__, object.__ne__)
                )
                category = (l.__module__, l.__qualname__) if valid else None
                categories[l] = category
                if valid:
                    members[category] = {k: v.name for k, v in l.__members__.items()}
            category = categories[l]
            key = (category, left.name, right.name) if category else None
        else:
            key = None
        seen.add((i, key))
        if importing:
            imports.add((i, key))
        return functions[i](left, right)

    builtins._br_pair_compare = compare

    class Rewrite(ast.NodeTransformer):
        def visit_Compare(self, node):
            self.generic_visit(node)
            if (
                len(node.ops) != 1
                or type(node.ops[0]) not in RELATIONS
                or isinstance(node.comparators[0], ast.Constant)
            ):
                return node
            i = len(sites)
            sites.append((self.path, (node.lineno, node.col_offset)))
            functions.append(RELATIONS[type(node.ops[0])])
            return ast.copy_location(
                ast.Call(
                    ast.Name("_br_pair_compare", ast.Load()),
                    [ast.Constant(i), node.left, node.comparators[0]],
                    [],
                ),
                node,
            )

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
    enums = defaultdict(dict)
    for i, entries in masks.items():
        file, site = sites[i]
        table = files[file].setdefault(site, {})
        for key, bits in entries.items():
            table[key] = table.get(key, 0) | bits
            if isinstance(key, tuple):
                enums[file][key[0]] = members[key[0]]
    with ZipFile(output, "w", ZIP_DEFLATED) as z:
        for file, table in files.items():
            z.writestr(file, marshal.dumps((table, enums[file]), 4))


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
