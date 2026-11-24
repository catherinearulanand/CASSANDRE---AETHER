"""Typed, bounded, three-valued Rule DSL (JSON AST). No eval, no dynamic imports, no user code, no regex.

Boolean nodes : all, any, not, eq, in, gt, gte, lt, lte, exists, between, effective_on
Value nodes   : {"field": f} | bare string that IS a registry field | {"const": v, "unit": u} | {"param": p}
                | {"abs": x} | {"ratio": [a, b]} | {"sum_by": {"rows": r, "value": f, "group": g, "key": k}}
Evaluation    : TRUE / FALSE / UNKNOWN (None). Missing context => UNKNOWN. UNKNOWN is never PASS.
Units         : every decimal carries a unit; numeric constants WITHOUT a valid unit do not compile (G29).
Limits        : depth, node count, step count, rows and wall-clock are bounded.
"""
from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

from app.core.money import NumericError, to_decimal

UNITS = frozenset({"INR", "USD", "LOTS", "RATIO", "PERCENT", "HOURS", "FLAG", "COUNT"})
MARKETS = ["OTC", "ETD_DOMESTIC", "ETD_IFSC", "CCP_OTC"]
MAX_DEPTH, MAX_NODES, MAX_STEPS, MAX_ROWS, MAX_SECONDS = 8, 200, 20000, 100000, 0.5

# Approved field registry: units and sign conventions are defined HERE and nowhere else.
FIELD_REGISTRY: dict[str, dict] = {
    "market_type": {"type": "string", "enum": MARKETS, "description": "Market type of the subject"},
    "venue": {"type": "string", "description": "Venue code, e.g. DEMO_NSE / DEMO_IFSC"},
    "jurisdiction": {"type": "string", "description": "Jurisdiction code of the subject"},
    "entity_category": {"type": "string", "description": "Participant/entity category"},
    "instrument_class": {"type": "string", "description": "Instrument class"},
    "account_id": {"type": "string", "description": "Account identifier"},
    "currency": {"type": "string", "description": "ISO currency of the subject"},
    "client_classification": {"type": "string", "description": "CLIENT / PROPRIETARY / MEMBER aggregation class"},
    "as_of_date": {"type": "date", "description": "Business date of evaluation (venue calendar)"},
    "required_margin_inr": {"type": "decimal", "unit": "INR", "sign": "non-negative magnitude", "description": "Required margin"},
    "eligible_collateral_inr": {"type": "decimal", "unit": "INR", "sign": "non-negative magnitude", "description": "Eligible POSTED collateral (effective value)"},
    "required_margin_usd": {"type": "decimal", "unit": "USD", "sign": "non-negative magnitude", "description": "Required margin"},
    "eligible_collateral_usd": {"type": "decimal", "unit": "USD", "sign": "non-negative magnitude", "description": "Eligible POSTED collateral (effective value)"},
    "net_position_lots": {"type": "decimal", "unit": "LOTS", "sign": "signed (long +, short -)", "description": "Net position in lots"},
    "gross_position_lots": {"type": "decimal", "unit": "LOTS", "sign": "non-negative magnitude", "description": "Gross position in lots"},
    "source_age_hours": {"type": "decimal", "unit": "HOURS", "sign": "non-negative", "description": "Age of the governing source/market data"},
    "margin_discrepancy_inr": {"type": "decimal", "unit": "INR", "sign": "signed (imported - computed)", "description": "Reconciliation discrepancy"},
    "positions": {"type": "rows", "description": "Position rows (bounded)"},
}
ROW_REGISTRY: dict[str, dict] = {
    "positions": {
        "lots": {"type": "decimal", "unit": "LOTS"}, "account_id": {"type": "string"},
        "instrument_id": {"type": "string"}, "client_classification": {"type": "string"},
    }
}
BINARY = {"eq", "gt", "gte", "lt", "lte"}
BOOL_OPS = BINARY | {"all", "any", "not", "in", "exists", "between", "effective_on"}
VALUE_OPS = {"field", "const", "param", "abs", "ratio", "sum_by"}


class DSLLimitError(Exception):
    pass


