import ast
import operator
import guards
import match_inputs
import import_check
import test_blocks
import reference_sql
import order_profile
import operands
import paired_values
import truth_sites
import compiled_equal
from branches import guarded, get, put, site
from cases import OPT_FILES, changed_pairs, pair_versions
import json
import re
import sys
import time
from probe import check
import stress
import rule_replay
from functools import lru_cache
from pathlib import Path


def schedule(selected, durations):
    if not selected:
        return []
    import heapq

    order = sorted(selected, key=lambda n: (-durations[n], n))
    total = sum(durations[n] for n in order)
    longest = durations[order[0]]
    best_cost, best = total + 2000, [order]
    for count in range(2, min(8, len(order)) + 1):
        if max(longest, (total + count - 1) // count) + count * 2000 >= best_cost:
            continue
        loads = [(0, slot) for slot in range(count)]
        bins = [[] for _ in loads]
        for n in order:
            load, slot = loads[0]
            bins[slot].append(n)
            heapq.heapreplace(loads, (load + durations[n], slot))
        cost = max(loads)[0] + 2000 * count
        if cost < best_cost:
            best_cost, best = cost, bins
    return best


def changes(patch):
    files = {}
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            path = line.split(" b/", 1)[1]
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("unexpected patch path")
            edits = files.setdefault(path, [])
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
            pos = int(m[1]) if m[2] == "0" else max(0, int(m[1]) - 1)
            in_hunk = True
        elif not in_hunk or line.startswith("\\"):
            continue
        elif line.startswith(" "):
            pos += 1
        elif line.startswith("-"):
            edits.append((pos, "-", line[1:]))
            pos += 1
        elif line.startswith("+"):
            edits.append((pos, "+", line[1:]))
    return files


@lru_cache(None)
def source_lines(path):
    return (SOURCE / path).read_text().splitlines()


def local_function(lines, first, last):
    snippet = "\n".join(lines[first - 1 : last]) + "\n"
    indented = lines[first - 1][:1].isspace()
    text = "if True:\n" + snippet if indented else snippet
    tree = ast.parse(text)
    compile(tree, "<function>", "exec")
    node = tree.body[0].body[0] if indented else tree.body[0]
    if not isinstance(node, ast.FunctionDef):
        raise ValueError("function boundary changed")
    return ast.increment_lineno(node, first - 1 - int(indented))


def update(lines, edits):
    out = []
    end = 0
    for pos, op, value in edits:
        out.extend(lines[end:pos])
        end = pos
        if op == "-":
            if lines[pos] != value:
                raise ValueError("patch context mismatch")
            end += 1
        else:
            out.append(value)
    out.extend(lines[end:])
    return "\n".join(out) + "\n"


def same(a, b):
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        return all(same(getattr(a, k, None), getattr(b, k, None)) for k in a._fields)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def checkpoint_lines(path, edits, first, end):
    """Narrow a trace to a single-line statement's evaluated value."""
    broad = set(range(first, end + 1))
    if len(edits) != 2 or edits[0][1] != "-" or edits[1][:2] != (edits[0][0] + 1, "+"):
        return broad
    line = edits[0][0] + 1
    old_lines = source_lines(path)
    new_lines = old_lines.copy()
    new_lines[line - 1] = edits[1][2]
    try:
        old = local_function(old_lines, first, end)
        new = local_function(new_lines, first, end)
    except (SyntaxError, ValueError, IndexError):
        return broad
    for a, b in zip(ast.walk(old), ast.walk(new)):
        if type(a) is not type(b) or getattr(a, "lineno", None) != line:
            continue
        if isinstance(a, (ast.If, ast.While)):
            field = "test"
        elif isinstance(a, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)):
            field = "value"
        else:
            continue
        left, right = getattr(a, field), getattr(b, field)
        if not all(
            isinstance(e, ast.expr) and e.lineno == e.end_lineno == line
            for e in (left, right)
        ):
            continue
        # A deferred code object can execute outside its defining statement.
        if any(
            isinstance(
                n,
                (
                    ast.Lambda,
                    ast.GeneratorExp,
                    ast.Yield,
                    ast.YieldFrom,
                    ast.Await,
                    ast.NamedExpr,
                ),
            )
            for e in (left, right)
            for n in ast.walk(e)
        ):
            continue
        setattr(b, field, left)
        if same(old, new):
            return {line}
        setattr(b, field, right)
    return broad


