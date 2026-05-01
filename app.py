from __future__ import annotations

import dataclasses
import concurrent.futures
import multiprocessing
import math
import re
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import PySide6
import qiskit_qasm3_import
from PySide6.QtCore import QObject, QRunnable, QRect, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPainter, QPixmap, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGraphicsScene,
    QGraphicsView,
    QComboBox,
    QCheckBox,
    QHBoxLayout,
    QFormLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QInputDialog,
    QMessageBox,
    QLineEdit,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from openqasm3 import dumps, parse
from qiskit import transpile
from qiskit_aer import AerSimulator
from qiskit_qasm3_import import parse as qiskit_parse
from qiskit.visualization import circuit_drawer


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"
REPO_URLS = ROOT / "repositories.url"
REWRITE_RULES_FILE = ROOT / "rewrite_rules.txt"
REWRITE_RULES_SYNC_MARKERS = (
    "Insert OPENQASM 3.0 header when missing.",
    "Normalize hardware qubit identifiers like $0 into a declared qubit register.",
    "Expand include \"stdgates.inc\" into inline compatibility gate definitions.",
    "Drop includes other than stdgates.inc.",
    "Fold const declarations with statically known values into an environment.",
    "Keep only classical declarations whose type is bit or bool.",
    "Drop unsupported classical declarations that cannot be folded.",
    "Unroll for loops only when range bounds are statically known.",
    "Cap static loop unrolling to avoid output blow-ups.",
    "Fold branching statements when condition is statically known.",
    "Rewrite bit comparisons for qiskit-compatible conditions.",
    "Drop calibration grammar/definitions, extern declarations, stretch, duration, and unsupported delay statements.",
    "Unbox box statements by emitting only their inner body statements.",
    "Drop subroutines, aliasing, and classical assignments not supported by qiskit importer.",
    "Rewrite gate definitions by rewriting/unrolling their bodies when needed.",
    "Keep reset, measurement, gate definitions, and quantum gate operations.",
    "Substitute compile-time environment values into emitted statements.",
    "Normalize uint tokens to int in rewritten output.",
    "Strip residual stretch and duration forms in post-processing.",
)


@dataclass(slots=True)
class Issue:
    start: int
    end: int
    kind: str
    detail: str


class AerRunSignals(QObject):
    finished = Signal(int, object, object, object, object)


class AerRunWorker(QRunnable):
    def __init__(self, token: int, circuit: Any, shots: int = 1024) -> None:
        super().__init__()
        self.token = token
        self.circuit = circuit
        self.shots = shots
        self.signals = AerRunSignals()

    def run(self) -> None:
        run_timestamp = datetime.now()
        try:
            counts = run_circuit_counts(self.circuit, self.shots)
            error = None
        except Exception as exc:
            counts = None
            error = str(exc)
        self.signals.finished.emit(self.token, self.circuit, counts, error, run_timestamp)


def kind(node: Any) -> str:
    return type(node).__name__


def span(node: Any) -> Any:
    return getattr(node, "span", None)


def to_pos(editor: QPlainTextEdit, line: int, column: int) -> int:
    block = editor.document().findBlockByNumber(max(0, line))
    if not block.isValid():
        return max(0, len(editor.toPlainText()))
    pos = block.position() + max(0, column)
    return max(0, min(pos, max(0, len(editor.toPlainText()) - 1)))


def clamp_cursor_pos(editor: QPlainTextEdit, pos: int) -> int:
    limit = max(0, len(editor.toPlainText()) - 1)
    return max(0, min(pos, limit))


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


AUTO_PARAM_DEFAULT_EXPR = "pi/2 - 1"
AUTO_PARAM_DEFAULT_VALUE = (math.pi / 2.0) - 1.0


def run_circuit_counts(circuit: Any, shots: int = 1024) -> Any:
    backend = AerSimulator()
    if getattr(circuit, "num_parameters", 0):
        circuit = circuit.assign_parameters({parameter: AUTO_PARAM_DEFAULT_VALUE for parameter in circuit.parameters})
    compiled = transpile(circuit, backend)
    result = backend.run(compiled, shots=shots).result()
    return result.get_counts()


def run_aer_job(circuit: Any, shots: int) -> tuple[Any | None, str | None, datetime]:
    run_timestamp = datetime.now(timezone.utc)
    try:
        return run_circuit_counts(circuit, shots), None, run_timestamp
    except Exception as exc:
        return None, str(exc), run_timestamp


