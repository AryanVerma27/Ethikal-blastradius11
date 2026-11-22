"""Check the input to the unchanged Python row engine before paying to run it."""

import hashlib
import json
import pickle
from stages import plan_state
from cases import TABLES, fixture_cases, run_cases


def plan_key(plan):
    state = plan_state(plan)
    pending = [state]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            if len(node) == 7 and node[0] == "node" and isinstance(node[-1], dict):
                node[-1] = {
                    k: v
                    for k, v in node[-1].items()
                    if k not in ("line", "col", "start", "end")
                } or None
            pending.extend(node)
        elif isinstance(node, dict):
            pending.extend(node.values())
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReachedEngine(Exception):
    pass


def check(name, checks):
    from unittest import mock
    from sqlglot import parse_one, transpile, find_tables
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table
    from sqlglot.executor.python import PythonExecutor
    from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA, load_sql_fixture_pairs

    schema = TPCH_SCHEMA if name.endswith("_tpch") else TPCDS_SCHEMA
    filename = TABLES[name][1].removeprefix("tests/fixtures/")
    rows = list(load_sql_fixture_pairs(filename))
    tables = {n: Table(columns=cols, rows=[]) for n, cols in schema.items()}

    def capture(self, plan, *a, **kw):
        raise ReachedEngine(plan_key(plan))

    residual = []
    with mock.patch.object(PythonExecutor, "execute", capture):
        for index, reference, plan, require_data in checks:
            if require_data:
                residual.append(index)
                continue
            meta, sql, _ = rows[index]
            expression = pickle.loads(pickle.dumps(parse_one(sql)))
            if not meta.get("execute"):
                continue
            if transpile(sql, write="duckdb")[0] != reference:
                residual.append(index)
                continue
            try:
                subset = {t.name: tables[t.name] for t in find_tables(expression)}
                execute(expression, schema=schema, tables=subset)
            except ReachedEngine as reached:
                if reached.args[0] != plan:
                    residual.append(index)
            else:
                residual.append(index)
    return residual