def scalar_exclusions(tree, modified, path, domains):
    ops = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
    }
    for old, new in zip(ast.walk(tree), ast.walk(modified)):
        if not isinstance(old, ast.Compare) or not isinstance(new, ast.Compare):
            continue
        if (
            len(old.ops) != 1
            or len(new.ops) != 1
            or type(old.ops[0]) not in ops
            or type(new.ops[0]) not in ops
        ):
            continue
        if type(old.ops[0]) == type(new.ops[0]) and same(
            old.comparators[0], new.comparators[0]
        ):
            continue
        table = domains.get(f"{path}:{old.lineno}:{old.col_offset}")
        if not table or not same(old.left, new.left):
            continue
        try:
            before, after = ast.literal_eval(old.comparators[0]), ast.literal_eval(
                new.comparators[0]
            )
        except (ValueError, TypeError):
            continue
        previous = new.ops, new.comparators
        new.ops, new.comparators = old.ops, old.comparators
        isolated = same(tree, modified)
        new.ops, new.comparators = previous
        if not isolated:
            continue
        seen, changed = 0, table.get("?", 0)
        for encoded, bits in table.items():
            if encoded == "?":
                continue
            kind, value = json.loads(encoded)
            if kind == "tuple":
                value = tuple(value)
            seen |= bits
            try:
                if ops[type(old.ops[0])](value, before) != ops[type(new.ops[0])](
                    value, after
                ):
                    changed |= bits
            except TypeError:
                changed |= bits
        return seen & ~changed
    return 0


def equivalent(tree, modified, path, observed):
    # Compare whole ASTs after normalizing proven identity comparisons.
    restore = []
    for original, changed in zip(ast.walk(tree), ast.walk(modified)):
        if not isinstance(original, ast.Compare) or not isinstance(
            changed, ast.Compare
        ):
            continue
        if len(original.ops) != 1 or len(changed.ops) != 1:
            continue
        pair = type(original.ops[0]), type(changed.ops[0])
        if pair not in ((ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)):
            continue
        right = original.comparators[0]
        types = observed.get(f"{path}:{original.lineno}", [])
        if not types:
            continue
        if (
            isinstance(right, ast.Constant)
            and right.value is None
            and "custom" not in types
        ):
            restore.append((changed, changed.ops[0]))
            changed.ops[0] = original.ops[0]
        elif isinstance(right, ast.Attribute) and isinstance(right.value, ast.Name):
            if types == ["enum:" + right.value.id]:
                restore.append((changed, changed.ops[0]))
                changed.ops[0] = original.ops[0]
    matched = same(tree, modified)
    for changed, op in restore:
        changed.ops[0] = op
    return matched


def dictionary_hits(path, tree, modified, observed, coverage):
    for old, new in zip(ast.walk(tree), ast.walk(modified)):
        if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
            continue
        site = f"{path}:{old.lineno}:dict"
        if site not in observed or len(old.keys) != len(new.keys):
            continue
        if any(
            ast.dump(a) != ast.dump(b) for a, b in zip(old.keys, new.keys) if a and b
        ):
            continue
        keys = [
            key.value
            for key, a, b in zip(old.keys, old.values, new.values)
            if isinstance(key, ast.Constant) and ast.dump(a) != ast.dump(b)
        ]
        if not keys:
            continue
        original_values = new.values
        new.values = old.values
        isolated = same(tree, modified)
        new.values = original_values
        if isolated:
            mask = coverage.get(site, {}).get(1, 0)
            for key in keys:
                mask |= coverage.get(site + "/" + key, {}).get(1, 0)
            return mask
    return None