def format_elapsed_time(seconds: float) -> str:
    total_milliseconds = max(0, int(round(seconds * 1000.0)))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def format_utc_timestamp(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    return utc_value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"


def format_counts_readable(run_counts: Any) -> list[str]:
    if not isinstance(run_counts, dict):
        return [f"Raw counts: {run_counts}"]

    rows: list[tuple[str, int]] = []
    for reading, count in run_counts.items():
        try:
            rows.append((str(reading), int(count)))
        except Exception:
            rows.append((str(reading), 0))

    if not rows:
        return ["No measurement counts returned."]

    rows.sort(key=lambda item: (-item[1], item[0]))
    total_shots = sum(count for _, count in rows)

    lines = [
        "Measurement outcomes:",
        "  reading -> occurrences (share of shots)",
    ]
    for reading, count in rows:
        pct = (100.0 * count / total_shots) if total_shots else 0.0
        lines.append(f"  {reading} -> {count} ({pct:.2f}%)")
    return lines


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
    
    # Check if this is a comparison of a simple identifier to a boolean value
    # (single bit vs bit array)
    k = kind(expr)
    if k == "BinaryExpression":
        op = getattr(expr, "op", None)
        op_name = getattr(op, "name", None) if hasattr(op, "name") else str(op)
        if op_name == "==":
            lhs = getattr(expr, "lhs", None)
            rhs = getattr(expr, "rhs", None)
            lhs_k = kind(lhs)
            rhs_k = kind(rhs)
            
            # For simple identifier == constant patterns
            if lhs_k == "Identifier" and rhs_k == "IntegerLiteral":
                ident_name = getattr(lhs, "name", "")
                rhs_value = getattr(rhs, "value", None)
                if ident_name and rhs_value is not None:
                    # Single bit == 1 should become == true
                    if rhs_value == 1:
                        return f"{ident_name} == true"
                    # Single bit == 0 should become == false
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


def emit_stmt(stmt: Any, env: dict[str, Any], issues: list[Issue], indent: int = 0) -> list[str]:
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
        issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), f"cannot fold constant {name}"))
        return []
    if k == "ClassicalDeclaration":
        if is_supported_decl(stmt):
            return [pad + dumps(stmt).strip()]
        name = getattr(getattr(stmt, "identifier", None), "name", "")
        value = eval_node(getattr(stmt, "init_expression", None), env)
        if value is not None:
            env[name] = value
            return []
        issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), f"drop unsupported declaration {name}"))
        return []
    if k == "ClassicalAssignment":
        lvalue = getattr(stmt, "lvalue", None)
        rvalue = getattr(stmt, "rvalue", None)
        op = getattr(getattr(stmt, "op", None), "name", None) or str(getattr(stmt, "op", ""))
        lvalue_kind = kind(lvalue)
        rvalue_kind = kind(rvalue)

        # Assignments into array elements/slices are not supported by the Qiskit
        # importer, and attempting to substitute folded Python lists will
        # produce invalid OpenQASM.  Drop these gracefully.
        if lvalue_kind == "IndexExpression":
            issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "drop array/slice assignment (not supported by qiskit importer)"))
            return []

        # If this is a simple compile-time update, fold it into the environment
        # so it can be substituted into later quantum operations and loop bounds.
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

        # Qiskit-qasm3-import does not support `def` subroutines; drop calls.
        if rvalue_kind == "FunctionCall":
            issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "drop subroutine call assignment (not supported by qiskit importer)"))
            return []

        # Otherwise, keep the statement (this is needed for dynamic-circuit style
        # code such as `mid[0] = measure q[0];`).
        try:
            # Avoid substituting folded array literals into the emitted OpenQASM;
            # this can generate invalid syntax.  Classical assignments are kept
            # verbatim unless they were folded above.
            return [pad + dumps(stmt).strip()]
        except Exception:
            issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "cannot emit classical assignment"))
            return []
    if k == "ForInLoop":
        values = range_values(getattr(stmt, "set_declaration", None), env)
        if values is None:
            issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "loop range is not statically known"))
            return []
        # Avoid output blow-ups when the source uses large shot loops.
        # Qiskit importer can represent loops, but it cannot parse most of the
        # classical machinery that tends to appear around these examples anyway.
        MAX_UNROLL = 256
        if len(values) > MAX_UNROLL:
            issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), f"loop range too large to unroll ({len(values)} > {MAX_UNROLL})"))
            return []
        out: list[str] = []
        ident = getattr(getattr(stmt, "identifier", None), "name", "")
        for value in values:
            next_env = dict(env)
            next_env[ident] = value
            for inner in getattr(stmt, "block", []):
                out.extend(emit_stmt(inner, next_env, issues, indent))
        return out
    if k == "BranchingStatement":
        cond_value = eval_node(getattr(stmt, "condition", None), env)
        if isinstance(cond_value, bool):
            block = getattr(stmt, "if_block" if cond_value else "else_block", [])
            out: list[str] = []
            for inner in block:
                out.extend(emit_stmt(inner, env, issues, indent))
            return out
        cond_text = rewrite_condition_text(getattr(stmt, "condition", None), env)
        if cond_text is None:
            issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "condition cannot be rewritten for qiskit"))
            return []
        cond_text = subst_env(cond_text, env)
        out = [pad + f"if ({cond_text}) {{"]
        for inner in getattr(stmt, "if_block", []):
            out.extend(emit_stmt(inner, env, issues, indent + 1))
        if getattr(stmt, "else_block", []):
            out.append(pad + "} else {")
            for inner in getattr(stmt, "else_block", []):
                out.extend(emit_stmt(inner, env, issues, indent + 1))
        out.append(pad + "}")
        return out
    if k == "Box":
        out: list[str] = []
        for inner in getattr(stmt, "body", []):
            out.extend(emit_stmt(inner, env, issues, indent))
        return out
    if k in {
        "CalibrationGrammarDeclaration",
        "CalibrationDefinition",
        "ExternDeclaration",
        "StretchDeclaration",
        "DurationDeclaration",
        "ReturnStatement",
        "ExpressionStatement",
        "QuantumDelay",
    }:
        issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "not supported by qiskit importer"))
        return []
    if k == "DelayInstruction":
        # Drop delays whose duration depends on timing constructs we do not keep
        # (stretch, duration declarations, durationof).
        duration_expr = getattr(stmt, "duration", None)
        if duration_expr is not None:
            if kind(duration_expr) == "Identifier" and getattr(duration_expr, "name", "") not in env:
                issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "drop delay (duration depends on dropped timing symbol)"))
                return []
            if contains_timing_constructs(duration_expr):
                issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "drop delay (unsupported timing construct)"))
                return []
        issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "drop delay (not supported by qiskit importer)"))
        return []
    if k == "QuantumReset":
        return [pad + subst_env(dumps(stmt).strip(), env)]
    if k == "QuantumMeasurementStatement":
        return [pad + subst_env(dumps(stmt).strip(), env)]
    if k == "QuantumGate":
        text = subst_env(dumps(stmt).strip(), env)
        if re.match(r"^u\b", text):
            text = re.sub(r"^u\b", "U(0, 0, 0)", text)
        return [pad + text]
    if k == "QuantumGateDefinition":
        # Re-emit gate definitions with rewritten bodies so we can unroll static
        # loops inside the gate body (Qiskit does not reliably parse these).
        name = getattr(getattr(stmt, "name", None), "name", "")
        params: list[str] = []
        for arg in getattr(stmt, "arguments", []) or []:
            # openqasm3 stores gate parameters as Identifiers.
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
            # Fallback to serializer if we cannot confidently reconstruct header.
            return [pad + dumps(stmt).strip()]

        header = f"gate {name}"
        if params:
            header += "(" + ", ".join(params) + ")"
        header += " " + ", ".join(qubits) + " {"

        out: list[str] = [pad + header]
        for inner in getattr(stmt, "body", []) or []:
            out.extend(emit_stmt(inner, env, issues, indent + 1))
        out.append(pad + "}")
        return out
    if k == "SubroutineDefinition":
        name = getattr(getattr(stmt, "name", None), "name", "")
        issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), f"drop subroutine definition {name or ''}".strip()))
        return []
    if k in {"Program", "StatementOrScope"}:
        out: list[str] = []
        for inner in getattr(stmt, "statements", getattr(stmt, "block", [])):
            out.extend(emit_stmt(inner, env, issues, indent))
        return out
    if k == "Annotation":
        return []
    try:
        return [pad + subst_env(dumps(stmt).strip(), env)]
    except Exception:
        issues.append(Issue(stmt.span.start_line, stmt.span.end_line, k.lower(), "cannot emit statement"))
        return []


def extract_qasm_version(source: str) -> str:
    """Extract OPENQASM version from source header. Returns '3.0', '3.1', or '3.0' (default)."""
    for line in source.split('\n'):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        # Look for OPENQASM version header
        if stripped.upper().startswith('OPENQASM'):
            if '3.1' in stripped:
                return '3.1'
            elif '3.0' in stripped:
                return '3.0'
            # Fallback to 3.0 if version not recognized
            return '3.0'
    return '3.0'  # Default to 3.0 if no header found