def _is_numeric_str(s: str) -> bool:
    try:
        Decimal(s)
        return True
    except Exception:  # noqa: BLE001
        return False


def _as_date(v) -> date | None:
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def normalise_params(params: dict | None) -> tuple[dict, list[dict]]:
    """params: name -> {"value": decimal-string, "unit": UNIT}. Returns (normalised, errors)."""
    out, errs = {}, []
    for name, p in (params or {}).items():
        if not isinstance(p, dict) or "value" not in p:
            errs.append({"path": f"parameters.{name}", "message": "threshold value missing"})
            continue
        unit = p.get("unit")
        if unit not in UNITS:
            errs.append({"path": f"parameters.{name}.unit", "message": f"missing or invalid unit {unit!r}; allowed: {sorted(UNITS)}"})
            continue
        try:
            out[name] = {"value": to_decimal(p["value"], field=name), "unit": unit}
        except NumericError as exc:
            errs.append({"path": f"parameters.{name}.value", "message": str(exc)})
    return out, errs


def params_to_json(params: dict) -> dict:
    return {k: {"value": format(v["value"].normalize(), "f") if v["value"] != 0 else "0", "unit": v["unit"]} for k, v in params.items()}


class _Compiler:
    def __init__(self, params: dict):
        self.params, self.errors, self.deps, self.nodes = params, [], set(), 0

    def err(self, path, msg):
        self.errors.append({"path": path, "message": msg})

    def tick(self, path):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise DSLLimitError(f"more than {MAX_NODES} nodes at {path}")

    # -------- boolean nodes
    def boolean(self, n, path: str, depth: int):
        self.tick(path)
        if depth > MAX_DEPTH:
            self.err(path, f"nesting deeper than {MAX_DEPTH}")
            return
        if not isinstance(n, dict) or len(n) != 1:
            self.err(path, "boolean node must be an object with exactly one operator")
            return
        (op, arg), = n.items()
        if op in ("all", "any"):
            if not isinstance(arg, list) or not arg:
                return self.err(path, f"'{op}' needs a non-empty list")
            for i, c in enumerate(arg):
                self.boolean(c, f"{path}.{op}[{i}]", depth + 1)
        elif op == "not":
            self.boolean(arg, f"{path}.not", depth + 1)
        elif op in BINARY:
            if not isinstance(arg, list) or len(arg) != 2:
                return self.err(path, f"'{op}' needs exactly two operands")
            a = self.value(arg[0], f"{path}.{op}[0]", depth + 1, first=True)
            b = self.value(arg[1], f"{path}.{op}[1]", depth + 1)
            if a and b:
                self.cmp_types(op, a, b, path)
        elif op == "in":
            if not isinstance(arg, list) or len(arg) != 2 or not isinstance(arg[1], list) or not arg[1]:
                return self.err(path, "'in' needs [operand, non-empty list of literals]")
            a = self.value(arg[0], f"{path}.in[0]", depth + 1, first=True)
            if a and a[0] != "string":
                self.err(path, "'in' supports string operands only")
            if not all(isinstance(x, str) for x in arg[1]):
                self.err(path, "'in' list must contain strings")
        elif op == "exists":
            f = arg.get("field") if isinstance(arg, dict) else arg
            if not isinstance(f, str) or f not in FIELD_REGISTRY:
                self.err(path, f"'exists' needs a registry field, got {f!r}")
            else:
                self.deps.add(f)
        elif op == "between":
            if not isinstance(arg, list) or len(arg) != 3:
                return self.err(path, "'between' needs [x, lo, hi]")
            ts = [self.value(a, f"{path}.between[{i}]", depth + 1, first=(i == 0)) for i, a in enumerate(arg)]
            if all(ts):
                for t in ts[1:]:
                    self.cmp_types("gte", ts[0], t, path)
        elif op == "effective_on":
            ok = isinstance(arg, list) and len(arg) == 3 and isinstance(arg[0], (str, dict))
            if not ok:
                return self.err(path, "'effective_on' needs [date_field, from_iso, to_iso|null]")
            t = self.value(arg[0], f"{path}.effective_on[0]", depth + 1)
            if t and t[0] != "date":
                self.err(path, "'effective_on' first operand must be a date field")
            for x in arg[1:]:
                if x is not None and _as_date(x) is None:
                    self.err(path, f"invalid ISO date {x!r}")
        else:
            self.err(path, f"unsupported operator '{op}' (allowed: {sorted(BOOL_OPS)})")

    # -------- value nodes -> (type, unit)
    def value(self, n, path: str, depth: int, first: bool = False):
        self.tick(path)
        if depth > MAX_DEPTH:
            self.err(path, f"nesting deeper than {MAX_DEPTH}")
            return None
        if isinstance(n, bool):
            return ("bool", None)
        if isinstance(n, float):
            self.err(path, "floating-point literals are forbidden; use a decimal string with a unit")
            return None
        if isinstance(n, int):
            self.err(path, "numeric constant requires an explicit unit: use {\"const\": \"<decimal>\", \"unit\": \"<UNIT>\"}")
            return None
        if isinstance(n, str):
            if n in FIELD_REGISTRY:
                return self.field(n, path)
            if first:
                self.err(path, f"unknown field {n!r}; the subject operand must be a field in the approved registry")
                return None
            return ("string", None)
        if isinstance(n, dict) and len(n) in (1, 2) and ("const" in n):
            v, u = n["const"], n.get("unit")
            if isinstance(v, bool):
                return ("bool", None)
            if isinstance(v, float):
                self.err(path, "floating-point constant forbidden")
                return None
            if isinstance(v, (int, str)) and _is_numeric_str(str(v)):
                if u not in UNITS:
                    self.err(path, f"numeric constant has missing/invalid unit {u!r}")
                    return None
                return ("decimal", u)
            if isinstance(v, str):
                return ("string", None)
            self.err(path, "unsupported constant")
            return None
        if not isinstance(n, dict) or len(n) != 1:
            self.err(path, "value node must be a field name, a literal or a single-operator object")
            return None
        (op, arg), = n.items()
        if op == "field":
            if not isinstance(arg, str) or arg not in FIELD_REGISTRY:
                self.err(path, f"unknown field {arg!r}; not in approved registry")
                return None
            return self.field(arg, path)
        if op == "param":
            p = self.params.get(arg)
            if p is None:
                self.err(path, f"parameter {arg!r} is not defined (or has a missing/invalid unit/value)")
                return None
            return ("decimal", p["unit"])
        if op == "abs":
            t = self.value(arg, f"{path}.abs", depth + 1)
            if t and t[0] != "decimal":
                self.err(path, "'abs' needs a decimal operand")
                return None
            return t
        if op == "ratio":
            if not isinstance(arg, list) or len(arg) != 2:
                self.err(path, "'ratio' needs [numerator, denominator]")
                return None
            a, b = (self.value(x, f"{path}.ratio[{i}]", depth + 1) for i, x in enumerate(arg))
            if a and b:
                if a[0] != "decimal" or b[0] != "decimal" or a[1] != b[1]:
                    self.err(path, f"'ratio' needs decimals with identical units, got {a} / {b}")
                    return None
            return ("decimal", "RATIO")
        if op == "sum_by":
            if not isinstance(arg, dict) or arg.get("rows") not in ROW_REGISTRY:
                self.err(path, f"'sum_by.rows' must be one of {sorted(ROW_REGISTRY)}")
                return None
            self.deps.add(arg["rows"])
            reg = ROW_REGISTRY[arg["rows"]]
            vf = reg.get(arg.get("value"))
            if not vf or vf["type"] != "decimal":
                self.err(path, "'sum_by.value' must be a decimal row field")
                return None
            if arg.get("group") is not None and arg.get("group") not in reg:
                self.err(path, "'sum_by.group' not a row field")
            return ("decimal", vf["unit"])
        self.err(path, f"unsupported value operator '{op}'")
        return None

    def field(self, name, path):
        spec = FIELD_REGISTRY[name]
        if spec["type"] == "rows":
            self.err(path, f"rows field '{name}' only usable inside sum_by")
            return None
        self.deps.add(name)
        return (spec["type"], spec.get("unit"))

    def cmp_types(self, op, a, b, path):
        if a[0] == "date" or b[0] == "date":
            if {a[0], b[0]} - {"date", "string"}:
                self.err(path, "date compares only with date/ISO-string")
            return
        if a[0] != b[0]:
            self.err(path, f"type mismatch {a[0]} vs {b[0]}")
        elif a[0] == "decimal" and a[1] != b[1]:
            self.err(path, f"unit mismatch {a[1]} vs {b[1]}: units are never converted implicitly")
        elif a[0] in ("string", "bool") and op != "eq":
            self.err(path, f"'{op}' not supported for {a[0]}")