def guard_exclusions(path, old_tree, new_tree, removed, tables):
    if not removed:
        return 0
    for a, b in zip(ast.walk(old_tree), ast.walk(new_tree)):
        if not isinstance(a, (ast.If, ast.While, ast.IfExp)) or type(a) is not type(b):
            continue
        old, new = a.test, b.test
        if not (old.lineno <= min(removed) and max(removed) <= old.end_lineno):
            continue
        if not guards.supported(old) or same(old, new):
            continue
        table = tables.get(guards.site(path, old))
        if not table:
            continue
        atoms = [ast.dump(n) for n in guards.leaves(old)]
        if any(ast.dump(n) not in atoms for n in guards.leaves(new)):
            continue
        # Identity comparisons with non-singleton literals can depend on
        # interning across separate compilations.
        if any(
            isinstance(n, ast.Compare)
            and any(
                isinstance(op, (ast.Is, ast.IsNot))
                and not (
                    isinstance(rhs, ast.Constant)
                    and (rhs.value is None or type(rhs.value) is bool)
                )
                for op, rhs in zip(n.ops, n.comparators)
            )
            for n in ast.walk(old)
        ):
            continue
        b.test = old
        isolated = same(old_tree, new_tree)
        b.test = new
        if not isolated:
            continue
        seen = 0
        changed = table.get("?", 0)
        for key, bits in table.items():
            if key == "?":
                continue
            _, pattern = json.loads(key)
            truth = {v: bool(pattern & (1 << i)) for i, v in enumerate(atoms)}
            seen |= bits
            if guards.evaluate(old, truth) != guards.evaluate(new, truth):
                changed |= bits
        return seen & ~changed
    return 0


def branch_exclusions(path, tree, modified, removed, branches, test_mask):
    # Only narrow changes inside a guarded expression in executable function
    # code. Its guard and everything outside the expression must be unchanged.
    in_function = any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.body
        and n.body[0].lineno <= min(removed)
        and max(removed) <= n.end_lineno
        for n in ast.walk(tree)
    )
    if not in_function:
        return 0
    excluded = 0
    for old, new in zip(ast.walk(tree), ast.walk(modified)):
        if type(old) is not type(new) or not guarded(old):
            continue
        options = set(guarded(new))
        for field, i in guarded(old):
            if (field, i) not in options:
                continue
            child = get(old, field, i)
            if not (child.lineno <= min(removed) and max(removed) <= child.end_lineno):
                continue
            key = site(path, old, field, i)
            if key not in branches:
                continue
            replacement = get(new, field, i)
            if same(child, replacement):
                continue
            put(new, field, i, child)
            isolated = same(tree, modified)
            put(new, field, i, replacement)
            if isolated:
                excluded |= test_mask & ~branches[key].get(0, 0)
    return excluded & ~branches.get("?", {}).get(0, 0)


def python_hits(
    path,
    edits,
    coverage,
    all_bits,
    observed,
    replacements,
    domains,
    branches,
    test_mask,
    guard_tables,
    scopes,
    isolated,
):
    lines = source_lines(path)
    updated = update(lines, edits)
    replacements[str((SOURCE / path).resolve())] = updated
    tree = modified = None
    # A same-indent replacement within a recorded function can be parsed with
    # its lexical scope. Retain original columns for the coverage maps.
    if len(edits) == 2 and edits[0][1] == "-" and edits[1][1] == "+":
        pos, _, old = edits[0]
        new = edits[1][2]
        if (
            edits[1][0] == pos + 1
            and old[: len(old) - len(old.lstrip())]
            == new[: len(new) - len(new.lstrip())]
        ):
            span = next(
                (
                    (first, end)
                    for first, (body, end) in scopes.items()
                    if body <= pos + 1 <= end
                ),
                None,
            )
            if span:
                try:
                    tree = local_function(lines, *span)
                    modified = local_function(updated.splitlines(), *span)
                except (SyntaxError, ValueError, IndexError):
                    tree = modified = None
    if tree is None:
        try:
            tree = ast.parse("\n".join(lines))
            modified = ast.parse(updated)
            compile(modified, path, "exec")
        except (ValueError, SyntaxError):
            return all_bits
    foldable = (
        isolated
        and len(edits) == 2
        and any(
            re.search(r"\b(?:True|False) (?:and|or)\b|\b\d+\s*[-+*]\s*\d+\b", text)
            for _, _, text in edits
        )
    )
    if equivalent(tree, modified, path, observed) or (
        foldable and compiled_equal.unchanged(tree, modified)
    ):
        replacements.pop(str((SOURCE / path).resolve()), None)
        return 0
    dict_mask = dictionary_hits(path, tree, modified, observed, coverage)
    if dict_mask is not None:
        return dict_mask
    removed = {pos + 1 for pos, op, _ in edits if op == "-"}
    for pos, op, _ in edits:
        if op == "+" and pos not in removed and pos + 1 not in removed:
            removed.update((max(1, pos), pos + 1))
    mask = 0

    def merge(line):
        return coverage.get(path, {}).get(line, 0)

    for line in removed:
        direct = merge(line)
        mask |= direct
        candidates = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.expr, ast.stmt)) and n.lineno <= line <= n.end_lineno
        ]
        # An edit to a later short-circuit operand can make it reachable even
        # when the original line was skipped.
        for n in candidates:
            if isinstance(n, ast.BoolOp):
                mask |= merge(n.lineno)
        if not direct:
            statements = [n for n in candidates if isinstance(n, ast.stmt)]
            if statements:
                n = min(statements, key=lambda n: n.end_lineno - n.lineno)
                if not isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                    mask |= merge(n.lineno)
    if not mask & test_mask or not removed:
        return mask
    if isolated and any(
        re.search(r"\b(None|True|False)\b", text) for _, _, text in edits
    ):
        mask &= ~operands.exclusions(tree, modified, path, removed, same)
    if isolated and any(re.search(r"==|!=|[<>]|\bis\b", text) for _, _, text in edits):
        mask &= ~paired_values.exclusions(path, tree, modified, removed, same)
    if isolated and any(re.search(r"\b(and|or|not)\b", text) for _, _, text in edits):
        mask &= ~truth_sites.exclusions(path, tree, modified, removed, same)
    if isolated and any("_match(" in text for _, _, text in edits):
        mask &= ~match_inputs.exclusions(path, tree, modified, removed, same)
    if isolated and any(
        re.search(r"(?:[<>=!]=?|\bnot\b).*\d", text) for _, _, text in edits
    ):
        mask &= ~order_profile.exclusions(path, tree, modified, removed, same)
    mask &= ~scalar_exclusions(tree, modified, path, domains)
    mask &= ~guard_exclusions(path, tree, modified, removed, guard_tables)
    return mask & ~branch_exclusions(path, tree, modified, removed, branches, test_mask)