def transpile_qasm(source: str) -> tuple[str, list[Issue], Any | None]:
    # Respect explicit OpenQASM 3.1 sources: do not rewrite them to 3.0.
    version = extract_qasm_version(source)
    if version == "3.1":
        program = parse(source)
        return source, [], program

    # Parse the *original* text first so span offsets match the editor.  This is
    # important for include gray-highlighting and other UI range markers.
    program_original: Any | None
    try:
        program_original = parse(source)
    except Exception:
        program_original = None

    def strip_calibration_blocks_preserve_lines(text: str) -> str:
        # Keep newline count stable by replacing removed lines with blank lines.
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

    # For rewriting, fall back to a calibration-stripped parse if needed.
    try:
        program = program_original if program_original is not None else parse(source)
    except Exception:
        stripped = strip_calibration_blocks_preserve_lines(source)
        # If the file has no version line at all, ensure the stripped form is parsable.
        if not re.search(r"(?mi)^[ \t]*OPENQASM\b", stripped):
            stripped = "OPENQASM 3.0;\n" + stripped
        program = parse(stripped)
    env: dict[str, Any] = {}
    issues: list[Issue] = []
    lines: list[str] = []
    lines.append("OPENQASM 3.0;")
    for stmt in program.statements:
        # Keep Qiskit-style input declarations verbatim.
        if kind(stmt) == "IODeclaration":
            try:
                lines.append(dumps(stmt).strip())
            except Exception:
                issues.append(Issue(stmt.span.start_line, stmt.span.end_line, "iodeclaration", "cannot emit IO declaration"))
            continue
        lines.extend(emit_stmt(stmt, env, issues, 0))

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
            # Ignore calibration/OpenPulse sections when inferring qubit decls.
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
    # If the source uses standard gates (CX, U, etc.) but does not include
    # stdgates.inc, expand the compatibility gate definitions automatically
    # so the rewritten output can be imported by Qiskit.  Do not insert defs
    # that collide with user-defined gate names.
    stdgates_defs = stdgates_compat_lines()
    stdgates_names: list[str] = []
    import re as _re
    for defline in stdgates_defs:
        m = _re.match(r"^gate\s+([A-Za-z_]\w*)", defline)
        if m:
            stdgates_names.append(m.group(1))

    # Collect user-defined gate names from the AST to avoid re-defining them.
    user_defined: set[str] = set()
    try:
        for stmt in getattr(program, "statements", []):
            if kind(stmt) == "QuantumGateDefinition":
                # Some AST nodes use `name` (Identifier) for gate defs
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

    joined = "\n".join(line for line in lines if line.strip())
    # Only add defs if at least one std gate name is referenced and the defs
    # are not already present in the emitted lines.
    if stdgates_names:
        pattern = _re.compile(r"\b(" + "|".join(_re.escape(n) for n in stdgates_names) + r")\b")
        has_std_ref = bool(pattern.search(joined))
        has_defs = any(defline.strip() in (ln.strip() for ln in lines) for defline in stdgates_defs)
        if has_std_ref and not has_defs:
            # Insert stdgates after an initial OPENQASM header if present,
            # otherwise at the beginning.
            # Filter out stdgates that would collide with user-defined gates
            filtered_defs = []
            filtered_names = []
            for defline in stdgates_defs:
                m = _re.match(r"^gate\s+([A-Za-z_]\w*)", defline)
                if m:
                    nm = m.group(1)
                    if nm in user_defined:
                        continue
                    filtered_defs.append(defline)
                    filtered_names.append(nm)

            original_lines = [ln for ln in lines if ln.strip()]
            if original_lines and original_lines[0].strip().upper().startswith("OPENQASM"):
                out_lines = [original_lines[0]] + filtered_defs + original_lines[1:]
                start_idx = 1 + len(filtered_defs)
            else:
                out_lines = filtered_defs + original_lines
                start_idx = len(filtered_defs)

            # Normalize usages of standard gate names in the original portion
            # to the lowercase names we defined above (e.g. replace `CX` -> `cx`).
            for i in range(start_idx, len(out_lines)):
                line = out_lines[i]
                for name in (filtered_names if 'filtered_names' in locals() else stdgates_names):
                    up = name.upper()
                    if up == name:
                        continue
                    line = _re.sub(rf"\b{_re.escape(up)}\b", name, line)
                out_lines[i] = line

            joined = "\n".join(out_lines)

    # Normalize hardware-qubit identifiers like $0 into a declared qubit
    # register.  Detect `$<number>` usages in the emitted text, compute the
    # required register size, choose a non-colliding name, insert a
    # declaration and rewrite occurrences to `<hwname>[<index>]`.
    hw_matches = _re.findall(r"\$([0-9]+)", joined)
    if hw_matches:
        hw_indices = [int(x) for x in hw_matches]
        max_hw = max(hw_indices)
        hw_name = "__hw"
        # Ensure the chosen name does not collide with existing tokens in
        # the emitted text.
        while _re.search(rf"\b{_re.escape(hw_name)}\b", joined):
            hw_name += "_"
        hw_decl = f"qubit[{max_hw + 1}] {hw_name};"
        # Rewrite `$N` occurrences to `hw_name[N]`.
        joined = _re.sub(r"\$([0-9]+)", lambda m: f"{hw_name}[{int(m.group(1))}]", joined)
        # Prepend hardware qubit declaration to any inferred qubit decls.
        if 'qubit_decl_lines' in locals() and qubit_decl_lines:
            qubit_decl_lines.insert(0, hw_decl)
        else:
            qubit_decl_lines = [hw_decl]

    if qubit_decl_lines:
        out_lines = joined.splitlines()
        insert_at = 0
        in_gate_def = False
        for idx, line in enumerate(out_lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.upper().startswith("OPENQASM"):
                insert_at = idx + 1
                continue
            if in_gate_def:
                if not line[: len(line) - len(line.lstrip())] and stripped == "}":
                    insert_at = idx + 1
                    in_gate_def = False
                continue
            if stripped.startswith("gate "):
                in_gate_def = True
                insert_at = idx + 1
                continue
            break
        out_lines[insert_at:insert_at] = qubit_decl_lines
        joined = "\n".join(out_lines)

    rewritten = joined + "\n"
    rewritten = re.sub(r"\buint\b", "int", rewritten)
    rewritten = re.sub(r"\bstretch\s+\w+;\n?", "", rewritten)
    rewritten = re.sub(r"\bduration\s+\w+\s*=.*?;\n?", "", rewritten)
    # Do not strip delay instructions globally; Qiskit can represent delays.
    return rewritten, issues, (program_original if program_original is not None else program)


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


def make_tree(program: Any) -> QTreeWidgetItem:
    root = QTreeWidgetItem(["Program"])
    root.setData(0, Qt.ItemDataRole.UserRole, program)

    def add(parent: QTreeWidgetItem, node: Any) -> None:
        label = kind(node)
        if kind(node) == "Identifier":
            label = f"Identifier: {getattr(node, 'name', '')}"
        elif kind(node) in {"QuantumGateDefinition", "QuantumGate", "ClassicalDeclaration", "ConstantDeclaration", "QubitDeclaration", "ForInLoop", "BranchingStatement"}:
            label = f"{kind(node)}: {getattr(getattr(node, 'identifier', None), 'name', getattr(getattr(node, 'name', None), 'name', ''))}"
        item = QTreeWidgetItem([label])
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        parent.addChild(item)
        for child in node_iter(node):
            add(item, child)

    for stmt in getattr(program, "statements", []):
        add(root, stmt)
    return root


def span_offsets(editor: QPlainTextEdit, node: Any) -> tuple[int, int] | None:
    s = span(node)
    if not s:
        return None
    # Spans use 1-indexed line numbers, convert to 0-indexed for to_pos
    start = to_pos(editor, s.start_line - 1, s.start_column)
    end = clamp_cursor_pos(editor, to_pos(editor, s.end_line - 1, s.end_column + 1))
    return start, end


def mark_unsupported(program: Any, editor: QPlainTextEdit) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []

    def add_span(node: Any, reason: str) -> None:
        off = span_offsets(editor, node)
        if off:
            spans.append((off[0], off[1], reason))

    def walk(node: Any) -> None:
        if not dataclasses.is_dataclass(node):
            return
        name = kind(node)
        if name in {"ConstantDeclaration", "ExternDeclaration", "CalibrationGrammarDeclaration", "CalibrationDefinition", "QuantumDelay", "Box"}:
            reason_map = {
                "ConstantDeclaration": "Folded away during rewrite because constants are resolved at compile time.",
                "ExternDeclaration": "Removed because extern declarations are not supported by the qiskit importer.",
                "CalibrationGrammarDeclaration": "Removed because calibration grammar is not supported by the qiskit importer.",
                "CalibrationDefinition": "Removed because calibration definitions are not supported by the qiskit importer.",
                "QuantumDelay": "Removed because delay operations are not supported by the qiskit importer.",
                "Box": "Unboxed during rewrite; only inner statements are kept.",
            }
            add_span(node, reason_map.get(name, "Rewritten for compatibility."))
        if name == "ClassicalDeclaration" and kind(getattr(node, "type", None)) in {"IntType", "UintType", "FloatType", "ArrayType", "StretchType", "DurationType"}:
            add_span(node, "Unsupported classical type for importer; declaration is dropped unless it can be folded.")
        if name == "ForInLoop" and range_values(getattr(node, "set_declaration", None), {}) is None:
            add_span(node, "Loop cannot be unrolled because range bounds are not statically known.")
        if name == "BranchingStatement" and rewrite_condition_text(getattr(node, "condition", None), {}) is None and eval_node(getattr(node, "condition", None), {}) is None:
            add_span(node, "Condition cannot be rewritten into qiskit-compatible form.")
        for child in node_iter(node):
            walk(child)

    walk(program)
    return spans


def mark_includes(program: Any, editor: QPlainTextEdit) -> list[tuple[int, int, str]]:
    """Mark include statements with a special type for dark-gray linking."""
    spans: list[tuple[int, int, str]] = []

    def walk(node: Any) -> None:
        if not dataclasses.is_dataclass(node):
            return
        name = kind(node)
        if name == "Include":
            filename = getattr(node, "filename", "")
            off = span_offsets(editor, node)
            if off:
                spans.append((off[0], off[1], f"include:{filename}"))
        for child in node_iter(node):
            walk(child)

    walk(program)
    return spans


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event: Any) -> None:
        self.editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.line_number_area = LineNumberArea(self)
        self._issue_ranges: list[tuple[int, int, str]] = []
        self._include_ranges: list[tuple[int, int, str]] = []
        self._hovered_issue_index: int | None = None
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setMouseTracking(True)
        self.update_line_number_area_width(0)

    def line_number_area_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def paint_line_numbers(self, event: Any) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#1f1f1f"))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#808080"))
                painter.drawText(0, top, self.line_number_area.width() - 4, self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            number += 1

    def highlight_current_line(self) -> None:
        extra = []
        if not self.isReadOnly():
            selection = cast(Any, QTextEdit).ExtraSelection()
            selection.format.setBackground(QColor("#fff9c4"))
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extra.append(selection)
        for start, end, _ in self._include_ranges:
            start = clamp_cursor_pos(self, start)
            end = clamp_cursor_pos(self, end)
            if end < start:
                continue
            selection = cast(Any, QTextEdit).ExtraSelection()
            selection.format.setBackground(QColor("#e0e0e0"))
            selection.format.setForeground(QColor("#303030"))
            cursor = self.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            extra.append(selection)
        for start, end, _ in self._issue_ranges:
            start = clamp_cursor_pos(self, start)
            end = clamp_cursor_pos(self, end)
            if end < start:
                continue
            selection = cast(Any, QTextEdit).ExtraSelection()
            selection.format.setBackground(QColor("#dcedc8"))
            selection.format.setForeground(QColor("#1b1b1b"))
            cursor = self.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            extra.append(selection)
        self.setExtraSelections(extra)

    def issue_index_at_position(self, pos: int) -> int | None:
        for i, (start, end, _) in enumerate(self._issue_ranges):
            if start <= pos <= end:
                return i
        return None

    def include_index_at_position(self, pos: int) -> int | None:
        for i, (start, end, _) in enumerate(self._include_ranges):
            if start <= pos <= end:
                return i
        return None

    def mouseMoveEvent(self, event: Any) -> None:
        pos = self.cursorForPosition(event.position().toPoint()).position()
        issue_index = self.issue_index_at_position(pos)
        include_index = self.include_index_at_position(pos) if issue_index is None else None
        
        if issue_index is None and include_index is None:
            if self._hovered_issue_index is not None:
                QToolTip.hideText()
                self._hovered_issue_index = None
            super().mouseMoveEvent(event)
            return

        if issue_index is not None and issue_index != self._hovered_issue_index:
            reason = self._issue_ranges[issue_index][2]
            QToolTip.showText(event.globalPosition().toPoint(), reason, self)
            self._hovered_issue_index = issue_index
        elif include_index is not None and include_index != self._hovered_issue_index:
            filename = self._include_ranges[include_index][2].split(":", 1)[1]
            QToolTip.showText(event.globalPosition().toPoint(), f"Include: {filename}\n(expanded in right pane)", self)
            self._hovered_issue_index = include_index
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: Any) -> None:
        QToolTip.hideText()
        self._hovered_issue_index = None
        super().leaveEvent(event)

    def set_issue_spans(self, ranges: list[tuple[int, int, str]]) -> None:
        self._issue_ranges = ranges
        self._hovered_issue_index = None
        QToolTip.hideText()
        self.highlight_current_line()

    def set_include_spans(self, ranges: list[tuple[int, int, str]]) -> None:
        self._include_ranges = ranges
        self._hovered_issue_index = None
        QToolTip.hideText()
        self.highlight_current_line()