def compile_rule(scope: dict, condition: dict, parameters: dict | None) -> dict:
    """Validate fields, types, operators, units, dependency graph. Returns a compile report."""
    params, perrs = normalise_params(parameters)
    c = _Compiler(params)
    c.errors.extend(perrs)
    try:
        c.boolean(scope, "scope", 0)
        c.boolean(condition, "condition", 0)
    except DSLLimitError as exc:
        c.err("rule", str(exc))
    return {"ok": not c.errors, "errors": c.errors, "dependencies": sorted(c.deps)}


def node_dependencies(node: dict, parameters: dict | None = None) -> list[str]:
    """Registry fields a single boolean node depends on (used to report exactly which context is missing)."""
    params, _ = normalise_params(parameters)
    c = _Compiler(params)
    try:
        c.boolean(node, "node", 0)
    except DSLLimitError:
        pass
    return sorted(c.deps)


# ------------------------------------------------------------------ evaluation
def _tv(x):
    return "UNKNOWN" if x is None else ("TRUE" if x else "FALSE")


class _Eval:
    def __init__(self, ctx: dict, params: dict):
        self.ctx, self.params, self.steps, self.t0, self.trace = ctx, params, 0, time.monotonic(), []

    def tick(self):
        self.steps += 1
        if self.steps > MAX_STEPS or time.monotonic() - self.t0 > MAX_SECONDS:
            raise DSLLimitError("evaluation budget exceeded")

    def note(self, op, res, detail=None, depth=0):
        self.trace.append({"depth": depth, "op": op, "result": _tv(res), **({"detail": detail} if detail else {})})

    def field(self, name):
        spec, raw = FIELD_REGISTRY[name], self.ctx.get(name)
        if raw is None:
            return None, spec["type"], spec.get("unit")
        try:
            if spec["type"] == "decimal":
                return to_decimal(raw, field=name), "decimal", spec["unit"]
            if spec["type"] == "date":
                d = _as_date(raw)
                return d, "date", None
        except NumericError:
            return None, spec["type"], spec.get("unit")
        return str(raw), spec["type"], spec.get("unit")

    def value(self, n):
        self.tick()
        if isinstance(n, bool):
            return n, "bool", None
        if isinstance(n, str):
            return self.field(n) if n in FIELD_REGISTRY else (n, "string", None)
        if "const" in n:
            v = n["const"]
            if isinstance(v, bool):
                return v, "bool", None
            if isinstance(v, (int, str)) and _is_numeric_str(str(v)):
                return Decimal(str(v)), "decimal", n.get("unit")
            return v, "string", None
        (op, arg), = n.items()
        if op == "field":
            return self.field(arg)
        if op == "param":
            p = self.params[arg]
            return p["value"], "decimal", p["unit"]
        if op == "abs":
            v, t, u = self.value(arg)
            return (None if v is None else abs(v)), t, u
        if op == "ratio":
            a, _, _ = self.value(arg[0])
            b, _, _ = self.value(arg[1])
            if a is None or b is None or b == 0:
                return None, "decimal", "RATIO"
            return a / b, "decimal", "RATIO"
        if op == "sum_by":
            rows = self.ctx.get(arg["rows"])
            if rows is None:
                return None, "decimal", ROW_REGISTRY[arg["rows"]][arg["value"]]["unit"]
            if len(rows) > MAX_ROWS:
                raise DSLLimitError("too many rows")
            total, unit = Decimal(0), ROW_REGISTRY[arg["rows"]][arg["value"]]["unit"]
            for r in rows:
                self.tick()
                if arg.get("group") is not None and "key" in arg and r.get(arg["group"]) != arg["key"]:
                    continue
                raw = r.get(arg["value"])
                if raw is None:
                    return None, "decimal", unit
                try:
                    total += to_decimal(raw)
                except NumericError:
                    return None, "decimal", unit
            return total, "decimal", unit
        raise AssertionError(op)

    def boolean(self, n, depth=0):
        self.tick()
        (op, arg), = n.items()
        if op == "all":
            res, seen_unknown = True, False
            for c in arg:
                r = self.boolean(c, depth + 1)
                if r is False:
                    res = False
                    break
                if r is None:
                    seen_unknown = True
            if res and seen_unknown:
                res = None
        elif op == "any":
            res, seen_unknown = False, False
            for c in arg:
                r = self.boolean(c, depth + 1)
                if r is True:
                    res = True
                    break
                if r is None:
                    seen_unknown = True
            if res is False and seen_unknown:
                res = None
        elif op == "not":
            r = self.boolean(arg, depth + 1)
            res = None if r is None else (not r)
        elif op in BINARY:
            (a, ta, _), (b, tb, _) = self.value(arg[0]), self.value(arg[1])
            if a is None or b is None:
                res = None
            else:
                if ta == "date" or tb == "date":
                    a, b = _as_date(a), _as_date(b)
                    if a is None or b is None:
                        res = None
                        self.note(op, res, None, depth)
                        return res
                res = {"eq": a == b, "gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op] if not (op != "eq" and isinstance(a, str)) else None
            self.note(op, res, {"left": None if a is None else str(a), "right": None if b is None else str(b)}, depth)
            return res
        elif op == "in":
            a, _, _ = self.value(arg[0])
            res = None if a is None else a in arg[1]
        elif op == "exists":
            f = arg.get("field") if isinstance(arg, dict) else arg
            res = self.ctx.get(f) is not None
        elif op == "between":
            (x, tx, _), (lo, _, _), (hi, _, _) = (self.value(a) for a in arg)
            res = None if x is None or lo is None or hi is None else (lo <= x <= hi)
        elif op == "effective_on":
            d, _, _ = self.value(arg[0])
            if d is None:
                res = None
            else:
                lo, hi = _as_date(arg[1]), _as_date(arg[2]) if arg[2] is not None else None
                res = (lo is None or d >= lo) and (hi is None or d < hi)
        else:
            raise AssertionError(op)
        self.note(op, res, None, depth)
        return res


def evaluate(node: dict, context: dict, parameters: dict | None = None) -> tuple[str, list[dict]]:
    """Evaluate a COMPILED node. Returns ("TRUE"|"FALSE"|"UNKNOWN", trace). Budget overrun => UNKNOWN + trace."""
    params, _ = normalise_params(parameters)
    ev = _Eval(context, params)
    try:
        r = ev.boolean(node)
    except DSLLimitError as exc:
        ev.trace.append({"depth": 0, "op": "LIMIT", "result": "UNKNOWN", "detail": str(exc)})
        return "UNKNOWN", ev.trace
    return _tv(r), ev.trace


def inline_params(node, params_json: dict):
    """Return the AST with {"param": p} replaced by {"const": value, "unit": unit} (for external evaluators)."""
    if isinstance(node, list):
        return [inline_params(x, params_json) for x in node]
    if isinstance(node, dict):
        if set(node) == {"param"} and node["param"] in params_json:
            p = params_json[node["param"]]
            return {"const": p["value"], "unit": p["unit"]}
        return {k: inline_params(v, params_json) for k, v in node.items()}
    return node


def run_test_vectors(scope: dict, condition: dict, parameters: dict | None, vectors: list[dict]) -> list[dict]:
    out = []
    for i, v in enumerate(vectors):
        s, _ = evaluate(scope, v.get("context", {}), parameters)
        c, _ = evaluate(condition, v.get("context", {}), parameters)
        es, ec = v.get("expect_scope", "TRUE"), v.get("expect_condition", "TRUE")
        out.append({"index": i, "name": v.get("name", f"vector-{i}"), "scope": s, "condition": c,
                    "expect_scope": es, "expect_condition": ec, "passed": s == es and c == ec})
    return out
