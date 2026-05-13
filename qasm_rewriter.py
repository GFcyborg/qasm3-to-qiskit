"""Shared OpenQASM 3 rewriting logic for splitting and running.

This module extracts the transpilation and rewriting logic from run.py
so both run.py and split.py can use the same rewrite rules without duplication.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openqasm3 import dumps, parse


ROOT = Path(__file__).resolve().parent
REWRITE_RULES_FILE = ROOT / "rewrite_rules.txt"


@dataclass(slots=True)
class Issue:
    start: int
    end: int
    kind: str
    detail: str


def kind(node: Any) -> str:
    return type(node).__name__


def span(node: Any) -> Any:
    return getattr(node, "span", None)


def eval_text(expr: str, env: dict[str, Any]) -> Any | None:
    expr = expr.replace("π", "pi")
    expr = re.sub(r"\btrue\b", "True", expr)
    expr = re.sub(r"\bfalse\b", "False", expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!\s*(?!=)", " not ", expr)
    expr = re.sub(r"\bpi\b", "math.pi", expr)
    expr = re.sub(r"\btau\b", "math.tau", expr)
    expr = re.sub(r"\beuler_gamma\b", "math.euler_gamma", expr)
    for name, value in sorted(env.items(), key=lambda item: -len(item[0])):
        expr = re.sub(rf"\b{name}\b", repr(value), expr)
    expr = re.sub(r"\b([A-Za-z_]\w*)\[(\d+)\]", r"bit(\1, \2)", expr)
    try:
        return eval(expr, {"__builtins__": {}, "math": math, "bit": lambda v, i: (int(v) >> int(i)) & 1, "bool": bool, "int": int, "float": float}, {})
    except Exception:
        return None


def append_issue(issues: list[Issue], node: Any, kind_name: str, detail: str) -> None:
    s = span(node)
    if s is None:
        start, end = 0, 0
    else:
        start, end = int(getattr(s, "start_line", 0)), int(getattr(s, "end_line", 0))
    issues.append(Issue(start, end, kind_name, detail))


def subst_env(text: str, env: dict[str, Any]) -> str:
    for name, value in sorted(env.items(), key=lambda item: -len(item[0])):
        text = re.sub(rf"\b{name}\b", str(value), text)
    def simplify(match: re.Match[str]) -> str:
        inner = match.group(1)
        value = eval_text(inner, env)
        return f"[{value}]" if value is not None else match.group(0)

    return re.sub(r"\[([^\[\]]+)\]", simplify, text)


def stdgates_compat_lines() -> list[str]:
    return [
        'gate p(lambda) a { U(0, 0, lambda) a; }',
        'gate phase(lambda) q { U(0, 0, lambda) q; }',
        'gate x a { U(pi, 0, pi) a; }',
        'gate y a { U(pi, pi/2, pi/2) a; }',
        'gate z a { U(0, 0, pi) a; }',
        'gate h a { U(pi/2, 0, pi) a; }',
        'gate s a { U(0, 0, pi/2) a; }',
        'gate sdg a { U(0, 0, -pi/2) a; }',
        'gate t a { U(0, 0, pi/4) a; }',
        'gate tdg a { U(0, 0, -pi/4) a; }',
        'gate sx a { U(pi/2, -pi/2, pi/2) a; }',
        'gate rx(theta) a { U(theta, -pi/2, pi/2) a; }',
        'gate ry(theta) a { U(theta, 0, 0) a; }',
        'gate rz(lambda) a { U(0, 0, lambda) a; }',
        'gate u1(lambda) q { U(0, 0, lambda) q; }',
        'gate u2(phi, lambda) q { U(pi/2, phi, lambda) q; }',
        'gate u3(theta, phi, lambda) q { U(theta, phi, lambda) q; }',
        'gate id a { U(0, 0, 0) a; }',
        'gate cx a, b { ctrl @ x a, b; }',
        'gate cy a, b { ctrl @ y a, b; }',
        'gate cz a, b { ctrl @ z a, b; }',
        'gate cp(lambda) a, b { ctrl @ p(lambda) a, b; }',
        'gate cphase(lambda) a, b { ctrl @ p(lambda) a, b; }',
        'gate crx(theta) a, b { ctrl @ rx(theta) a, b; }',
        'gate cry(theta) a, b { ctrl @ ry(theta) a, b; }',
        'gate crz(lambda) a, b { ctrl @ rz(lambda) a, b; }',
        'gate ch a, b { ctrl @ h a, b; }',
        'gate swap a, b { cx a, b; cx b, a; cx a, b; }',
        'gate ccx a, b, c { ctrl @ ctrl @ x a, b, c; }',
        'gate cswap a, b, c { ctrl @ swap a, b, c; }',
        'gate cu(theta, phi, lambda, gamma) a, b { p(gamma - theta / 2) a; ctrl @ U(theta, phi, lambda) a, b; }',
    ]


def eval_node(node: Any, env: dict[str, Any]) -> Any | None:
    k = kind(node)
    if k == "IntegerLiteral":
        return int(getattr(node, "value", 0))
    if k == "FloatLiteral":
        return float(getattr(node, "value", 0.0))
    if k == "BooleanLiteral":
        return str(getattr(node, "value", "false")).lower() == "true"
    if k == "Identifier":
        return env.get(getattr(node, "name", ""))
    if k == "ArrayLiteral":
        return [eval_node(value, env) for value in getattr(node, "values", [])]
    if k == "IndexExpression":
        base = eval_node(getattr(node, "collection", None), env)
        if base is None:
            return None
        indices = getattr(node, "index", [])
        if isinstance(base, int):
            value = base
            for index in indices:
                resolved = eval_node(index, env)
                if resolved is None:
                    return None
                value = (int(value) >> int(resolved)) & 1
            return value
        value = base
        for index in indices:
            resolved = eval_node(index, env)
            if resolved is None:
                return None
            value = value[int(resolved)]
        return value
    if k == "Cast":
        value = eval_node(getattr(node, "argument", None), env)
        if value is None:
            return None
        target = kind(getattr(node, "type", None))
        if target == "BoolType":
            return bool(value)
        if target in {"IntType", "UintType", "BitType"}:
            return int(value)
        if target == "FloatType":
            return float(value)
    if k == "UnaryExpression":
        value = eval_node(getattr(node, "expression", None), env)
        op = getattr(node, "op", None)
        if value is None:
            return None
        if op == "-":
            return -value
        if op == "+":
            return +value
        if op == "!":
            return not bool(value)
        if op == "~":
            return ~int(value)
    if k == "BinaryExpression":
        left = eval_node(getattr(node, "lhs", None), env)
        right = eval_node(getattr(node, "rhs", None), env)
        op = getattr(node, "op", None)
        if left is None or right is None:
            return None
        match op:
            case "+":
                return left + right
            case "-":
                return left - right
            case "*":
                return left * right
            case "/":
                return left / right
            case "%":
                return left % right
            case "**":
                return left ** right
            case "<":
                return left < right
            case "<=":
                return left <= right
            case ">":
                return left > right
            case ">=":
                return left >= right
            case "==":
                return left == right
            case "!=":
                return left != right
            case "&&":
                return bool(left) and bool(right)
            case "||":
                return bool(left) or bool(right)
            case "&":
                return int(left) & int(right)
            case "|":
                return int(left) | int(right)
            case "^":
                return int(left) ^ int(right)
            case "<<":
                return int(left) << int(right)
            case ">>":
                return int(left) >> int(right)
    try:
        return eval_text(dumps(node).strip().rstrip(";"), env)
    except Exception:
        return None


def range_values(node: Any, env: dict[str, Any]) -> list[int] | None:
    start = eval_node(getattr(node, "start", None), env)
    end = eval_node(getattr(node, "end", None), env)
    step = eval_node(getattr(node, "step", None), env) if getattr(node, "step", None) is not None else None
    if start is None or end is None:
        return None
    start = int(start)
    end = int(end)
    step = int(step) if step is not None else (1 if start <= end else -1)
    if step == 0:
        return None
    values: list[int] = []
    current = start
    if step > 0:
        while current <= end:
            values.append(current)
            current += step
    else:
        while current >= end:
            values.append(current)
            current += step
    return values


def rewrite_condition_text(expr: Any, env: dict[str, Any]) -> str | None:
    if eval_node(expr, env) is not None:
        value = eval_node(expr, env)
        if isinstance(value, bool):
            return "true" if value else "false"
    
    k = kind(expr)
    if k == "BinaryExpression":
        op = getattr(expr, "op", None)
        op_name = getattr(op, "name", None) if hasattr(op, "name") else str(op)
        if op_name == "==":
            lhs = getattr(expr, "lhs", None)
            rhs = getattr(expr, "rhs", None)
            lhs_k = kind(lhs)
            rhs_k = kind(rhs)
            
            if lhs_k == "Identifier" and rhs_k == "IntegerLiteral":
                ident_name = getattr(lhs, "name", "")
                rhs_value = getattr(rhs, "value", None)
                if ident_name and rhs_value is not None:
                    if rhs_value == 1:
                        return f"{ident_name} == true"
                    if rhs_value == 0:
                        return f"{ident_name} == false"
    
    text = subst_env(dumps(expr).strip().rstrip(";"), env)
    text = re.sub(r"\b(?:int|uint|bool|float|bit)\b(?:\[\d+\])?\s*\(([^)]+)\)", r"\1", text)
    if re.fullmatch(r"[A-Za-z_]\w*", text):
        return f"{text} == true"
    if re.fullmatch(r"![A-Za-z_]\w*", text):
        return f"{text[1:]} == false"
    text = re.sub(r"^\(?\s*([A-Za-z_]\w*)\s*==\s*0\s*\)?$", r"\1 == false", text)
    text = re.sub(r"^\(?\s*(.+?)\s*==\s*0\s*\)?$", r"\1 == false", text)
    return text


def is_supported_decl(stmt: Any) -> bool:
    tname = kind(getattr(stmt, "type", None))
    return tname in {"BitType", "BoolType"}


def contains_timing_constructs(node: Any) -> bool:
    """Return True if the expression uses unsupported timing machinery."""
    for child in node_iter(node):
        if kind(child) in {"DurationOf", "StretchDeclaration", "DurationDeclaration", "DurationType", "StretchType"}:
            return True
    return kind(node) in {"DurationOf"}


def subroutine_param_names(subroutine: Any) -> list[str]:
    names: list[str] = []
    for arg in getattr(subroutine, "arguments", []) or []:
        arg_name = getattr(getattr(arg, "name", None), "name", "")
        if arg_name:
            names.append(arg_name)
    return names


def substitute_names(text: str, mapping: dict[str, str]) -> str:
    out = text
    for name, value in sorted(mapping.items(), key=lambda item: -len(item[0])):
        out = re.sub(rf"\b{name}\b", value, out)
    return out


def renamed_gate_name(name: str, taken_names: set[str]) -> str:
    candidate = f"my_{name}"
    while candidate in taken_names or candidate == name:
        candidate += "_"
    return candidate


def subroutine_is_inlineable(subroutine: Any) -> bool:
    def stmt_inlineable(stmt: Any) -> bool:
        k = kind(stmt)
        if k in {"QuantumGate", "QuantumMeasurementStatement", "QuantumReset", "ReturnStatement"}:
            return True
        if k == "Box":
            return all(stmt_inlineable(inner) for inner in (getattr(stmt, "body", []) or []))
        return False

    return all(stmt_inlineable(stmt) for stmt in (getattr(subroutine, "body", []) or []))


def majority_vote_pairs_from_subroutine(subroutine: Any, arg_map: dict[str, str]) -> list[tuple[str, str]] | None:
    param_names = subroutine_param_names(subroutine)
    if len(param_names) != 1:
        return None

    body = getattr(subroutine, "body", []) or []
    return_stmt = next((stmt for stmt in body if kind(stmt) == "ReturnStatement"), None)
    if return_stmt is None:
        return None
    ret_name = getattr(getattr(return_stmt, "expression", None), "name", "")
    if not ret_name:
        return None

    pairs: list[tuple[str, str]] = []
    for stmt in body:
        if kind(stmt) != "BranchingStatement":
            continue
        if getattr(stmt, "else_block", []):
            return None
        if_block = getattr(stmt, "if_block", [])
        if len(if_block) != 1 or kind(if_block[0]) != "ClassicalAssignment":
            return None
        assign = if_block[0]
        l_name = getattr(getattr(assign, "lvalue", None), "name", "")
        r_node = getattr(assign, "rvalue", None)
        r_val = getattr(r_node, "value", None) if kind(r_node) == "IntegerLiteral" else None
        op = getattr(getattr(assign, "op", None), "name", None) or str(getattr(assign, "op", ""))
        if l_name != ret_name or op != "=" or r_val != 1:
            return None

        cond_expr = getattr(stmt, "condition", None)
        if kind(cond_expr) != "BinaryExpression":
            return None
        cond_op = getattr(cond_expr, "op", None)
        cond_op_name = getattr(cond_op, "name", None) if hasattr(cond_op, "name") else str(cond_op)
        if cond_op_name != "&":
            return None

        lhs = getattr(cond_expr, "lhs", None)
        rhs = getattr(cond_expr, "rhs", None)
        if lhs is None or rhs is None:
            return None
        try:
            lhs_text = substitute_names(dumps(lhs).strip().rstrip(";"), arg_map)
            rhs_text = substitute_names(dumps(rhs).strip().rstrip(";"), arg_map)
        except Exception:
            return None
        pairs.append((lhs_text, rhs_text))

    return pairs if pairs else None


def logical_meas_call_pattern(
    call: Any,
    subroutines: dict[str, Any],
    inline_depth: int,
) -> tuple[list[str], list[tuple[str, str]]] | None:
    name = getattr(getattr(call, "name", None), "name", "")
    if name not in subroutines:
        return None

    subroutine = subroutines[name]
    param_names = subroutine_param_names(subroutine)
    call_args = getattr(call, "arguments", []) or []
    if len(param_names) != 1 or len(call_args) != 1:
        return None

    body = getattr(subroutine, "body", []) or []
    decls = [stmt for stmt in body if kind(stmt) == "ClassicalDeclaration"]
    meas = next((stmt for stmt in body if kind(stmt) == "QuantumMeasurementStatement"), None)
    vote_assign = next((stmt for stmt in body if kind(stmt) == "ClassicalAssignment"), None)
    ret_stmt = next((stmt for stmt in body if kind(stmt) == "ReturnStatement"), None)
    if meas is None or vote_assign is None or ret_stmt is None:
        return None

    target_name = getattr(getattr(meas, "target", None), "name", "")
    target_decl = next((stmt for stmt in decls if getattr(getattr(stmt, "identifier", None), "name", "") == target_name), None)
    if target_decl is None:
        return None

    rvalue = getattr(vote_assign, "rvalue", None)
    if kind(rvalue) != "FunctionCall":
        return None
    vote_name = getattr(getattr(rvalue, "name", None), "name", "")
    if vote_name not in subroutines:
        return None
    vote_args = getattr(rvalue, "arguments", []) or []
    if len(vote_args) != 1:
        return None

    vote_param = subroutine_param_names(subroutines[vote_name])
    if len(vote_param) != 1:
        return None

    span_obj = getattr(call, "span", None)
    call_id = int(getattr(span_obj, "start_line", 0) or 0)
    temp_name = f"tmp{call_id}"

    try:
        decl_text = dumps(target_decl).strip().rstrip(";")
        decl_text = re.sub(rf"\b{re.escape(target_name)}\b", temp_name, decl_text)
        arg_text = dumps(call_args[0]).strip().rstrip(";")
        meas_text = dumps(meas).strip().rstrip(";")
        meas_text = re.sub(rf"\b{re.escape(param_names[0])}\b", arg_text, meas_text)
        meas_text = re.sub(rf"\b{re.escape(target_name)}\b", temp_name, meas_text)
    except Exception:
        return None

    pairs = majority_vote_pairs_from_subroutine(
        subroutines[vote_name],
        {vote_param[0]: temp_name},
    )
    if pairs is None:
        return None

    return [decl_text + ";", meas_text + ";"], pairs


def inline_call_assignment_if_pattern(
    assign_stmt: Any,
    next_stmt: Any,
    env: dict[str, Any],
    issues: list[Issue],
    indent: int,
    subroutines: dict[str, Any],
    inline_depth: int,
    gate_renames: dict[str, str] | None = None,
) -> list[str] | None:
    if kind(assign_stmt) != "ClassicalAssignment" or kind(next_stmt) != "BranchingStatement":
        return None
    lvalue = getattr(assign_stmt, "lvalue", None)
    rvalue = getattr(assign_stmt, "rvalue", None)
    if kind(lvalue) != "Identifier" or kind(rvalue) != "FunctionCall":
        return None
    assigned_name = getattr(lvalue, "name", "")
    if not assigned_name:
        return None

    cond = getattr(next_stmt, "condition", None)
    cond_kind = kind(cond)
    compare_true = False
    if cond_kind == "Identifier" and getattr(cond, "name", "") == assigned_name:
        compare_true = True
    elif cond_kind == "BinaryExpression":
        op = getattr(cond, "op", None)
        op_name = getattr(op, "name", None) if hasattr(op, "name") else str(op)
        lhs = getattr(cond, "lhs", None)
        rhs = getattr(cond, "rhs", None)
        lhs_name = getattr(lhs, "name", "") if kind(lhs) == "Identifier" else ""
        if op_name == "==" and lhs_name == assigned_name and kind(rhs) == "IntegerLiteral":
            compare_true = int(getattr(rhs, "value", 0)) == 1

    if not compare_true or getattr(next_stmt, "else_block", []):
        return None

    if_block = getattr(next_stmt, "if_block", [])
    if len(if_block) != 1 or kind(if_block[0]) != "QuantumGate":
        return None
    gate_stmt = if_block[0]
    gate_name = getattr(getattr(gate_stmt, "name", None), "name", "")
    if gate_name != "z":
        return None

    lowered = logical_meas_call_pattern(rvalue, subroutines, inline_depth)
    if lowered is None:
        return None
    prelude, pairs = lowered

    pad = "  " * indent
    out: list[str] = [pad + line for line in prelude]
    for lhs_text, rhs_text in pairs:
        out.append(pad + f"if ({lhs_text} == true) {{")
        out.append(pad + f"  if ({rhs_text} == true) {{")
        out.extend(emit_stmt(gate_stmt, env, issues, indent + 2, subroutines=subroutines, inline_depth=inline_depth, gate_renames=gate_renames))
        out.append(pad + "  }")
        out.append(pad + "}")
    return out


def inline_subroutine_call(
    subroutine: Any,
    call: Any,
    assign_target: str | None,
    env: dict[str, Any],
    issues: list[Issue],
    indent: int,
    subroutines: dict[str, Any],
    inline_depth: int,
    gate_renames: dict[str, str] | None = None,
) -> list[str] | None:
    MAX_INLINE_DEPTH = 6
    if inline_depth >= MAX_INLINE_DEPTH:
        append_issue(issues, call, "functioncall", f"cannot inline subroutine (depth>{MAX_INLINE_DEPTH})")
        return None

    if not subroutine_is_inlineable(subroutine):
        append_issue(issues, call, "functioncall", "cannot inline subroutine (body uses unsupported classical flow)")
        return None

    param_names = subroutine_param_names(subroutine)
    call_args = getattr(call, "arguments", []) or []
    if len(param_names) != len(call_args):
        append_issue(issues, call, "functioncall", "cannot inline subroutine (argument count mismatch)")
        return None

    arg_texts: dict[str, str] = {}
    for name, arg in zip(param_names, call_args):
        try:
            arg_texts[name] = dumps(arg).strip().rstrip(";")
        except Exception:
            append_issue(issues, call, "functioncall", "cannot inline subroutine (cannot serialize argument)")
            return None

    local_renames: dict[str, str] = {}
    call_name = getattr(getattr(call, "name", None), "name", "sub")
    for body_stmt in getattr(subroutine, "body", []) or []:
        if kind(body_stmt) == "ClassicalDeclaration":
            ident = getattr(getattr(body_stmt, "identifier", None), "name", "")
            if ident and ident not in arg_texts:
                local_renames[ident] = f"tmp{inline_depth}_{ident}"

    name_map = dict(arg_texts)
    name_map.update(local_renames)

    out: list[str] = []
    for body_stmt in getattr(subroutine, "body", []) or []:
        if kind(body_stmt) == "ReturnStatement":
            if assign_target is None:
                continue
            expr = getattr(body_stmt, "expression", None)
            if expr is None:
                append_issue(issues, body_stmt, "returnstatement", "cannot inline return without value")
                return None
            try:
                expr_text = dumps(expr).strip().rstrip(";")
            except Exception:
                append_issue(issues, body_stmt, "returnstatement", "cannot inline return expression")
                return None
            mapped_expr = substitute_names(expr_text, name_map)
            if mapped_expr != assign_target:
                out.append(("  " * indent) + f"{assign_target} = {mapped_expr};")
            continue

        inner_lines = emit_stmt(
            body_stmt,
            dict(env),
            issues,
            indent,
            subroutines=subroutines,
            inline_depth=inline_depth + 1,
            gate_renames=gate_renames,
        )
        out.extend(substitute_names(line, name_map) for line in inner_lines)
    return out


def emit_stmt(
    stmt: Any,
    env: dict[str, Any],
    issues: list[Issue],
    indent: int = 0,
    *,
    subroutines: dict[str, Any] | None = None,
    inline_depth: int = 0,
    gate_renames: dict[str, str] | None = None,
) -> list[str]:
    if subroutines is None:
        subroutines = {}
    pad = "  " * indent
    k = kind(stmt)
    if k == "Include":
        name = getattr(stmt, "filename", "")
        if Path(name).name == "stdgates.inc":
            return [pad + line for line in stdgates_compat_lines()]
        return []
    if k == "ConstantDeclaration":
        name = getattr(getattr(stmt, "identifier", None), "name", "")
        value = eval_node(getattr(stmt, "init_expression", None), env)
        if value is not None:
            env[name] = value
            return []
        append_issue(issues, stmt, k.lower(), f"cannot fold constant {name}")
        return []
    if k == "ClassicalDeclaration":
        if is_supported_decl(stmt):
            return [pad + dumps(stmt).strip()]
        name = getattr(getattr(stmt, "identifier", None), "name", "")
        value = eval_node(getattr(stmt, "init_expression", None), env)
        if value is not None:
            env[name] = value
            return []
        append_issue(issues, stmt, k.lower(), f"drop unsupported declaration {name}")
        return []
    if k == "ClassicalAssignment":
        lvalue = getattr(stmt, "lvalue", None)
        rvalue = getattr(stmt, "rvalue", None)
        op = getattr(getattr(stmt, "op", None), "name", None) or str(getattr(stmt, "op", ""))
        lvalue_kind = kind(lvalue)
        rvalue_kind = kind(rvalue)

        if lvalue_kind == "IndexExpression":
            append_issue(issues, stmt, k.lower(), "drop array/slice assignment (not supported by qiskit importer)")
            return []

        if lvalue_kind == "Identifier":
            name = getattr(lvalue, "name", "")
            if name:
                rhs = eval_node(rvalue, env)
                if rhs is not None and name in env:
                    try:
                        if op == "=":
                            env[name] = rhs
                            return []
                        if op == "+=":
                            env[name] = env[name] + rhs
                            return []
                        if op == "-=":
                            env[name] = env[name] - rhs
                            return []
                        if op == "*=":
                            env[name] = env[name] * rhs
                            return []
                        if op == "/=":
                            env[name] = env[name] / rhs
                            return []
                        if op == "%=":
                            env[name] = env[name] % rhs
                            return []
                        if op == "<<=":
                            env[name] = int(env[name]) << int(rhs)
                            return []
                        if op == ">>=":
                            env[name] = int(env[name]) >> int(rhs)
                            return []
                        if op == "&=":
                            env[name] = int(env[name]) & int(rhs)
                            return []
                        if op == "|=":
                            env[name] = int(env[name]) | int(rhs)
                            return []
                        if op == "^=":
                            env[name] = int(env[name]) ^ int(rhs)
                            return []
                    except Exception:
                        pass

        if rvalue_kind == "FunctionCall":
            name = getattr(getattr(rvalue, "name", None), "name", "")
            if name and name in subroutines and lvalue_kind == "Identifier":
                assign_target = getattr(lvalue, "name", "")
                if assign_target:
                    inlined = inline_subroutine_call(
                        subroutines[name],
                        rvalue,
                        assign_target,
                        env,
                        issues,
                        indent,
                        subroutines,
                        inline_depth,
                        gate_renames,
                    )
                    if inlined is not None:
                        return inlined
            append_issue(issues, stmt, k.lower(), "drop subroutine call assignment (cannot inline)")
            return []

        try:
            return [pad + dumps(stmt).strip()]
        except Exception:
            append_issue(issues, stmt, k.lower(), "cannot emit classical assignment")
            return []
    if k == "ForInLoop":
        values = range_values(getattr(stmt, "set_declaration", None), env)
        if values is None:
            append_issue(issues, stmt, k.lower(), "loop range is not statically known")
            return []
        MAX_UNROLL = 256
        if len(values) > MAX_UNROLL:
            append_issue(issues, stmt, k.lower(), f"loop range too large to unroll ({len(values)} > {MAX_UNROLL})")
            return []
        out: list[str] = []
        ident = getattr(getattr(stmt, "identifier", None), "name", "")
        for value in values:
            next_env = dict(env)
            next_env[ident] = value
            for inner in getattr(stmt, "block", []):
                out.extend(emit_stmt(inner, next_env, issues, indent, subroutines=subroutines, inline_depth=inline_depth, gate_renames=gate_renames))
        return out
    if k == "BranchingStatement":
        cond_value = eval_node(getattr(stmt, "condition", None), env)
        if isinstance(cond_value, bool):
            block = getattr(stmt, "if_block" if cond_value else "else_block", [])
            out: list[str] = []
            for inner in block:
                out.extend(emit_stmt(inner, env, issues, indent, subroutines=subroutines, inline_depth=inline_depth, gate_renames=gate_renames))
            return out
        cond_text = rewrite_condition_text(getattr(stmt, "condition", None), env)
        if cond_text is None:
            append_issue(issues, stmt, k.lower(), "condition cannot be rewritten for qiskit")
            return []
        cond_text = subst_env(cond_text, env)
        out = [pad + f"if ({cond_text}) {{"]
        for inner in getattr(stmt, "if_block", []):
            out.extend(emit_stmt(inner, env, issues, indent + 1, subroutines=subroutines, inline_depth=inline_depth, gate_renames=gate_renames))
        if getattr(stmt, "else_block", []):
            out.append(pad + "} else {")
            for inner in getattr(stmt, "else_block", []):
                out.extend(emit_stmt(inner, env, issues, indent + 1, subroutines=subroutines, inline_depth=inline_depth, gate_renames=gate_renames))
        out.append(pad + "}")
        return out
    if k == "Box":
        out: list[str] = []
        for inner in getattr(stmt, "body", []):
            out.extend(emit_stmt(inner, env, issues, indent, subroutines=subroutines, inline_depth=inline_depth, gate_renames=gate_renames))
        return out
    if k == "ExpressionStatement":
        expr = getattr(stmt, "expression", None)
        if kind(expr) == "FunctionCall":
            name = getattr(getattr(expr, "name", None), "name", "")
            if name and name in subroutines:
                inlined = inline_subroutine_call(
                    subroutines[name],
                    expr,
                    None,
                    env,
                    issues,
                    indent,
                    subroutines,
                    inline_depth,
                    gate_renames,
                )
                if inlined is not None:
                    return inlined
            append_issue(issues, stmt, k.lower(), "drop subroutine call statement (cannot inline)")
            return []
        append_issue(issues, stmt, k.lower(), "not supported by qiskit importer")
        return []
    if k in {
        "CalibrationGrammarDeclaration",
        "CalibrationDefinition",
        "ExternDeclaration",
        "StretchDeclaration",
        "DurationDeclaration",
        "ReturnStatement",
        "QuantumDelay",
    }:
        append_issue(issues, stmt, k.lower(), "not supported by qiskit importer")
        return []
    if k == "DelayInstruction":
        duration_expr = getattr(stmt, "duration", None)
        if duration_expr is not None:
            if kind(duration_expr) == "Identifier" and getattr(duration_expr, "name", "") not in env:
                append_issue(issues, stmt, k.lower(), "drop delay (duration depends on dropped timing symbol)")
                return []
            if contains_timing_constructs(duration_expr):
                append_issue(issues, stmt, k.lower(), "drop delay (unsupported timing construct)")
                return []
        append_issue(issues, stmt, k.lower(), "drop delay (not supported by qiskit importer)")
        return []
    if k == "QuantumReset":
        return [pad + subst_env(dumps(stmt).strip(), env)]
    if k == "QuantumMeasurementStatement":
        return [pad + subst_env(dumps(stmt).strip(), env)]
    if k == "QuantumGate":
        text = subst_env(dumps(stmt).strip(), env)
        if re.match(r"^u\b", text):
            text = re.sub(r"^u\b", "U(0, 0, 0)", text)
        if gate_renames:
            text = substitute_names(text, gate_renames)
        return [pad + text]
    if k == "QuantumGateDefinition":
        name = getattr(getattr(stmt, "name", None), "name", "")
        rename_to = gate_renames.get(name) if gate_renames else None
        if rename_to:
            append_issue(issues, stmt, "quantumgatedefinition", f"rename colliding gate {name} -> {rename_to} to avoid stdgates.inc collision")
            name = rename_to
        params: list[str] = []
        for arg in getattr(stmt, "arguments", []) or []:
            if kind(arg) == "Identifier":
                arg_name = getattr(arg, "name", "")
            else:
                arg_name = getattr(getattr(arg, "name", None), "name", "")
            if arg_name:
                params.append(arg_name)
        qubits: list[str] = []
        for qb in getattr(stmt, "qubits", []) or []:
            qb_name = getattr(qb, "name", "")
            if qb_name:
                qubits.append(qb_name)

        if not name or not qubits:
            text = dumps(stmt).strip()
            if gate_renames:
                text = substitute_names(text, gate_renames)
            return [pad + text]

        header = f"gate {name}"
        if params:
            header += "(" + ", ".join(params) + ")"
        header += " " + ", ".join(qubits) + " {"

        out: list[str] = [pad + header]
        for inner in getattr(stmt, "body", []) or []:
            out.extend(emit_stmt(inner, env, issues, indent + 1, subroutines=subroutines, inline_depth=inline_depth, gate_renames=gate_renames))
        out.append(pad + "}")
        return out
    if k == "SubroutineDefinition":
        return []
    if k in {"Program", "StatementOrScope"}:
        out: list[str] = []
        block = list(getattr(stmt, "statements", getattr(stmt, "block", [])))
        i = 0
        while i < len(block):
            current = block[i]
            nxt = block[i + 1] if i + 1 < len(block) else None
            if nxt is not None:
                lowered = inline_call_assignment_if_pattern(
                    current,
                    nxt,
                    env,
                    issues,
                    indent,
                    subroutines,
                    inline_depth,
                    gate_renames,
                )
                if lowered is not None:
                    out.extend(lowered)
                    i += 2
                    continue
            out.extend(emit_stmt(current, env, issues, indent, subroutines=subroutines, inline_depth=inline_depth, gate_renames=gate_renames))
            i += 1
        return out
    if k == "Annotation":
        return []
    try:
        text = subst_env(dumps(stmt).strip(), env)
        if gate_renames:
            text = substitute_names(text, gate_renames)
        return [pad + text]
    except Exception:
        append_issue(issues, stmt, k.lower(), "cannot emit statement")
        return []


def extract_qasm_version(source: str) -> str:
    """Extract OPENQASM version from source header. Returns '3.0', '3.1', or '3.0' (default)."""
    for line in source.split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.upper().startswith('OPENQASM'):
            if '3.1' in stripped:
                return '3.1'
            elif '3.0' in stripped:
                return '3.0'
            return '3.0'
    return '3.0'


def node_iter(node: Any):
    if not dataclasses.is_dataclass(node):
        return
    for field in dataclasses.fields(node):
        value = getattr(node, field.name)
        if isinstance(value, list):
            for child in value:
                if dataclasses.is_dataclass(child):
                    yield child
                    yield from node_iter(child)
        elif dataclasses.is_dataclass(value):
            yield value
            yield from node_iter(value)


def transpile_qasm(source: str) -> tuple[str, list[Issue], Any | None]:
    """Rewrite OpenQASM 3 source to Qiskit-compatible form.
    
    Returns (rewritten_text, issues, parsed_program).
    """
    version = extract_qasm_version(source)
    if version == "3.1":
        program = parse(source)
        return source, [], program

    program_original: Any | None
    try:
        program_original = parse(source)
    except Exception:
        program_original = None

    def strip_calibration_blocks_preserve_lines(text: str) -> str:
        out_lines: list[str] = []
        lines = text.splitlines(True)
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.lstrip()
            if re.match(r"(?i)^defcalgrammar\b", stripped):
                out_lines.append("\n" if line.endswith("\n") else "")
                i += 1
                continue
            if re.match(r"(?i)^defcal\b", stripped):
                brace = line.count("{") - line.count("}")
                out_lines.append("\n" if line.endswith("\n") else "")
                i += 1
                while i < len(lines) and brace > 0:
                    ln = lines[i]
                    brace += ln.count("{") - ln.count("}")
                    out_lines.append("\n" if ln.endswith("\n") else "")
                    i += 1
                continue
            out_lines.append(line)
            i += 1
        return "".join(out_lines)

    try:
        program = program_original if program_original is not None else parse(source)
    except Exception:
        stripped = strip_calibration_blocks_preserve_lines(source)
        if not re.search(r"(?mi)^[ \t]*OPENQASM\b", stripped):
            stripped = "OPENQASM 3.0;\n" + stripped
        program = parse(stripped)
    
    env: dict[str, Any] = {}
    issues: list[Issue] = []
    lines: list[str] = []
    subroutines: dict[str, Any] = {}

    for stmt in getattr(program, "statements", []):
        if kind(stmt) == "SubroutineDefinition":
            name = getattr(getattr(stmt, "name", None), "name", "")
            if name:
                subroutines[name] = stmt

    stdgates_defs = stdgates_compat_lines()
    stdgates_names: list[str] = []
    import re as _re
    for defline in stdgates_defs:
        m = _re.match(r"^gate\s+([A-Za-z_]\w*)", defline)
        if m:
            stdgates_names.append(m.group(1))

    user_defined: set[str] = set()
    try:
        for stmt in getattr(program, "statements", []):
            if kind(stmt) == "QuantumGateDefinition":
                name_obj = getattr(stmt, "name", None) or getattr(stmt, "identifier", None)
                name = getattr(name_obj, "name", None)
                if name:
                    user_defined.add(name)
            for node in node_iter(stmt):
                if kind(node) == "QuantumGateDefinition":
                    name = getattr(getattr(node, "identifier", None), "name", None)
                    if name:
                        user_defined.add(name)
    except Exception:
        pass

    gate_renames: dict[str, str] = {}
    taken_gate_names = set(user_defined) | set(stdgates_names)
    for name in sorted(user_defined):
        if name in stdgates_names:
            renamed = renamed_gate_name(name, taken_gate_names)
            gate_renames[name] = renamed
            taken_gate_names.add(renamed)

    lines.append("OPENQASM 3.0;")
    stmts = list(program.statements)
    i = 0
    while i < len(stmts):
        stmt = stmts[i]
        nxt = stmts[i + 1] if i + 1 < len(stmts) else None

        if kind(stmt) == "IODeclaration":
            try:
                lines.append(dumps(stmt).strip())
            except Exception:
                append_issue(issues, stmt, "iodeclaration", "cannot emit IO declaration")
            i += 1
            continue

        if nxt is not None:
            lowered = inline_call_assignment_if_pattern(
                stmt,
                nxt,
                env,
                issues,
                0,
                subroutines,
                0,
                gate_renames,
            )
            if lowered is not None:
                lines.extend(lowered)
                i += 2
                continue

        lines.extend(emit_stmt(stmt, env, issues, 0, subroutines=subroutines, inline_depth=0, gate_renames=gate_renames))
        i += 1

    def inferred_qubit_decl_lines() -> list[str]:
        if any(kind(stmt) == "QubitDeclaration" for stmt in getattr(program, "statements", [])):
            return []

        usages: dict[str, int] = {}

        def operand_name_and_max_index(operand: Any) -> tuple[str, int] | None:
            k = kind(operand)
            if k == "Identifier":
                name = getattr(operand, "name", "")
                if name.startswith("$"):
                    return None
                return (name, 0) if name else None
            if k == "IndexedIdentifier":
                name_obj = getattr(operand, "name", None)
                name = getattr(name_obj, "name", "")
                if not name:
                    return None
                if name.startswith("$"):
                    return None
                max_index = 0
                for index_group in getattr(operand, "indices", []):
                    for index_expr in index_group:
                        value = eval_node(index_expr, {})
                        if value is None:
                            return None
                        max_index = max(max_index, int(value))
                return name, max_index
            return None

        for stmt in getattr(program, "statements", []):
            if kind(stmt) in {"CalibrationGrammarDeclaration", "CalibrationDefinition"}:
                continue
            if kind(stmt) == "QuantumGateDefinition":
                continue
            for operand in getattr(stmt, "qubits", []):
                info = operand_name_and_max_index(operand)
                if info is None:
                    continue
                name, max_index = info
                usages[name] = max(usages.get(name, 0), max_index)

        decl_lines: list[str] = []
        for name in sorted(usages):
            decl_lines.append(f"qubit[{usages[name] + 1}] {name};")
        return decl_lines

    qubit_decl_lines = inferred_qubit_decl_lines()
    stdgates_defs = stdgates_compat_lines()
    stdgates_names: list[str] = []
    import re as _re
    for defline in stdgates_defs:
        m = _re.match(r"^gate\s+([A-Za-z_]\w*)", defline)
        if m:
            stdgates_names.append(m.group(1))

    user_defined: set[str] = set()
    try:
        for stmt in getattr(program, "statements", []):
            if kind(stmt) == "QuantumGateDefinition":
                name_obj = getattr(stmt, "name", None) or getattr(stmt, "identifier", None)
                name = getattr(name_obj, "name", None)
                if name:
                    user_defined.add(name)
            for node in node_iter(stmt):
                if kind(node) == "QuantumGateDefinition":
                    name = getattr(getattr(node, "identifier", None), "name", None)
                    if name:
                        user_defined.add(name)
    except Exception:
        pass

    gate_renames: dict[str, str] = {}
    taken_gate_names = set(user_defined) | set(stdgates_names)
    for name in sorted(user_defined):
        if name in stdgates_names:
            renamed = renamed_gate_name(name, taken_gate_names)
            gate_renames[name] = renamed
            taken_gate_names.add(renamed)

    joined = "\n".join(line for line in lines if line.strip())
    if stdgates_names:
        pattern = _re.compile(r"\b(" + "|".join(_re.escape(n) for n in stdgates_names) + r")\b")
        has_std_ref = bool(pattern.search(joined))
        has_defs = any(defline.strip() in (ln.strip() for ln in lines) for defline in stdgates_defs)
        if has_std_ref and not has_defs:
            original_lines = [ln for ln in lines if ln.strip()]
            if original_lines and original_lines[0].strip().upper().startswith("OPENQASM"):
                out_lines = [original_lines[0]] + stdgates_defs + original_lines[1:]
                start_idx = 1 + len(stdgates_defs)
            else:
                out_lines = stdgates_defs + original_lines
                start_idx = len(stdgates_defs)

            for i in range(start_idx, len(out_lines)):
                line = out_lines[i]
                for name in stdgates_names:
                    up = name.upper()
                    if up == name:
                        continue
                    line = _re.sub(rf"\b{_re.escape(up)}\b", name, line)
                out_lines[i] = line

            joined = "\n".join(out_lines)

    hw_matches = _re.findall(r"\$([0-9]+)", joined)
    if hw_matches:
        hw_indices = [int(x) for x in hw_matches]
        max_hw = max(hw_indices)
        hw_name = "hw"
        while _re.search(rf"\b{_re.escape(hw_name)}\b", joined):
            hw_name += "_"
        hw_decl = f"qubit[{max_hw + 1}] {hw_name};"
        joined = _re.sub(r"\$([0-9]+)", lambda m: f"{hw_name}[{int(m.group(1))}]", joined)
        if 'qubit_decl_lines' in locals() and qubit_decl_lines:
            qubit_decl_lines.insert(0, hw_decl)
        else:
            qubit_decl_lines = [hw_decl]

    if qubit_decl_lines:
        out_lines = joined.splitlines()
        insert_at = 0
        idx = 0
        if out_lines and out_lines[0].strip().upper().startswith("OPENQASM"):
            insert_at = 1
            idx = 1

        while idx < len(out_lines):
            stripped = out_lines[idx].strip()
            if not stripped:
                idx += 1
                continue
            if not stripped.startswith("gate "):
                break

            brace_depth = stripped.count("{") - stripped.count("}")
            insert_at = idx + 1
            idx += 1
            while idx < len(out_lines) and brace_depth > 0:
                line = out_lines[idx]
                brace_depth += line.count("{") - line.count("}")
                insert_at = idx + 1
                idx += 1
        out_lines[insert_at:insert_at] = qubit_decl_lines
        joined = "\n".join(out_lines)

    rewritten = joined + "\n"
    rewritten = re.sub(r"\buint\b", "int", rewritten)
    rewritten = re.sub(r"\bstretch\s+\w+;\n?", "", rewritten)
    rewritten = re.sub(r"\bduration\s+\w+\s*=.*?;\n?", "", rewritten)
    return rewritten, issues, (program_original if program_original is not None else program)