def main(data):
    from profile_store import Store

    store = Store()
    meta = store.meta
    names = meta["names"]
    fixtures = meta["fixtures"]
    offset = len(names)
    detail = {
        "tests": meta["case_records"],
        "cpu": meta["cpu"],
        "uncertain": meta["uncertain"],
    }
    stages = {"tests": meta["stage_records"], "plan_inputs": meta["inputs"]}
    stage_offset = offset + len(detail["tests"])
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    numbers = {t["id"]: t["number"] for t in tests}
    durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    case_records = detail["tests"]
    uncertain = set(detail["uncertain"])
    probe_deadline = time.monotonic() + 80
    stress_gaps = json.loads(Path(__file__).with_name("stress.json").read_bytes())
    prepared_tables = False

    def plan(files, isolated=False):
        mask = 0
        extra = set()
        replacements = {}
        fixture_jobs = {}
        has_fixture = False
        for path, edits in files.items():
            if path.endswith(".py") and (SOURCE / path).is_file():
                bucket = store.file(path)
                if "branches" in bucket:
                    bucket["branches"]["?"] = (
                        store.file("?").get("branches", {}).get("?", {})
                    )
                selection = python_hits(
                    path,
                    edits,
                    bucket.get("lines", {}),
                    (1 << meta["bits"]) - 1,
                    meta["types"],
                    replacements,
                    bucket.get("values", {}),
                    bucket.get("branches", {}),
                    (1 << offset) - 1,
                    bucket.get("guards", {}),
                    bucket.get("scopes", {}),
                    isolated,
                )
                mask |= selection
                if path.startswith("tests/") and selection:
                    extra.update(t["id"] for t in tests if t["file"] == path)
            elif path in fixtures:
                has_fixture = True
                if path.endswith(".sql") and (SOURCE / path).is_file():
                    content = update((SOURCE / path).read_text().splitlines(), edits)
                    replacements[str(SOURCE / path)] = content
                if path in OPT_FILES.values() and len(files) == 1:
                    try:
                        jobs = changed_pairs(SOURCE, path, content)
                    except (ValueError, IndexError):
                        jobs = None
                    if jobs is not None:
                        replacements[str(SOURCE / path)] = content
                        fixture_jobs = {n: v for n, v in jobs.items() if v}
                        extra.update(fixture_jobs)
                        continue
                if (
                    len(files) == 1
                    and path.startswith("tests/fixtures/")
                    and path.endswith(".sql")
                ):
                    relative = path.removeprefix("tests/fixtures/")
                    fixture_bits = fixtures[path]
                    consumers = [
                        n for i, n in enumerate(names) if (fixture_bits >> i) & 1
                    ]
                    profiled = {}
                    for name in consumers:
                        entry = meta["groups"].get(name, {}).get("rows")
                        if entry:
                            paths = {f for f, _ in entry[2]}
                            if relative in paths:
                                profiled[name] = paths
                    if profiled:
                        try:
                            versions = pair_versions(SOURCE, path, content)
                        except (ValueError, IndexError):
                            versions = None
                        if versions is not None:
                            old, new = versions
                            changed = {
                                i
                                for i, pair in enumerate(zip(old, new))
                                if pair[0] != pair[1]
                            }
                            for name in consumers:
                                if name not in profiled:
                                    extra.add(name)
                                elif changed:
                                    extra.add(name)
                                    if any(old[i][:2] != new[i][:2] for i in changed):
                                        allowed = {f: set() for f in profiled[name]}
                                        allowed[relative] = changed
                                        fixture_jobs[name] = {"rows": allowed}
                            continue
                mask |= fixtures[path]
        affected = {
            n for i, n in enumerate(names) if (mask >> i) & 1 and n in numbers
        } | extra
        indices = {}
        estimated = {}
        for i, (name, index) in enumerate(case_records):
            if name in affected and ((mask >> (offset + i)) & 1 or i in uncertain):
                indices.setdefault(name, []).append(index)
                estimated[name] = estimated.get(name, 0) + detail["cpu"][i]
        jobs = []
        if fixture_jobs:
            jobs = list(fixture_jobs.items())
        elif replacements and all(p.endswith(".py") for p in files):
            cost = sum(durations[numbers[n]] for n in affected)
            # Checking a small entire selection can save a worker's fixed charge.
            if cost < 1700:
                jobs = [
                    (n, None)
                    for n in sorted(affected, key=lambda n: durations[numbers[n]])
                ]
            for n, ids in indices.items():
                if estimated[n] < min(5.0, durations[numbers[n]] / 1800):
                    jobs = [job for job in jobs if job[0] != n]
                    jobs.insert(0, (n, ids))
            stage_jobs = {}
            external = set()
            covered_cases = {}
            for i, (n, index, stage, digest, cost) in enumerate(stages["tests"]):
                if n not in affected or not (mask >> (stage_offset + i)) & 1:
                    continue
                if stage in ("setup", "outside"):
                    external.add(n)
                else:
                    covered_cases.setdefault(n, set()).add(index)
                    stage_jobs.setdefault(n, []).append((index, stage, digest, cost))
            for n, checks in stage_jobs.items():
                if n in estimated and estimated[n] < 0.85:
                    continue
                if n in external or not set(indices.get(n, [])) <= covered_cases[n]:
                    continue
                if sum(c[3] for c in checks) > 5.5:
                    continue
                # Probe the most exposed iterations first, so an actual change
                # stops confirmation before paying for the rest of the suite.
                checks.sort(key=lambda c: c[3])
                jobs = [job for job in jobs if job[0] != n]
                inputs = {
                    str(i): stages["plan_inputs"][f"{n}/{i}"]
                    for i, stage, _, _ in checks
                    if stage == "plan"
                }
                jobs.insert(
                    0, (n, {"stages": [c[:3] for c in checks], "inputs": inputs})
                )
            if jobs:
                planned = {n for n, _ in jobs}
                rest = sorted(affected - planned, key=lambda n: durations[numbers[n]])
                if len(rest) <= 40 and sum(durations[numbers[n]] for n in rest) < 900:
                    jobs.extend((n, None) for n in rest)
        elif has_fixture and len(files) > 1 and len(replacements) == len(files):
            cost = sum(durations[numbers[n]] for n in affected)
            if cost < 1000 and len(affected) <= 24:
                jobs = [
                    (n, None)
                    for n in sorted(affected, key=lambda n: durations[numbers[n]])
                ]
        # Estimate actual probe work after restricting independent SQL assertions.
        # Keep the original whole-test jobs for unattributed coverage.
        pure_code = (
            bool(replacements)
            and not has_fixture
            and all(p.endswith(".py") for p in files)
        )
        if pure_code and affected:
            candidates = dict(jobs)
            estimates = {n: durations[numbers[n]] for n in affected}
            parse_only = all(
                p.endswith(("/parser.py", "/tokenizer_core.py", "/tokens.py"))
                or "/parsers/" in p
                for p in files
            )
            for n in affected:
                for kind, (start, count, records, positions) in (
                    meta["groups"].get(n, {}).items()
                ):
                    bits = (mask >> start) & ((1 << count) - 1)
                    outside = (mask >> (start + count)) & 1
                    if not bits:
                        continue
                    selected = [i for i in range(count) if (bits >> i) & 1]
                    fraction = len(selected) / count
                    if kind == "engine":
                        continue
                    if kind == "boundary":
                        if not parse_only or outside or len(selected) > 500:
                            continue
                        if n not in candidates or estimates[n] > 750:
                            candidates[n] = {
                                "parse_calls": [records[i] for i in selected],
                                "positions": positions,
                            }
                            estimates[n] = min(estimates[n], max(40, len(selected) * 2))
                        continue
                    if kind == "calls" and fraction < 0.85:
                        included = {records[i] for i in selected}
                        payload = set(records) - included
                    elif kind == "rows" and fraction < 0.85:
                        payload = {f: set() for f, j in records}
                        for i in selected:
                            f, j = records[i]
                            payload[f].add(j)
                    else:
                        continue
                    estimate = max(
                        8,
                        durations[numbers[n]]
                        * (fraction + (0.13 if outside else 0.025)),
                    )
                    if estimate < estimates[n]:
                        estimates[n] = estimate
                        candidates[n] = {kind: payload}
            for n in affected:
                entry = meta["groups"].get(n, {}).get("engine")
                if not entry or durations[numbers[n]] < 3500:
                    continue
                start, count, records, _ = entry
                if (mask >> (start + count)) & 1:
                    continue
                kernel = (mask >> start) & ((1 << count) - 1)
                # If every relevant iteration touches row-level code, a matching
                # upstream plan alone cannot certify that the result is unchanged.
                ids = indices.get(n, list(range(count)))
                if not ids or all((kernel >> i) & 1 for i in ids):
                    continue
                engine_checks = [(i, *records[i], bool((kernel >> i) & 1)) for i in ids]
                prior = candidates.get(n)
                if isinstance(prior, dict) and "parse_calls" in prior:
                    continue
                if isinstance(prior, dict) and "stages" in prior:
                    prior["engine"] = engine_checks
                elif prior is None or estimates[n] > 1200:
                    candidates[n] = {"engine": engine_checks}
                else:
                    continue
                estimates[n] = min(estimates[n], max(60, 25 * len(ids)))
            modules = {}
            for filename in replacements:
                relative = Path(filename).relative_to(SOURCE).as_posix()
                if not relative.startswith("sqlglot/optimizer/"):
                    break
                spans = store.file(relative).get("scopes", {})
                removed = {pos + 1 for pos, op, _ in files[relative] if op == "-"}
                if not removed or any(
                    not any(
                        first < line and body <= line <= end
                        for first, (body, end) in spans.items()
                    )
                    for line in removed
                ):
                    break
                if any(
                    op == "+" and pos not in removed and pos + 1 not in removed
                    for pos, op, _ in files[relative]
                ):
                    break
                locations = None
                if len(files[relative]) == 2 and len(removed) == 1:
                    line = next(iter(removed))
                    span = next(
                        (
                            (first, end)
                            for first, (body, end) in spans.items()
                            if body <= line <= end
                        ),
                        None,
                    )
                    if span:
                        try:
                            tree = local_function(source_lines(relative), *span)
                            locations = {line}
                            for node in ast.walk(tree):
                                if (
                                    isinstance(node, (ast.expr, ast.stmt))
                                    and not isinstance(
                                        node, (ast.FunctionDef, ast.ClassDef)
                                    )
                                    and node.lineno <= line <= node.end_lineno
                                ):
                                    if (
                                        isinstance(node, ast.expr)
                                        or node.lineno == line
                                    ):
                                        locations.add(node.lineno)
                            # A continuation may change short-circuit reachability.
                            if not any(
                                node.lineno == line
                                for node in ast.walk(tree)
                                if isinstance(node, (ast.expr, ast.stmt))
                            ):
                                locations = None
                        except (SyntaxError, ValueError, IndexError):
                            pass
                modules[relative[:-3].replace("/", ".")] = locations
            else:
                if modules:
                    for name in affected:
                        record = rule_replay.catalog().get(name, {})
                        if all(m in record for m in modules):
                            entries = rule_replay.affected_entries(record, modules)
                            limit = 64 if durations[numbers[name]] > 5000 else 16
                            if len(entries) <= limit and (
                                not entries or 5 * len(entries) < estimates[name]
                            ):
                                candidates[name] = {
                                    "rules": entries,
                                    "fallback": candidates.get(name),
                                }
                                estimates[name] = min(
                                    estimates[name], max(1, 5 * len(entries))
                                )
            # The checkpoint trace covers optimizer function bodies. Include
            # the function's entry lines so short-circuited edits still replay.
            if modules and len(modules) == len(replacements) and len(affected) < 250:
                locations = {}
                for filename in replacements:
                    relative = Path(filename).relative_to(SOURCE).as_posix()
                    spans = store.file(relative).get("scopes", {})
                    lines = {pos + 1 for pos, op, _ in files[relative] if op == "-"}
                    probes = set(lines)
                    for line in lines:
                        owners = [
                            (end - start, start, body, end)
                            for start, (body, end) in spans.items()
                            if body <= line <= end
                        ]
                        if owners:
                            _, start, body, end = min(owners)
                            probes.update(
                                checkpoint_lines(relative, files[relative], start, end)
                            )
                    locations[relative] = probes
                for name, spec in candidates.items():
                    if isinstance(spec, dict):
                        fallback = spec.get("fallback") if "rules" in spec else spec
                        if (
                            isinstance(fallback, dict)
                            and "stages" in fallback
                            and any(
                                phase == "optimize"
                                for _, phase, _ in fallback["stages"]
                            )
                        ):
                            fallback["locations"] = locations
                for name in affected:
                    if (
                        "rows" in meta["groups"].get(name, {})
                        and durations[numbers[name]] >= 350
                    ):
                        previous = candidates.get(name)
                        if isinstance(previous, dict) and "rules" in previous:
                            continue
                        candidates[name] = {
                            "checkpoint": locations,
                            "fallback": previous,
                        }
                        estimates[name] = min(
                            estimates[name], max(40, durations[numbers[name]] // 4)
                        )
            if stress.NAME in affected and stress.checkable(files):
                # Retain the full test if the pristine full workload reached
                # source paths missing from either of the reduced workloads.
                if all(not stress_gaps.get(p) for p in files):
                    candidates[stress.NAME] = {"stress": True}
                    estimates[stress.NAME] = 300
            if isolated and len(files) == 1:
                path, edits = next(iter(files.items()))
                if (
                    path.startswith("sqlglot/")
                    and len(edits) == 2
                    and edits[0][1] == "-"
                    and edits[1][:2] == (edits[0][0] + 1, "+")
                ):
                    line = edits[0][0] + 1
                    owners = [
                        (end - start, start, end)
                        for start, (body, end) in store.file(path)
                        .get("scopes", {})
                        .items()
                        if start < line and body <= line <= end
                    ]
                    if owners and reference_sql.NAME in affected:
                        _, start, end = min(owners)
                        lines = checkpoint_lines(path, edits, start, end)
                        ids = reference_sql.plan(path, lines)
                        if ids is not None:
                            candidates[reference_sql.NAME] = {"reference_sql": ids}
                            estimates[reference_sql.NAME] = 5 + 3 * len(ids)
                    for n in affected & {
                        "tests/test_parser.py::TestParser::test_max_nodes",
                        "tests/test_optimizer.py::TestOptimizer::test_scope",
                    }:
                        if not owners:
                            continue
                        _, start, end = min(owners)
                        block_plan = test_blocks.plan(
                            n, {path: checkpoint_lines(path, edits, start, end)}
                        )
                        if block_plan:
                            ids, cpu = block_plan
                            estimate = max(10, round(cpu * 1000))
                            if estimate < min(1200, estimates[n]):
                                candidates[n] = {"blocks": ids}
                                estimates[n] = estimate
            independent = {
                name
                for name, spec in candidates.items()
                if isinstance(spec, dict)
                and any(
                    key in spec and spec[key] == []
                    for key in ("reference_sql", "rules", "blocks")
                )
            }
            affected.difference_update(independent)
            for name in independent:
                candidates.pop(name)
                estimates.pop(name)
            if not affected:
                return set(), {}, replacements
            remaining = sum(estimates.values())
            full_budget = max(
                3500, min(6500, 0.8 * max(durations[numbers[n]] for n in affected))
            )
            if remaining < full_budget:
                for n in affected:
                    candidates.setdefault(n, None)
            # For larger selections, probe independent expensive tests where
            # the estimated verification cost is well below the worker load.
            jobs = [
                (n, plan)
                for n, plan in candidates.items()
                if remaining < full_budget
                or n in dict(jobs)
                or estimates[n] < durations[numbers[n]] * 0.55
            ]
            jobs.sort(
                key=lambda pair: (
                    -durations[numbers[pair[0]]]
                    if durations[numbers[pair[0]]] > 5000
                    else estimates[pair[0]] / max(1, durations[numbers[pair[0]]])
                )
            )
        return affected, dict(jobs), replacements

    for patch in sorted((data / "commits").glob("*.patch")):
        files = changes(patch.read_text())
        python_files = {p: edits for p, edits in files.items() if p.endswith(".py")}
        fixture_files = {
            p: edits
            for p, edits in files.items()
            if p in fixtures and p.endswith(".sql")
        }
        if (
            fixture_files
            and len(files) > 1
            and len(python_files) + len(fixture_files) == len(files)
        ):
            # Each fixture contributes its own assertions. Run every chosen
            # probe with the complete patch mounted, including the Python edits.
            affected, planned, replacements = (
                plan(python_files) if python_files else (set(), {}, {})
            )
            for path, edits in fixture_files.items():
                impacted, fixture_plans, overlay = plan({path: edits})
                for name in impacted:
                    if name not in affected:
                        if name in fixture_plans:
                            planned[name] = fixture_plans[name]
                    elif isinstance(planned.get(name), list) and isinstance(
                        fixture_plans.get(name), list
                    ):
                        planned[name] = sorted(
                            set(planned[name]) | set(fixture_plans[name])
                        )
                    elif durations[numbers[name]] < 1800:
                        planned[name] = None
                    else:
                        planned.pop(name, None)
                affected.update(impacted)
                replacements.update(overlay)
        else:
            affected, planned, replacements = plan(files, len(files) == 1)
        if (
            python_files
            and len(affected) < len(numbers)
            and import_check.broken(SOURCE, python_files, replacements)
        ):
            print(
                json.dumps(
                    dict(
                        commit=patch.stem,
                        workers=schedule(list(numbers.values()), durations),
                    ),
                    separators=(",", ":"),
                )
            )
            continue
        jobs = list(planned.items())
        remaining_time = probe_deadline - time.monotonic()
        if jobs and remaining_time > 0.3:
            budget = 5.0 if any(ids is not None for _, ids in jobs) else 2.5
            if not prepared_tables and any(
                n.startswith(("tests/test_executor.", "tests/test_optimizer."))
                for n, job in jobs
            ):
                # Share the third-party dataframe import between child probes.
                # SQLGlot and its tests still import the actual patch in each child.
                import importlib

                importlib.import_module("pandas")
                prepared_tables = True
            current_schedule = schedule([numbers[n] for n in affected], durations)
            current_cost = 2000 * len(current_schedule) + max(
                (sum(durations[t] for t in w) for w in current_schedule), default=0
            )
            fixed = affected - planned.keys()

            def minimum_cost(protected):
                if not protected:
                    return 0
                required = [durations[numbers[name]] for name in protected]
                total, longest = sum(required), max(required)
                return min(
                    2000 * k + max(longest, (total + k - 1) // k)
                    for k in range(1, min(8, len(required)) + 1)
                )

            floor = minimum_cost(fixed)

            def done(passed, failed):
                bins = schedule([numbers[n] for n in affected - passed], durations)
                cost = 2000 * len(bins) + max(
                    (sum(durations[i] for i in worker) for worker in bins), default=0
                )
                return cost <= minimum_cost(fixed | failed)

            # Even if every probe passes, worker time cannot go below this floor.
            budget = min(budget, (current_cost - floor) / 1000)
            anchors = {
                n
                for n, _ in jobs
                if current_cost - (durations[numbers[n]] + 2000)
                <= (2000 if durations[numbers[n]] >= 5000 else 200)
            }
            if budget > 0.04:
                affected -= check(
                    SOURCE,
                    replacements,
                    jobs,
                    min(budget, remaining_time),
                    anchors,
                    done,
                )
        selected = [numbers[n] for n in affected]
        print(
            json.dumps(
                dict(commit=patch.stem, workers=schedule(selected, durations)),
                separators=(",", ":"),
            )
        )


if __name__ == "__main__":
    SOURCE = (Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/src")).resolve()
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/data"))
