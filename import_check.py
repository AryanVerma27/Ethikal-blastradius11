"""Identify patches that break collection through transitive imports."""

import importlib
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import zlib
from functools import lru_cache


def modules(names):
    return sorted({name.split("::")[0][:-3].replace("/", ".") for name in names})


def profile(root, output):
    from profile_store import Store

    prefix = str(root) + "/"
    ranges = {}
    hits = set()
    sys.path.insert(0, str(root))
    os.chdir(root)
    monitor = sys.monitoring

    def visited(code, line):
        path = code.co_filename
        if path.startswith(prefix):
            hits.add((path[len(prefix) :], line))
        return monitor.DISABLE

    monitor.use_tool_id(3, "test collection")
    monitor.register_callback(3, monitor.events.LINE, visited)
    monitor.set_events(3, monitor.events.LINE)
    clean = []
    for name in modules(Store().meta["names"]):
        hits.clear()
        monitor.restart_events()
        try:
            importlib.import_module(name)
            for path, line in hits:
                table = ranges.setdefault(path, {})
                table[line] = table.get(line, 0) | (1 << len(clean))
            clean.append(name)
        except Exception:
            pass
    monitor.set_events(3, 0)
    output.write_bytes(zlib.compress(json.dumps([clean, ranges]).encode(), 9))


@lru_cache(maxsize=1)
def coverage():
    names, ranges = json.loads(
        zlib.decompress(Path(__file__).with_name("collection.z").read_bytes())
    )
    return names, {
        k: {int(n): mask for n, mask in v.items()} for k, v in ranges.items()
    }


def broken(source, edits, replacements):
    if not replacements:
        return False
    names, lines = coverage()
    mask = 0
    for path, patch in edits.items():
        table = lines.get(path, {})
        for pos, op, _ in patch:
            mask |= table.get(pos + 1, 0)
            if op == "+":
                mask |= table.get(pos, 0)
    if not mask:
        return False
    selected = [name for i, name in enumerate(names) if mask >> i & 1]
    try:
        result = subprocess.run(
            [sys.executable, __file__, "check", str(source)],
            input=json.dumps([selected, replacements]).encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        return True
    return result.returncode != 0


def check(root):
    names, replacements = json.load(sys.stdin)
    original = importlib.machinery.SourceFileLoader.get_code

    def code(loader, name):
        if loader.path in replacements:
            return compile(
                replacements[loader.path], loader.path, "exec", dont_inherit=True
            )
        return original(loader, name)

    importlib.machinery.SourceFileLoader.get_code = code
    os.chdir(root)
    sys.path.insert(0, str(root))
    for name in names:
        importlib.import_module(name)


if __name__ == "__main__":
    if sys.argv[1] == "check":
        check(Path(sys.argv[2]).resolve())
    else:
        profile(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