class CircuitView(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._dragging = False
        self._drag_start_pos: tuple[int, int] | None = None
        self._scroll_bar_start: tuple[int, int] | None = None
        self.show_placeholder()

    def create_placeholder_pixmap(self, width: int = 400, height: int = 300) -> QPixmap:
        """Create a placeholder pixmap with a crossed X icon."""
        pixmap = QPixmap(width, height)
        pixmap.fill(QColor("#f5f5f5"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#cccccc"))
        painter.drawRect(0, 0, width - 1, height - 1)
        
        pen = painter.pen()
        pen.setWidth(3)
        pen.setColor(QColor("#dddddd"))
        painter.setPen(pen)
        painter.drawLine(int(width * 0.2), int(height * 0.2), int(width * 0.8), int(height * 0.8))
        painter.drawLine(int(width * 0.8), int(height * 0.2), int(width * 0.2), int(height * 0.8))
        painter.end()
        return pixmap

    def show_placeholder(self) -> None:
        """Display a placeholder when no circuit is available."""
        self.scene().clear()
        placeholder = self.create_placeholder_pixmap(self.width() or 400, self.height() or 300)
        self.scene().addPixmap(placeholder)
        self.fitInView(self.scene().itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def set_image(self, image_bytes: bytes) -> None:
        self.scene().clear()
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_bytes):
            self.show_placeholder()
            return
        self.scene().addPixmap(pixmap)
        self.fitInView(self.scene().itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            pos = event.position().toPoint()
            self._drag_start_pos = (pos.x(), pos.y())
            self._scroll_bar_start = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value()
            )
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._dragging and self._drag_start_pos and self._scroll_bar_start:
            pos = event.position().toPoint()
            dx = pos.x() - self._drag_start_pos[0]
            dy = pos.y() - self._drag_start_pos[1]
            self.horizontalScrollBar().setValue(self._scroll_bar_start[0] - dx)
            self.verticalScrollBar().setValue(self._scroll_bar_start[1] - dy)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._drag_start_pos = None
            self._scroll_bar_start = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: Any) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def contextMenuEvent(self, event: Any) -> None:
        menu = QMenu(self)
        menu.addAction("Reset zoom", lambda: self.fitInView(self.scene().itemsBoundingRect(), Qt.AspectRatioMode.KeepAspectRatio))
        menu.exec(event.globalPos())


class RulesDialog(QDialog):
    def __init__(self, text: str, parent: QWidget | None = None, title: str = "Rewrite rules") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        box = QTextEdit()
        box.setReadOnly(True)
        box.setPlainText(text)
        layout.addWidget(box)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class SearchDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Find QASM")
        self.resize(420, 160)

        layout = QFormLayout(self)
        self.query = QLineEdit()
        self.scope = QComboBox()
        self.scope.addItems(["Original QASM", "Rewritten QASM", "Both"])
        self.case_sensitive = QCheckBox("Case sensitive")

        layout.addRow("Find:", self.query)
        layout.addRow("Search in:", self.scope)
        layout.addRow("", self.case_sensitive)

        buttons = QHBoxLayout()
        find_button = QPushButton("Find next")
        close_button = QPushButton("Close")
        find_button.clicked.connect(self._find_next)
        self.query.returnPressed.connect(self._find_next)
        close_button.clicked.connect(self.reject)
        buttons.addWidget(find_button)
        buttons.addWidget(close_button)
        layout.addRow(buttons)

    def text(self) -> str:
        return self.query.text().strip()

    def search_scope(self) -> str:
        return self.scope.currentText()

    def is_case_sensitive(self) -> bool:
        return self.case_sensitive.isChecked()

    def _find_next(self) -> None:
        parent = self.parent()
        if parent is None:
            return
        find_text = getattr(parent, "find_text", None)
        if callable(find_text):
            find_text(self.text(), self.search_scope(), self.is_case_sensitive())


class MainWindow(QMainWindow):
    def make_titled_panel(self, title: str, color: str, content: QWidget) -> tuple[QWidget, QLabel]:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title_bar = QLabel(title)
        title_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_bar.setFixedHeight(24)
        title_bar.setStyleSheet(
            f"background-color: {color}; color: #111111; font-weight: 600; "
            "border-bottom: 1px solid #888888;"
        )

        layout.addWidget(title_bar)
        layout.addWidget(content)
        return panel, title_bar

    def __init__(self) -> None:
        super().__init__()
        self.base_title = "QASM3 Aer Lab"
        self.setWindowTitle(self.base_title)
        self._syncing = False
        self._aer_run_token = 0
        self._aer_stopwatch_timer = QTimer(self)
        self._aer_stopwatch_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._aer_stopwatch_timer.setInterval(50)
        self._aer_stopwatch_timer.timeout.connect(self._refresh_aer_run_state)
        self._aer_run_start_monotonic: float | None = None
        self._aer_executor: concurrent.futures.ProcessPoolExecutor | None = None
        self._aer_future: concurrent.futures.Future[tuple[Any | None, str | None, datetime]] | None = None
        self._aer_future_token: int | None = None
        self._aer_future_circuit: Any | None = None
        self._search_dialog: SearchDialog | None = None
        self.current_program: Any | None = None
        self.font_size = 10
        # Number of shots to use for Qiskit/Aer runs (user-configurable)
        self.shots = 1024
        self._aer_timeout_seconds = 30

        self.editor = CodeEditor()
        self.editor.textChanged.connect(self.debounced_refresh)
        self.editor.cursorPositionChanged.connect(self.sync_tree_from_cursor)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemSelectionChanged.connect(self.sync_editor_from_tree)
        self.output = QPlainTextEdit()
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output.setReadOnly(True)
        self.circuit = CircuitView()
        self.circuit_info = QPlainTextEdit()
        self.circuit_info.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.circuit_info.setReadOnly(True)

        circuit_panel = QSplitter(Qt.Orientation.Vertical)
        circuit_panel.addWidget(self.circuit)
        circuit_panel.addWidget(self.circuit_info)
        circuit_panel.setStretchFactor(0, 4)
        circuit_panel.setStretchFactor(1, 1)
        circuit_panel.setSizes([300, 80])

        top = QSplitter(Qt.Orientation.Horizontal)
        editor_panel, _ = self.make_titled_panel("QASM original", "#d8ecff", self.editor)
        output_panel, self.output_title = self.make_titled_panel("Qiskit importer", "#ffe7c2", self.output)
        top.addWidget(editor_panel)
        top.addWidget(output_panel)
        bottom = QSplitter(Qt.Orientation.Horizontal)
        tree_title = "AST parse-tree (original -> openqasm3/antlr4)"
        tree_panel, tree_label = self.make_titled_panel(tree_title, "#d9f3d6", self.tree)
        tree_panel.setMinimumWidth(tree_label.fontMetrics().horizontalAdvance(tree_title) + 24)
        circuit_panel_titled, _ = self.make_titled_panel("Qiskit AER runtime (rewritten -> qiskit_qasm3_import)", "#ffd9d9", circuit_panel)
        bottom.addWidget(tree_panel)
        bottom.addWidget(circuit_panel_titled)
        root = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(top)
        root.addWidget(bottom)
        root.setStretchFactor(0, 3)
        root.setStretchFactor(1, 2)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 2)
        bottom.setStretchFactor(0, 2)
        bottom.setStretchFactor(1, 3)
        self.setCentralWidget(root)

        self._aer_stopwatch_label = QLabel("")
        self._aer_stopwatch_label.setVisible(False)
        self._aer_stopwatch_label.setStyleSheet("font-weight: 700; color: #1f6f2a; padding-left: 8px; padding-right: 8px;")
        self.statusBar().addPermanentWidget(self._aer_stopwatch_label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.refresh_views)

        self.build_menu()
        self.apply_font()
        self.load_default()

    def build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        open_action = QAction("Open...", self)
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)
        examples_menu = file_menu.addMenu("Examples")
        for path in sorted(EXAMPLES.glob("*.qasm")) + sorted(EXAMPLES.glob("*.inc")):
            action = QAction(path.name, self)
            action.triggered.connect(lambda _=False, p=path: self.load_path(p))
            examples_menu.addAction(action)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("View")
        zoom_in = QAction("Zoom in", self)
        zoom_in.setShortcut(QKeySequence.StandardKey.ZoomIn)
        zoom_in.triggered.connect(lambda: self.set_font_size(self.font_size + 1))
        view_menu.addAction(zoom_in)
        zoom_out = QAction("Zoom out", self)
        zoom_out.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out.triggered.connect(lambda: self.set_font_size(max(7, self.font_size - 1)))
        view_menu.addAction(zoom_out)
        reset_font = QAction("Reset font", self)
        reset_font.triggered.connect(lambda: self.set_font_size(10))
        view_menu.addAction(reset_font)
        find_action = QAction("Find...", self)
        find_action.setShortcut(QKeySequence.StandardKey.Find)
        find_action.triggered.connect(self.show_search_dialog)
        view_menu.addAction(find_action)
        rewrite_action = QAction("Analyze rewritten as if original", self)
        rewrite_action.setShortcut("Ctrl+E")
        rewrite_action.setStatusTip("Replace QASM original with the rewritten importer-compatible text")
        rewrite_action.triggered.connect(self.rewrite_current)
        view_menu.addAction(rewrite_action)

        run_menu = self.menuBar().addMenu("Run")
        run_action = QAction("Run manually (w/ params)", self)
        run_action.setShortcut("Ctrl+R")
        run_action.triggered.connect(self.run_current)
        run_menu.addAction(run_action)
        self.set_shots_action = QAction(f"Qiskit shots ({self.shots})...", self)
        self.set_shots_action.setStatusTip("Configure number of shots for Qiskit/Aer runs")
        self.set_shots_action.triggered.connect(self.set_shots_dialog)
        run_menu.addAction(self.set_shots_action)
        self.set_aer_timeout_action = QAction(self._aer_timeout_action_text(), self)
        self.set_aer_timeout_action.setStatusTip("Configure the maximum AER run time in seconds (0 = no timeout)")
        self.set_aer_timeout_action.triggered.connect(self.set_aer_timeout_dialog)
        run_menu.addAction(self.set_aer_timeout_action)
        diag_action = QAction("Diagnostics", self)
        diag_action.setShortcut("Ctrl+D")
        diag_action.triggered.connect(self.show_diagnostics)
        run_menu.addAction(diag_action)

        help_menu = self.menuBar().addMenu("Help")
        rules_action = QAction("Rewrite rules", self)
        rules_action.triggered.connect(self.show_rules)
        help_menu.addAction(rules_action)
        links = self.repository_links()
        for label, url in links:
            if url.endswith("/gpl-3.0.en.html"):
                license_action = QAction("License", self)
                license_action.triggered.connect(lambda _=False, u=url: webbrowser.open(u, new=2))
                help_menu.addAction(license_action)
                break
        repos_menu = help_menu.addMenu("Repositories")
        for label, url in links:
            if url.endswith("/gpl-3.0.en.html"):
                continue
            action = QAction(label, self)
            action.triggered.connect(lambda _=False, u=url: webbrowser.open(u, new=2))
            repos_menu.addAction(action)

    def repository_links(self) -> list[tuple[str, str]]:
        links: list[tuple[str, str]] = []
        if not REPO_URLS.exists():
            return links
        for raw in REPO_URLS.read_text().splitlines():
            url = raw.strip()
            if not url or url.startswith("#"):
                continue
            path = urlparse(url).path.rstrip("/")
            tail = path.split("/")[-1] if path else url
            tail = tail.removesuffix(".git")
            links.append((tail.capitalize(), url))
        return links

    def apply_font(self) -> None:
        font = QFont("DejaVu Sans Mono", self.font_size)
        for widget in (self.editor, self.output, self.tree, self.circuit_info):
            widget.setFont(font)

    def set_font_size(self, size: int) -> None:
        self.font_size = size
        self.apply_font()

    def load_default(self) -> None:
        default = EXAMPLES / "adder.qasm"
        if default.exists():
            self.load_path(default)

    def show_search_dialog(self) -> None:
        if self._search_dialog is None:
            self._search_dialog = SearchDialog(self)
        if self._search_dialog.exec() == QDialog.DialogCode.Accepted:
            self.find_text(self._search_dialog.text(), self._search_dialog.search_scope(), self._search_dialog.is_case_sensitive())

    def _find_in_widget(self, widget: QPlainTextEdit, query: str, case_sensitive: bool) -> bool:
        if not query:
            return False
        flags = QTextDocument.FindFlag(0)
        if case_sensitive:
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        cursor = widget.textCursor()
        if cursor.hasSelection():
            cursor.setPosition(cursor.selectionEnd())
        found = widget.document().find(query, cursor, flags)
        if found.isNull():
            found = widget.document().find(query, QTextCursor(widget.document()), flags)
        if found.isNull():
            return False
        widget.setTextCursor(found)
        widget.ensureCursorVisible()
        return True

    def find_text(self, query: str, scope: str, case_sensitive: bool) -> None:
        if not query:
            return
        widgets: list[QPlainTextEdit]
        if scope == "Original QASM":
            widgets = [self.editor]
        elif scope == "Rewritten QASM":
            widgets = [self.output]
        else:
            widgets = [self.editor, self.output]

        for widget in widgets:
            if self._find_in_widget(widget, query, case_sensitive):
                widget.setFocus()
                self.statusBar().showMessage(f"Found '{query}' in {scope.lower()}", 3000)
                return

        self.statusBar().showMessage(f"'{query}' not found", 3000)

    def open_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Open QASM", str(EXAMPLES), "QASM files (*.qasm *.inc);;All files (*)")
        if name:
            self.load_path(Path(name))

    def load_path(self, path: Path) -> None:
        self._syncing = True
        try:
            self.editor.setPlainText(path.read_text())
            # Clear any previously-marked spans immediately to avoid stale
            # include/issue highlights showing before the view is refreshed.
            self.editor.set_issue_spans([])
            self.editor.set_include_spans([])
            self.setWindowTitle(f"{self.base_title} - {path.resolve()}")
            self.statusBar().showMessage(f"Loaded {path}")
        finally:
            self._syncing = False
        self.refresh_views()

    def debounced_refresh(self) -> None:
        if not self._syncing:
            self._timer.start()

    def refresh_views(self) -> None:
        source = self.editor.toPlainText()
        try:
            rewritten, issues, program = transpile_qasm(source)
        except Exception as exc:
            self.current_program = None
            self.tree.clear()
            self.circuit.show_placeholder()
            self.circuit_info.clear()
            self.editor.set_issue_spans([])
            self.set_importer_output(None, f"Parse error: {exc}", issues=[])
            return
        self.current_program = program
        qiskit_error = ""
        circuit = None
        try:
            circuit = qiskit_parse(rewritten)
        except Exception as exc:
            qiskit_error = str(exc)
        self.set_importer_output(rewritten, qiskit_error, circuit is not None, issues)
        self.tree.clear()
        if program is not None:
            self.tree.addTopLevelItem(make_tree(program))
            self.tree.expandToDepth(2)
        self.editor.set_issue_spans(mark_unsupported(program, self.editor) if program else [])
        self.editor.set_include_spans(mark_includes(program, self.editor) if program else [])
        if circuit is not None:
            self.show_circuit(circuit)
            if getattr(circuit, "num_parameters", 0):
                self.set_circuit_info(
                    circuit,
                    run_status=f'Auto-run uses default parameter=({AUTO_PARAM_DEFAULT_EXPR}); please use "Run manually (w/ params)" to enter custom values.',
                )
            else:
                self.set_circuit_info(circuit, run_status="Running simulation...")
                self.start_aer_run(circuit)
        else:
            self.circuit.show_placeholder()
            self.circuit_info.clear()

    def set_importer_output(self, rewritten: str | None, error: str = "", success: bool = True, issues: list[Issue] | None = None) -> None:
        self.output.clear()
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        
        if rewritten is None:
            self.output.setPlainText(error)
            self.output_title.setText(f"Qiskit importer (rewriting: ERROR)")
            return
        
        red_format = QTextCharFormat()
        red_format.setForeground(QColor("#cc0000"))
        
        include_format = QTextCharFormat()
        include_format.setForeground(QColor("#303030"))
        include_format.setBackground(QColor("#e0e0e0"))
        
        default_format = QTextCharFormat()
        
        if issues:
            cursor.insertText("Unsupported / rewritten constructs:\n", red_format)
            for issue in issues:
                text = f"- {issue.kind}: {issue.detail} at lines {issue.start}-{issue.end}\n"
                cursor.insertText(text, red_format)
            cursor.insertText("\n", default_format)
        
        # Get the set of standard gate lines for comparison
        stdgates_lines = set(line.strip() for line in stdgates_compat_lines())
        
        # Insert each line, using dark-gray format for expanded includes
        lines = rewritten.rstrip().split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            is_include_line = stripped in stdgates_lines
            fmt = include_format if is_include_line else default_format
            cursor.insertText(line + ("\n" if i < len(lines) - 1 else ""), fmt)
        
        status = "OK" if success else "ERROR"
        self.output_title.setText(f"Qiskit importer (rewriting: {status})")

    def _build_circuit_info_lines(self, circuit: Any, run_counts: Any | None = None, run_error: str | None = None, run_timestamp: datetime | None = None, run_status: str | None = None, run_duration: float | None = None) -> list[str]:
        lines = [
            f"Circuit summary: qubits={circuit.num_qubits} clbits={circuit.num_clbits} depth={circuit.depth()} {circuit.count_ops()}",
        ]
        if run_status:
            lines.append("")
            lines.append(run_status)
        if run_counts is not None:
            lines.append("")
            meta_parts: list[str] = []
            if run_timestamp is not None:
                meta_parts.append(f"start timestamp {format_utc_timestamp(run_timestamp)}")
            if run_duration is not None:
                meta_parts.append(f"total computation time {format_elapsed_time(run_duration)}")
            # Append total shots into the Simulation results parentheses
            total_shots_text = ""
            if isinstance(run_counts, dict):
                try:
                    total_shots = sum(int(v) for v in run_counts.values())
                    total_shots_text = f"Total shots: {total_shots}"
                except Exception:
                    total_shots_text = ""
            if total_shots_text:
                meta_parts.append(total_shots_text)
            meta_text = f" ({', '.join(meta_parts)})" if meta_parts else ""
            lines.append(f"Simulation results:{meta_text}")
            lines.extend(format_counts_readable(run_counts))
        if run_error:
            lines.append("")
            lines.append(f"Run failed: {run_error}")
        return lines

    def set_circuit_info(self, circuit: Any, run_counts: Any | None = None, run_error: str | None = None, run_timestamp: datetime | None = None, run_status: str | None = None, run_duration: float | None = None) -> None:
        self.circuit_info.setPlainText(
            "\n".join(
                self._build_circuit_info_lines(
                    circuit,
                    run_counts=run_counts,
                    run_error=run_error,
                    run_timestamp=run_timestamp,
                    run_status=run_status,
                    run_duration=run_duration,
                )
            )
        )

    def draw_circuit(self, circuit: Any) -> None:
        fig = cast(Any, circuit_drawer(circuit, output="mpl"))
        buf = BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=140)
        try:
            fig.clf()
        except Exception:
            pass
        self.circuit.set_image(buf.getvalue())

    def show_circuit(self, circuit: Any) -> None:
        try:
            self.draw_circuit(circuit)
        except Exception as exc:
            self.circuit.show_placeholder()
            self.circuit_info.setPlainText(f"Circuit draw failed: {exc}")

    def run_circuit_through_aer(self, circuit: Any) -> Any:
        """Run a circuit through Aer simulator and return the counts."""
        return run_circuit_counts(circuit, shots=self.shots)

    def prompt_parameter_values(self, circuit: Any) -> Any | None:
        if not getattr(circuit, "num_parameters", 0):
            return circuit

        bindings: dict[Any, Any] = {}
        for parameter in circuit.parameters:
            while True:
                value_text, ok = QInputDialog.getText(
                    self,
                    "Parameter value",
                    f"Enter a value for parameter ({parameter.name}):",
                    QLineEdit.EchoMode.Normal,
                    AUTO_PARAM_DEFAULT_EXPR,
                )
                if not ok:
                    return None
                value = eval_text(value_text.strip(), {})
                if value is None:
                    QMessageBox.warning(
                        self,
                        "Invalid parameter value",
                        f"Could not parse a value for {parameter.name}. Use a number or expression like pi/2.",
                    )
                    continue
                bindings[parameter] = value
                break

        return circuit.assign_parameters(bindings)

    def start_aer_run(self, circuit: Any) -> None:
        self._aer_run_token += 1
        token = self._aer_run_token
        self._aer_run_start_monotonic = time.perf_counter()
        self.start_aer_stopwatch()
        self._aer_stopwatch_timer.start()
        self._update_aer_stopwatch()
        if self._aer_executor is None:
            self._aer_executor = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        self._aer_future_token = token
        self._aer_future_circuit = circuit
        self._aer_future = self._aer_executor.submit(run_aer_job, circuit, self.shots)

    def on_aer_run_finished(self, token: int, circuit: Any, counts: Any, error: Any, run_timestamp: Any, run_duration: float | None = None) -> None:
        if token != self._aer_run_token:
            return
        if run_duration is None and self._aer_run_start_monotonic is not None:
            run_duration = time.perf_counter() - self._aer_run_start_monotonic
        self._aer_stopwatch_timer.stop()
        self._aer_run_start_monotonic = None
        self.stop_aer_stopwatch()
        self._aer_future = None
        self._aer_future_token = None
        self._aer_future_circuit = None
        if error:
            self.set_circuit_info(circuit, run_error=str(error), run_timestamp=run_timestamp, run_duration=run_duration)
            self.statusBar().showMessage("Simulation failed", 3000)
        else:
            self.set_circuit_info(circuit, run_counts=counts, run_timestamp=run_timestamp, run_duration=run_duration)
            self.statusBar().showMessage("Simulation complete", 3000)

    def _aer_timeout_action_text(self) -> str:
        if self._aer_timeout_seconds == 0:
            return "AER timeout (no limit)..."
        return f"AER timeout ({self._aer_timeout_seconds} sec)..."

    def set_aer_timeout_dialog(self) -> None:
        value, ok = QInputDialog.getInt(
            self,
            "Set AER timeout",
            "Maximum AER runtime in seconds (0 = no timeout):",
            self._aer_timeout_seconds,
            0,
            3_600,
            1,
        )
        if not ok:
            return
        self._aer_timeout_seconds = int(value)
        try:
            self.set_aer_timeout_action.setText(self._aer_timeout_action_text())
        except Exception:
            pass
        if self._aer_timeout_seconds == 0:
            self.statusBar().showMessage("AER timeout disabled", 3000)
        else:
            self.statusBar().showMessage(f"AER timeout set to {self._aer_timeout_seconds} seconds", 3000)

    def start_aer_stopwatch(self) -> None:
        self._aer_stopwatch_label.setVisible(True)
        self._aer_stopwatch_label.setText("Running simulation... 00:00:00.000")

    def stop_aer_stopwatch(self) -> None:
        self._aer_stopwatch_label.clear()
        self._aer_stopwatch_label.setVisible(False)
        # Reset color to green
        self._aer_stopwatch_label.setStyleSheet("font-weight: 700; color: #1f6f2a; padding-left: 8px; padding-right: 8px;")

    def _shutdown_aer_executor(self) -> None:
        self._aer_stopwatch_timer.stop()
        self._aer_run_start_monotonic = None
        self._aer_future = None
        self._aer_future_token = None
        self._aer_future_circuit = None
        executor = self._aer_executor
        self._aer_executor = None
        if executor is None:
            return
        try:
            processes = list(getattr(executor, "_processes", {}).values())
        except Exception:
            processes = []
        for process in processes:
            try:
                if process.is_alive():
                    process.terminate()
            except Exception:
                pass
        for process in processes:
            try:
                process.join(timeout=1)
            except Exception:
                pass
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _refresh_aer_run_state(self) -> None:
        if self._aer_run_start_monotonic is None:
            return
        self._update_aer_stopwatch()
        future = self._aer_future
        if future is None:
            return
        timeout_seconds = self._aer_timeout_seconds
        if timeout_seconds > 0 and self._aer_run_start_monotonic is not None:
            elapsed = time.perf_counter() - self._aer_run_start_monotonic
            if elapsed >= timeout_seconds and not future.done():
                token = self._aer_future_token
                circuit = self._aer_future_circuit
                run_timestamp = datetime.now(timezone.utc)
                self._shutdown_aer_executor()
                if token is not None and circuit is not None:
                    self.on_aer_run_finished(
                        token,
                        circuit,
                        None,
                        f"AER run timed out after {timeout_seconds} seconds",
                        run_timestamp,
                        run_duration=elapsed,
                    )
                return
        if not future.done():
            return
        token = self._aer_future_token
        if token is None:
            return
        try:
            counts, error, run_timestamp = future.result()
        except Exception as exc:
            counts = None
            error = str(exc)
            run_timestamp = datetime.now()
        circuit = self._aer_future_circuit
        self._aer_future = None
        self._aer_future_token = None
        self._aer_future_circuit = None
        if circuit is not None:
            self.on_aer_run_finished(token, circuit, counts, error, run_timestamp)

    def _update_aer_stopwatch(self) -> None:
        if self._aer_run_start_monotonic is None:
            return
        elapsed = time.perf_counter() - self._aer_run_start_monotonic
        self._aer_stopwatch_label.setText(f"Running simulation... {format_elapsed_time(elapsed)}")
        # Turn red if execution exceeds 10 seconds
        if elapsed >= 10.0:
            self._aer_stopwatch_label.setStyleSheet("font-weight: 700; color: #c41e3a; padding-left: 8px; padding-right: 8px;")
        else:
            self._aer_stopwatch_label.setStyleSheet("font-weight: 700; color: #1f6f2a; padding-left: 8px; padding-right: 8px;")

    def closeEvent(self, event: Any) -> None:
        self._shutdown_aer_executor()
        super().closeEvent(event)

    def rewrite_current(self) -> None:
        source = self.editor.toPlainText()
        try:
            rewritten, _, _ = transpile_qasm(source)
        except Exception as exc:
            QMessageBox.critical(self, "Rewrite failed", str(exc))
            return

        if rewritten == source:
            self.statusBar().showMessage("No rewrite changes to apply", 3000)
            return

        answer = QMessageBox.question(
            self,
            "Analyze rewritten as if original",
            "This will replace the left 'QASM original' editor content with the rewritten importer-compatible text. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self._syncing = True
        try:
            self.editor.setPlainText(rewritten)
        finally:
            self._syncing = False
        self.refresh_views()
        self.statusBar().showMessage("Applied rewrite to source editor", 3000)

    def run_current(self) -> None:
        source = self.editor.toPlainText()
        circuit = None
        try:
            rewritten, _, _ = transpile_qasm(source)
            circuit = qiskit_parse(rewritten)
            circuit = self.prompt_parameter_values(circuit)
            if circuit is None:
                self.statusBar().showMessage("Run cancelled", 3000)
                return
            self.show_circuit(circuit)
            self.set_circuit_info(circuit, run_status="Running simulation...")
            self.start_aer_run(circuit)
        except Exception as exc:
            if circuit is not None:
                self.set_circuit_info(circuit, run_error=str(exc))
            else:
                self.circuit_info.setPlainText(f"Run failed: {exc}")

    def set_shots_dialog(self) -> None:
        """Prompt the user to configure the number of shots used for Qiskit/Aer runs."""
        value, ok = QInputDialog.getInt(
            self,
            "Set shots",
            "Number of shots:",
            self.shots,
            1,
            10_000_000,
            1,
        )
        if not ok:
            return
        self.shots = int(value)
        # Update menu label to reflect new shots value
        try:
            self.set_shots_action.setText(f"Qiskit shots ({self.shots})...")
        except Exception:
            pass
        self.statusBar().showMessage(f"Shots set to {self.shots}", 3000)
    def show_diagnostics(self) -> None:
        antlr4_version = "not installed"
        try:
            import importlib.metadata as importlib_metadata  # py3.8+

            antlr4_version = importlib_metadata.version("antlr4-python3-runtime")
        except Exception as exc:
            if exc.__class__.__name__ != "PackageNotFoundError":
                antlr4_version = "unknown"

        lines = [
            f"Python: {sys.version.split()[0]}",
            f"Python executable: {sys.executable}",
            f"PySide6: {PySide6.__version__}",
            f"openqasm3: {getattr(sys.modules.get('openqasm3'), '__version__', 'unknown')}",
            f"antlr4-python3-runtime: {antlr4_version}",
            "",
            "Qiskit runtime:",
            f"  qiskit: {getattr(sys.modules.get('qiskit'), '__version__', 'unknown')}",
            f"  qiskit-aer: {getattr(sys.modules.get('qiskit_aer'), '__version__', 'unknown')}",
            f"  qiskit-qasm3-import: {getattr(qiskit_qasm3_import, '__version__', 'unknown')}",
        ]

        qiskit_smoke = "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[1] q;\nbit[1] c;\nh q[0];\nc[0] = measure q[0];\n"
        try:
            t0 = time.perf_counter()
            circuit = qiskit_parse(qiskit_smoke)
            t_parse = (time.perf_counter() - t0) * 1000.0

            backend = AerSimulator()
            t1 = time.perf_counter()
            compiled = transpile(circuit, backend)
            t_transpile = (time.perf_counter() - t1) * 1000.0

            t2 = time.perf_counter()
            result = backend.run(compiled, shots=self.shots).result()
            t_run = (time.perf_counter() - t2) * 1000.0

            lines.append(f"  Aer backend: {backend.name}")
            lines.append(f"  qasm3 parse smoke: ok ({t_parse:.1f} ms)")
            lines.append(f"  transpile smoke: ok ({t_transpile:.1f} ms)")
            lines.append(f"  run smoke (Hadamard gate, {self.shots} shots): ok ({t_run:.1f} ms)")
            lines.append(f"  counts sample (Hadamard gate): {result.get_counts()}")
        except Exception as exc:
            lines.append(f"  runtime smoke: failed ({exc})")

        RulesDialog("\n".join(lines), self, title="Diagnostics").exec()

    def show_rules(self) -> None:
        RulesDialog(self.load_rewrite_rules_text(), self).exec()

    def load_rewrite_rules_text(self) -> str:
        if not REWRITE_RULES_FILE.exists():
            return (
                "Rewrite rules file not found.\n\n"
                f"Expected file: {REWRITE_RULES_FILE}\n"
                "Create this file to keep help text aligned with transpiler behavior."
            )

        text = REWRITE_RULES_FILE.read_text()
        lower_text = text.lower()
        missing = [marker for marker in REWRITE_RULES_SYNC_MARKERS if marker.lower() not in lower_text]
        if not missing:
            return text

        warning = [
            "",
            "SYNC WARNING:",
            "The rewrite_rules.txt file appears out of sync with transpiler behavior.",
            "Missing required rule markers:",
        ]
        warning.extend(f"- {marker}" for marker in missing)
        return text + "\n" + "\n".join(warning) + "\n"

    def tree_node_at_cursor(self) -> Any | None:
        if not self.current_program:
            return None
        pos = self.editor.textCursor().position()
        block = self.editor.document().findBlock(pos)
        line = block.blockNumber()
        col = pos - block.position()
        best = None
        best_size = None

        def walk(node: Any) -> None:
            nonlocal best, best_size
            s = span(node)
            # Spans use 1-indexed line numbers, convert to 0-indexed for comparison
            if s and (s.start_line - 1 < line or (s.start_line - 1 == line and s.start_column <= col)) and (s.end_line - 1 > line or (s.end_line - 1 == line and s.end_column > col)):
                size = (s.end_line - s.start_line) * 1000 + (s.end_column - s.start_column)
                if best is None or size < best_size:
                    best = node
                    best_size = size
            for child in node_iter(node):
                walk(child)

        walk(self.current_program)
        return best

    def sync_tree_from_cursor(self) -> None:
        if self._syncing or not self.current_program:
            return
        node = self.tree_node_at_cursor()
        if node is None:
            return
        self._syncing = True
        try:
            self.select_tree_node(node)
        finally:
            self._syncing = False

    def select_tree_node(self, target: Any) -> None:
        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(0, Qt.ItemDataRole.UserRole) is target:
                return item
            for i in range(item.childCount()):
                found = walk(item.child(i))
                if found is not None:
                    return found
            return None

        root = self.tree.topLevelItem(0)
        if root is None:
            return
        found = walk(root)
        if found is not None:
            self.tree.setCurrentItem(found)
            self.tree.scrollToItem(found)

    def sync_editor_from_tree(self) -> None:
        if self._syncing:
            return
        items = self.tree.selectedItems()
        if not items:
            return
        node = items[0].data(0, Qt.ItemDataRole.UserRole)
        s = span(node)
        if not s:
            return
        self._syncing = True
        try:
            cursor = self.editor.textCursor()
            # Spans use 1-indexed line numbers, convert to 0-indexed for to_pos
            cursor.setPosition(clamp_cursor_pos(self.editor, to_pos(self.editor, s.start_line - 1, s.start_column)))
            cursor.setPosition(clamp_cursor_pos(self.editor, to_pos(self.editor, s.end_line - 1, s.end_column + 1)), QTextCursor.MoveMode.KeepAnchor)
            self.editor.setTextCursor(cursor)
        finally:
            self._syncing = False


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("QASM3 Aer Lab")
    app.setOrganizationName("Copilot")
    window = MainWindow()
    screen = app.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        window.resize(int(geo.width() * 0.9), int(geo.height() * 0.9))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
