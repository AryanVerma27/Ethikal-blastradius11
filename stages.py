"""Replay SQLGlot transformations at the input to expensive TPC execution."""

import hashlib
import base64
import zlib
import json
import pickle


def encode_expr(value):
    from sqlglot import exp

    return _encode(value, exp)


def _encode(value, exp):
    if isinstance(value, exp.Expr):
        typ = "self" if value._type is value else _encode(value._type, exp)
        return [
            "node",
            type(value).__module__,
            type(value).__qualname__,
            [[k, _encode(v, exp)] for k, v in value.args.items()],
            typ,
            value.comments,
            value._meta,
        ]
    if isinstance(value, exp.DType):
        return ["dtype", value.value]
    if isinstance(value, list):
        return ["list", [_encode(v, exp) for v in value]]
    return value


def decode_expr(value):
    from sqlglot import exp
    import importlib

    if not isinstance(value, list):
        return value
    if value[0] == "dtype":
        return exp.DType(value[1])
    if value[0] == "list":
        return [decode_expr(v) for v in value[1]]
    _, module, cls, args, typ, comments, meta = value
    node = getattr(importlib.import_module(module), cls)(
        **{k: decode_expr(v) for k, v in args}
    )
    node._type = node if typ == "self" else decode_expr(typ)
    node.comments = comments
    node._meta = meta
    return node


def plan_state(plan):
    from sqlglot import exp
    from sqlglot.planner import Step

    def encode(value):
        if isinstance(value, Step):
            fields = {
                k: encode(v)
                for k, v in vars(value).items()
                if k not in ("dependencies", "dependents")
            }
            children = sorted(
                (encode(c) for c in value.dependencies),
                key=lambda r: json.dumps(r, sort_keys=True),
            )
            return [type(value).__name__, fields, children]
        if isinstance(value, exp.Expr):
            return encode_expr(value)
        if isinstance(value, dict):
            return [[encode(k), encode(v)] for k, v in value.items()]
        if isinstance(value, (tuple, list)):
            return [encode(v) for v in value]
        if isinstance(value, type):
            return value.__qualname__
        return value

    return [encode(plan.root), encode(plan.expression)]


def signature(stage, value):
    if stage in ("parse", "optimize"):
        value = encode_expr(value)
    if stage == "plan":
        value = plan_state(value)
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def verify(name, checks, snapshots, on_difference=None, locations=None):
    from sqlglot import parse_one, transpile
    from sqlglot.executor import optimize
    from sqlglot.schema import ensure_schema
    from tests.helpers import TPCH_SCHEMA, TPCDS_SCHEMA, load_sql_fixture_pairs
    from cases import TABLES

    filename = TABLES[name][1].removeprefix("tests/fixtures/")
    inputs = list(load_sql_fixture_pairs(filename))
    schema = TPCH_SCHEMA if name.endswith("_tpch") else TPCDS_SCHEMA
    different = set()
    for index, stage, expected in checks:
        if index in different:
            continue
        if stage == "optimize" and locations:
            from checkpoints import unchanged

            if unchanged(name, index, locations, expected=expected):
                continue
        sql = inputs[index][1]
        if stage == "plan":
            from sqlglot.planner import Plan

            value = Plan(
                decode_expr(
                    json.loads(zlib.decompress(base64.b64decode(snapshots[str(index)])))
                )
            )
        elif stage == "reference":
            value = transpile(sql, write="duckdb")
        else:
            value = parse_one(sql)
            if stage == "optimize":
                value = pickle.loads(pickle.dumps(value))
                value = optimize(
                    value,
                    ensure_schema(schema, dialect=None),
                    leave_tables_isolated=True,
                    dialect=None,
                )
        if signature(stage, value) != expected:
            if on_difference is not None and not different:
                on_difference(index)
            different.add(index)
            if len(different) > 3:
                return None
    return sorted(different)
