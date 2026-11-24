"""Typed, bounded, eval-free rule DSL evaluator (spec 4.1), owned by the compliance engine.

Three-valued logic: TRUE / FALSE / UNKNOWN. UNKNOWN is never PASS.
Operators: all any not eq in gt gte lt lte exists between sum_by ratio abs effective_on.
Operand convention: a bare string that is a registered field name is a field reference; explicit forms
{"field": name} and {"const": value} are also accepted. The FIRST operand of a comparison must be a field or an
expression (so a typo cannot silently turn into a literal). Floats are rejected. Units come from the field registry
and comparing different units is an error (no currency blending).
Bounds: depth, node count, row count and wall-clock budget; exceeding any raises DslError (=> status ERROR).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from app.core.money import NumericError, dstr, to_decimal

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
BOOL_OPS = {"all", "any", "not", "eq", "in", "gt", "gte", "lt", "lte", "exists", "between", "effective_on"}
VALUE_OPS = {"abs", "ratio", "sum_by"}
CMP_OPS = {"eq", "gt", "gte", "lt", "lte"}
MAX_DEPTH, MAX_NODES, MAX_ROWS, MAX_SECONDS = 12, 400, 10_000, 1.0


class DslError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


# Approved field registry: type + unit + sign convention. Money fields are always non-negative amounts.
FIELD_REGISTRY: dict[str, dict] = {
    "market_type": {"type": "str"}, "venue": {"type": "str"}, "jurisdiction": {"type": "str"},
    "account_id": {"type": "str"}, "currency": {"type": "str"}, "client_classification": {"type": "str"},
    "position_basis": {"type": "str"}, "terms_status": {"type": "str"}, "reconciliation_status": {"type": "str"},
    "required_margin_inr": {"type": "num", "unit": "INR"}, "eligible_collateral_inr": {"type": "num", "unit": "INR"},
    "required_margin_usd": {"type": "num", "unit": "USD"}, "eligible_collateral_usd": {"type": "num", "unit": "USD"},
    "position_lots": {"type": "num", "unit": "lots"}, "source_age_hours": {"type": "num", "unit": "hours"},
    "as_of": {"type": "ts"}, "agreement_effective_from": {"type": "ts"}, "agreement_effective_to": {"type": "ts"},
    "positions": {"type": "rows"},
}


def parse_ts(v) -> datetime:
    if isinstance(v, datetime):
        d = v
    else:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise DslError("BAD_TIMESTAMP", "naive timestamps are not accepted")
    return d.astimezone(UTC)


def validate(ast, registry: dict | None = None) -> set[str]:
    """Compile-time check: known ops/fields, bounded size/depth, arity. Returns referenced field names."""
    reg = registry or FIELD_REGISTRY
    fields: set[str] = set()
    count = 0

    def walk(node, depth: int, first: bool):
        nonlocal count
        count += 1
        if depth > MAX_DEPTH:
            raise DslError("TOO_DEEP", f"nesting exceeds {MAX_DEPTH}")
        if count > MAX_NODES:
            raise DslError("TOO_LARGE", f"more than {MAX_NODES} nodes")
        if isinstance(node, dict):
            if len(node) != 1:
                raise DslError("BAD_NODE", f"node must have exactly one key: {list(node)}")
            (op, arg), = node.items()
            if op == "field":
                if arg not in reg:
                    raise DslError("UNKNOWN_FIELD", f"{arg!r} is not in the approved field registry")
                fields.add(arg)
                return
            if op == "const":
                _lit(arg)
                return
            if op not in BOOL_OPS and op not in VALUE_OPS:
                raise DslError("UNSUPPORTED_OP", f"operator {op!r} is not allowed (no arbitrary expressions)")
            if not isinstance(arg, list):
                raise DslError("BAD_ARGS", f"{op} expects a list of operands")
            arity = {"not": (1, 1), "exists": (1, 1), "abs": (1, 1), "ratio": (2, 2), "between": (3, 3),
                     "in": (2, 2), "effective_on": (2, 2), "sum_by": (2, 4)}.get(op, (1, 10_000) if op in ("all", "any") else (2, 2))
            if not arity[0] <= len(arg) <= arity[1]:
                raise DslError("BAD_ARITY", f"{op} takes {arity[0]}..{arity[1]} operands, got {len(arg)}")
            if op in ("all", "any", "not"):
                for a in arg:
                    walk(a, depth + 1, False)
            elif op == "effective_on":
                for a in arg:
                    if a is not None:
                        _operand_check(a, reg, fields, depth + 1, walk, first=False)
            elif op == "sum_by":
                if arg[0] not in reg or reg[arg[0]]["type"] != "rows":
                    raise DslError("BAD_SUM_BY", "first operand must be a registered rows field")
                fields.add(arg[0])
            elif op == "in":
                _operand_check(arg[0], reg, fields, depth + 1, walk, first=True)
                if not isinstance(arg[1], list) or not arg[1]:
                    raise DslError("BAD_ARGS", "'in' needs a non-empty literal list")
                for x in arg[1]:
                    _lit(x)
            else:
                for i, a in enumerate(arg):
                    _operand_check(a, reg, fields, depth + 1, walk, first=(i == 0 and op != "ratio"))
        else:
            raise DslError("BAD_NODE", f"unexpected {type(node).__name__} where an operator node was required")

    walk(ast, 0, False)
    return fields


def _lit(x):
    if isinstance(x, float):
        raise DslError("FLOAT_LITERAL", "float literals are not allowed; use a decimal string or integer")
    if isinstance(x, (dict, list)):
        raise DslError("BAD_LITERAL", "literals must be scalars")


def _operand_check(a, reg, fields, depth, walk, first: bool):
    if isinstance(a, dict):
        walk(a, depth, first)
    elif isinstance(a, str) and a in reg:
        fields.add(a)
    elif first:
        raise DslError("UNKNOWN_FIELD", f"first operand {a!r} must be an approved field or expression")
    else:
        _lit(a)


class Evaluator:
    def __init__(self, facts: dict, as_of: datetime, registry: dict | None = None):
        self.facts, self.as_of, self.reg = facts, as_of, registry or FIELD_REGISTRY
        self.trace: list[dict] = []
        self._t0 = time.monotonic()
        self._steps = 0

    # -- helpers
    def _tick(self):
        self._steps += 1
        if self._steps % 16 == 0 and time.monotonic() - self._t0 > MAX_SECONDS:
            raise DslError("TIME_BUDGET", "evaluation exceeded its wall-clock budget")

    def _field(self, name: str):
        spec = self.reg[name]
        if name == "as_of":
            return self.as_of, None, name
        raw = self.facts.get(name)
        if raw is None:
            return None, spec.get("unit"), name
        t = spec["type"]
        try:
            if t == "num":
                return to_decimal(raw, field=name), spec.get("unit"), name
            if t == "ts":
                return parse_ts(raw), None, name
            if t == "rows":
                if not isinstance(raw, list):
                    raise DslError("BAD_FACT", f"{name} must be a list of rows")
                if len(raw) > MAX_ROWS:
                    raise DslError("TOO_MANY_ROWS", f"{name} exceeds {MAX_ROWS} rows")
                return raw, None, name
            return str(raw), None, name
        except NumericError as exc:
            raise DslError("BAD_FACT", str(exc)) from exc

    def _operand(self, a):
        """-> (value|None, unit, name|None)"""
        self._tick()
        if isinstance(a, dict):
            (op, arg), = a.items()
            if op == "field":
                return self._field(arg)
            if op == "const":
                return self._const(arg)
            return self.value(a)
        if isinstance(a, str) and a in self.reg:
            return self._field(a)
        return self._const(a)

    @staticmethod
    def _const(x):
        if isinstance(x, bool) or x is None:
            return x, None, None
        if isinstance(x, (int, Decimal)):
            return Decimal(x), None, None
        if isinstance(x, str):
            return x, None, None
        raise DslError("BAD_LITERAL", f"unsupported literal {x!r}")

    # -- value operators
    def value(self, node):
        (op, args), = node.items()
        if op == "abs":
            v, u, _ = self._operand(args[0])
            return (None if v is None else abs(self._num(v)), u, None)
        if op == "ratio":
            a, ua, _ = self._operand(args[0])
            b, ub, _ = self._operand(args[1])
            if a is None or b is None:
                return None, "ratio", None
            if ua and ub and ua != ub:
                raise DslError("UNIT_MISMATCH", f"ratio of {ua} and {ub}")
            if self._num(b) == 0:
                return None, "ratio", None
            return self._num(a) / self._num(b), "ratio", None
        if op == "sum_by":
            rows, _, _ = self._field(args[0])
            if rows is None:
                return None, None, None
            key = args[1]
            gk = args[2] if len(args) > 2 else None
            gv = args[3] if len(args) > 3 else None
            total = Decimal(0)
            for r in rows:
                if gk is not None and r.get(gk) != gv:
                    continue
                if r.get(key) is None:
                    return None, None, None  # a missing row value makes the sum UNKNOWN, never zero
                try:
                    total += to_decimal(r[key], field=key)
                except NumericError as exc:
                    raise DslError("BAD_FACT", str(exc)) from exc
            return total, None, None
        raise DslError("UNSUPPORTED_OP", op)

    @staticmethod
    def _num(v) -> Decimal:
        if isinstance(v, Decimal):
            return v
        try:
            return to_decimal(v)
        except NumericError as exc:
            raise DslError("TYPE_ERROR", f"non-numeric operand {v!r}") from exc

    # -- boolean operators
    def eval(self, node, depth: int = 0) -> str:
        self._tick()
        (op, args), = node.items()
        if op == "all":
            rs = [self.eval(a, depth + 1) for a in args]
            return FALSE if FALSE in rs else UNKNOWN if UNKNOWN in rs else TRUE
        if op == "any":
            rs = [self.eval(a, depth + 1) for a in args]
            return TRUE if TRUE in rs else UNKNOWN if UNKNOWN in rs else FALSE
        if op == "not":
            r = self.eval(args[0], depth + 1)
            return {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}[r]
        if op == "exists":
            v, _, name = self._operand(args[0])
            res = TRUE if v is not None else FALSE
            self.trace.append({"op": op, "field": name, "result": res})
            return res
        if op == "effective_on":
            lo, _, _ = self._operand(args[0]) if args[0] is not None else (None, None, None)
            hi, _, _ = self._operand(args[1]) if args[1] is not None else (None, None, None)
            if args[0] is not None and lo is None:
                res = UNKNOWN
            elif args[1] is not None and hi is None:
                res = UNKNOWN
            else:
                lo_t = parse_ts(lo) if lo is not None else None
                hi_t = parse_ts(hi) if hi is not None else None
                res = TRUE if (lo_t is None or lo_t <= self.as_of) and (hi_t is None or self.as_of < hi_t) else FALSE
            self.trace.append({"op": op, "as_of": self.as_of.isoformat(), "result": res})
            return res
        if op == "in":
            v, _, name = self._operand(args[0])
            res = UNKNOWN if v is None else TRUE if v in [self._const(x)[0] for x in args[1]] else FALSE
            self.trace.append({"op": op, "field": name, "left": _s(v), "result": res})
            return res
        if op == "between":
            v, u, name = self._operand(args[0])
            lo, ul, _ = self._operand(args[1])
            hi, uh, _ = self._operand(args[2])
            for uu in (ul, uh):
                if u and uu and u != uu:
                    raise DslError("UNIT_MISMATCH", f"{u} vs {uu}")
            if v is None or lo is None or hi is None:
                res = UNKNOWN
            else:
                res = TRUE if self._num(lo) <= self._num(v) <= self._num(hi) else FALSE
            self.trace.append({"op": op, "field": name, "left": _s(v), "result": res})
            return res
        if op in CMP_OPS:
            l, ul, ln = self._operand(args[0])
            r, ur, rn = self._operand(args[1])
            if ul and ur and ul != ur:
                raise DslError("UNIT_MISMATCH", f"cannot compare {ul} with {ur} ({ln} vs {rn}); no implicit blending")
            if l is None or r is None:
                res = UNKNOWN
            elif op == "eq":
                if isinstance(l, Decimal) or isinstance(r, Decimal):
                    res = TRUE if self._num(l) == self._num(r) else FALSE
                else:
                    res = TRUE if l == r else FALSE
            else:
                a, b = self._num(l), self._num(r)
                res = TRUE if {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op] else FALSE
            self.trace.append({"op": op, "left_name": ln, "right_name": rn, "left": _s(l), "right": _s(r),
                               "unit": ul or ur, "result": res})
            return res
        raise DslError("UNSUPPORTED_OP", op)


def _s(v):
    if isinstance(v, Decimal):
        return dstr(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, list):
        return f"<{len(v)} rows>"
    return v


def evaluate_tri(ast, facts: dict, as_of: datetime, registry: dict | None = None) -> tuple[str, list[dict]]:
    ev = Evaluator(facts, as_of, registry)
    return ev.eval(ast), ev.trace
