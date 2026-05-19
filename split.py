#!/usr/bin/env python
"""QASM3 Splitter - Split OpenQASM 3 files at statement boundaries.

Allows users to load a QASM file, mark split points via right-click,
preview chunks in tabs, save them to disk, and launch independent run.py instances.
"""

from __future__ import annotations

import sys
import subprocess
import html
import json
import shutil
import math
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, cast

import PySide6
from PySide6.QtCore import Qt, QTimer, QSize, QRect, QEvent, QPointF
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QAction, QKeySequence, QPen, QBrush, QTextCharFormat, QTextCursor, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QSplitter,
    QVBoxLayout,
    QPlainTextEdit,
    QTextEdit,
    QLabel,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QStackedWidget,
    QToolBar,
    QMenu,
    QSizePolicy,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsRectItem,
    QGraphicsLineItem,
    QGraphicsPolygonItem,
)

from openqasm3 import parse
from qasm_rewriter import kind, span, node_iter, stdgates_compat_lines
try:
    # Prefer the lightweight minimal transpiler used by run.py (keeps stdgates include)
    from run import minimal_transpile as transpile_for_split
except Exception:
    # Fall back to the full transpiler if run.py cannot be imported
    from qasm_rewriter import transpile_qasm as transpile_for_split
from dqc_container import (
    DqcDocument,
    display_split_lines_to_raw_split_after_lines,
    is_dqc_pragma_line,
    parse_dqc_text,
    prepare_chunk_text_for_run,
    render_dqc_text,
)
from qiskit.converters import circuit_to_dag
from qiskit_qasm3_import import parse as qiskit_parse


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"

# Map for anonymous wires to produce stable labels like 'bit0', 'bit1', ...
_anon_wire_map: dict[str, str] = {}
_anon_wire_counters: dict[str, int] = {}


STDGATES_LINE_SET = {line.strip() for line in stdgates_compat_lines()}
INNER_SCOPE_BLOCKING_KINDS = {
    "QuantumGateDefinition",
    "ForInLoop",
    "WhileLoop",
    "BranchingStatement",
    "Box",
    "SubroutineDefinition",
    "CalibrationDefinition",
    "CalibrationGrammarDeclaration",
}


def clear_directory_contents(path: Path) -> None:
    """Remove all existing contents from a directory without deleting it."""
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    if path.is_file() or path.is_symlink():
        path.unlink()
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def apply_gray_include_format(widget: QPlainTextEdit, text: str) -> None:
    """Populate a plain-text widget and gray out stdgates include lines.

    The rewritten output produced for the splitter should keep an `include
    "stdgates.inc";` line instead of inlining the full stdgates definitions.
    Gray that include line (or, as a fallback, any inlined stdgates definition
    lines) so users see the compatibility include highlighted.
    """
    widget.setPlainText(text)

    include_format = QTextCharFormat()
    include_format.setForeground(QColor("#303030"))
    include_format.setBackground(QColor("#e0e0e0"))

    document = widget.document()
    for block_number in range(document.blockCount()):
        block = document.findBlockByNumber(block_number)
        if not block.isValid():
            continue
        txt = block.text().strip()
        # Gray an explicit include line for stdgates
        if txt.lower() == 'include "stdgates.inc";':
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(include_format)
            continue
        # Fallback: gray any inlined stdgates definition lines
        if txt in STDGATES_LINE_SET:
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(include_format)


def apply_gray_line_numbers(widget: QPlainTextEdit, line_numbers: set[int]) -> None:
    """Gray out exact 1-indexed lines in a plain-text widget."""
    if not line_numbers:
        return

    line_format = QTextCharFormat()
    line_format.setForeground(QColor("#303030"))
    line_format.setBackground(QColor("#e0e0e0"))

    document = widget.document()
    for line_number in sorted(line_numbers):
        block = document.findBlockByNumber(line_number - 1)
        if not block.isValid():
            continue
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(line_format)


def line_is_inside_blocking_scope(program: Any, line: int) -> bool:
    """Return True when a line falls inside an inner scope that should not split."""
    for node in node_iter(program):
        if kind(node) not in INNER_SCOPE_BLOCKING_KINDS:
            continue
        s = span(node)
        if not s:
            continue
        start = int(getattr(s, "start_line", 0))
        end = int(getattr(s, "end_line", 0))
        if start <= line < end:
            return True
    return False


@dataclass(slots=True)
class SplitPoint:
    line: int  # 1-indexed line number where to split AFTER


@dataclass(slots=True)
class ChunkFlow:
    title: str
    original_text: str
    rewritten_text: str
    defined: set[str]
    used: set[str]
    incoming_sources: dict[str, set[int]]
    outgoing_targets: dict[str, set[int]]


def wire_label(wire: Any) -> str:
    # Try common patterns used by Qiskit wire objects, but be robust when
    # attributes are missing or None. Prefer compact varnames like `c0`.
    register = getattr(wire, "_register", None) or getattr(wire, "register", None)
    register_name = None
    if register is not None:
        register_name = getattr(register, "name", None)
        if register_name is None and isinstance(register, (tuple, list)) and len(register) > 1:
            # Qiskit sometimes exposes register as a tuple like (size, name)
            register_name = register[1]

    # Get index from common attribute names
    index = getattr(wire, "_index", None)
    if index is None:
        index = getattr(wire, "index", None)

    # If we have both register name and index, emit compact form like 'c0'
    if register_name is not None and index is not None:
        return f"{register_name}{index}"

    # If only register name is available, return it
    if register_name is not None:
        return str(register_name)

    # If the wire has a direct name attribute, use it
    name = getattr(wire, "name", None)
    if name:
        return str(name)

    # If we at least have an index, use a classname+index fallback (e.g., Clbit0)
    if index is not None:
        return f"{wire.__class__.__name__}{index}"

    # Final fallback: try to parse useful info from the repr string and
    # otherwise assign a stable anonymous label like 'bit0', 'bit1'.
    rep = repr(wire)
    import re

    # Try to capture index-like fields in common reprs: 'index=0' or 'uid=0'
    m = re.search(r"(?:index|uid)\s*=\s*(\d+)", rep)
    idx = int(m.group(1)) if m else None
    # Try to capture a register name inside quotes: (3, 'c') or (3, "c")
    mreg = re.search(r"\(\s*\d+\s*,\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\)", rep)
    reg = mreg.group(1) if mreg else None

    if reg is not None and idx is not None:
        return f"{reg}{idx}"

    # Use stable anonymous names when no var/register info is available.
    typename = wire.__class__.__name__.lower()
    # Map common classnames to the simpler 'bit' or 'qubit' labels
    if 'clbit' in typename or 'cbit' in typename or 'classical' in typename:
        anon_type = 'bit'
    elif 'qubit' in typename or 'qbit' in typename or 'quantum' in typename:
        anon_type = 'qubit'
    else:
        anon_type = typename

    if rep in _anon_wire_map:
        return _anon_wire_map[rep]

    counter = _anon_wire_counters.get(anon_type, 0)
    label = f"{anon_type}{counter}"
    _anon_wire_counters[anon_type] = counter + 1
    _anon_wire_map[rep] = label
    return label


def _identifier_name(node: Any) -> str:
    kind_name = kind(node)
    if kind_name == "Identifier":
        return getattr(node, "name", "") or ""
    if kind_name == "IndexedIdentifier":
        base = getattr(node, "name", None)
        return getattr(base, "name", "") or ""
    return ""


def _node_identifier_names(node: Any) -> set[str]:
    names: set[str] = set()
    if node is None:
        return names
    for nested in node_iter(node):
        name = _identifier_name(nested)
        if name:
            names.add(name)
    return names


def _operand_identifier_names(node: Any) -> set[str]:
    name = _identifier_name(node)
    if name:
        return {name}
    return _node_identifier_names(node)


def _as_iterable_nodes(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _stmt_defined_names(stmt: Any) -> set[str]:
    kind_name = kind(stmt)
    if kind_name == "QubitDeclaration":
        name_obj = getattr(stmt, "identifier", None) or getattr(stmt, "qubit", None)
        name = getattr(name_obj, "name", "") or ""
        return {name} if name else set()
    if kind_name in {"ClassicalDeclaration", "IODeclaration"}:
        name = getattr(getattr(stmt, "identifier", None), "name", "") or ""
        return {name} if name else set()
    if kind_name == "AliasStatement":
        name = getattr(getattr(stmt, "target", None), "name", "") or ""
        return {name} if name else set()
    if kind_name == "QuantumMeasurementStatement":
        target = getattr(stmt, "target", None)
        name = _identifier_name(target)
        return {name} if name else set()
    if kind_name == "ClassicalAssignment":
        name = _identifier_name(getattr(stmt, "lvalue", None))
        return {name} if name else set()
    if kind_name in {"QuantumGateDefinition", "SubroutineDefinition", "CalibrationDefinition"}:
        name = getattr(getattr(stmt, "name", None), "name", "") or getattr(getattr(stmt, "identifier", None), "name", "") or ""
        return {name} if name else set()
    return set()


def _stmt_read_write_names(stmt: Any) -> tuple[set[str], set[str]]:
    kind_name = kind(stmt)
    if kind_name in {"QuantumGateDefinition", "SubroutineDefinition", "CalibrationDefinition"}:
        return set(), set()

    if kind_name == "ClassicalDeclaration":
        name = getattr(getattr(stmt, "identifier", None), "name", "") or ""
        reads = _operand_identifier_names(getattr(stmt, "init_expression", None))
        writes = {name} if name else set()
        return reads, writes

    if kind_name == "IODeclaration":
        name = getattr(getattr(stmt, "identifier", None), "name", "") or ""
        return set(), ({name} if name else set())

    if kind_name == "AliasStatement":
        target = _identifier_name(getattr(stmt, "target", None))
        reads = _operand_identifier_names(getattr(stmt, "value", None))
        writes = {target} if target else set()
        return reads, writes

    if kind_name == "ClassicalAssignment":
        reads = _operand_identifier_names(getattr(stmt, "rvalue", None))
        writes = _operand_identifier_names(getattr(stmt, "lvalue", None))
        return reads - writes, writes

    if kind_name == "QuantumGate":
        qubit_names: set[str] = set()
        for qb in _as_iterable_nodes(getattr(stmt, "qubits", None)):
            qubit_names |= _operand_identifier_names(qb)
        reads = qubit_names.copy()
        for arg in _as_iterable_nodes(getattr(stmt, "arguments", None)):
            reads |= _operand_identifier_names(arg)
        return reads, qubit_names

    if kind_name == "QuantumMeasurementStatement":
        measure = getattr(stmt, "measure", None)
        qubit_names = _operand_identifier_names(getattr(measure, "qubit", None))
        target_names = _operand_identifier_names(getattr(stmt, "target", None))
        reads = qubit_names.copy()
        writes = qubit_names | target_names
        return reads, writes

    if kind_name == "QuantumReset":
        qubit_names: set[str] = set()
        for qb in _as_iterable_nodes(getattr(stmt, "qubits", None)):
            qubit_names |= _operand_identifier_names(qb)
        return qubit_names.copy(), qubit_names

    if kind_name == "BranchingStatement":
        return _operand_identifier_names(getattr(stmt, "condition", None)), set()

    if kind_name == "WhileLoop":
        return _operand_identifier_names(getattr(stmt, "while_condition", None)), set()

    if kind_name == "ForInLoop":
        reads = _operand_identifier_names(getattr(stmt, "set_declaration", None))
        writes = _operand_identifier_names(getattr(stmt, "identifier", None))
        return reads - writes, writes

    if kind_name == "Box":
        return set(), set()

    reads: set[str] = set()
    for node in node_iter(stmt):
        name = _identifier_name(node)
        if name:
            reads.add(name)
    writes = _stmt_defined_names(stmt)
    return reads - writes, writes


def analyze_chunk_flow(chunk_text: str, source_text: str) -> tuple[set[str], set[str]]:
    """Return (written_names, read_names) for a single chunk."""
    prepared = prepare_chunk_text_for_run(chunk_text, source_text)
    try:
        program = parse(prepared)
    except Exception:
        return set(), set()

    defined: set[str] = set()
    used: set[str] = set()
    for stmt in getattr(program, "statements", []):
        stmt_used, stmt_defined = _stmt_read_write_names(stmt)
        defined |= stmt_defined
        used |= stmt_used
    return defined, used


def _scan_statement_dependencies(
    stmt: Any,
    chunk_index: int,
    current_writers: dict[str, int],
    incoming_sources: dict[str, set[int]],
    outgoing_targets: dict[int, dict[str, set[int]]],
    defined: set[str],
    used: set[str],
) -> None:
    kind_name = kind(stmt)
    if kind_name in {"QuantumGateDefinition", "SubroutineDefinition", "CalibrationDefinition"}:
        return

    if kind_name == "BranchingStatement":
        condition_used = _node_identifier_names(getattr(stmt, "condition", None))
        used.update(condition_used)
        for name in sorted(condition_used):
            writer = current_writers.get(name)
            if writer is not None and writer != chunk_index:
                incoming_sources.setdefault(name, set()).add(writer)
                outgoing_targets.setdefault(writer, {}).setdefault(name, set()).add(chunk_index)
        for inner in getattr(stmt, "if_block", []) or []:
            _scan_statement_dependencies(inner, chunk_index, current_writers, incoming_sources, outgoing_targets, defined, used)
        for inner in getattr(stmt, "else_block", []) or []:
            _scan_statement_dependencies(inner, chunk_index, current_writers, incoming_sources, outgoing_targets, defined, used)
        return

    if kind_name == "WhileLoop":
        condition_used = _node_identifier_names(getattr(stmt, "while_condition", None))
        used.update(condition_used)
        for name in sorted(condition_used):
            writer = current_writers.get(name)
            if writer is not None and writer != chunk_index:
                incoming_sources.setdefault(name, set()).add(writer)
                outgoing_targets.setdefault(writer, {}).setdefault(name, set()).add(chunk_index)
        for inner in getattr(stmt, "block", []) or []:
            _scan_statement_dependencies(inner, chunk_index, current_writers, incoming_sources, outgoing_targets, defined, used)
        return

    if kind_name == "Box":
        for inner in getattr(stmt, "body", []) or []:
            _scan_statement_dependencies(inner, chunk_index, current_writers, incoming_sources, outgoing_targets, defined, used)
        return

    stmt_used, stmt_defined = _stmt_read_write_names(stmt)
    used.update(stmt_used)
    defined.update(stmt_defined)

    for name in sorted(stmt_used):
        writer = current_writers.get(name)
        if writer is not None and writer != chunk_index:
            incoming_sources.setdefault(name, set()).add(writer)
            outgoing_targets.setdefault(writer, {}).setdefault(name, set()).add(chunk_index)

    for name in sorted(stmt_defined):
        current_writers[name] = chunk_index


def compute_chunk_flows(chunk_texts: list[str], source_text: str) -> list[ChunkFlow]:
    """Analyze symbol flow between chunks."""
    current_writers: dict[str, int] = {}
    flows: list[ChunkFlow] = []
    outgoing_by_source: dict[int, dict[str, set[int]]] = {}

    for index, chunk_text in enumerate(chunk_texts, 1):
        defined: set[str] = set()
        used: set[str] = set()
        incoming_sources: dict[str, set[int]] = {}
        prepared = prepare_chunk_text_for_run(chunk_text, source_text)
        try:
            program = parse(prepared)
        except Exception:
            program = None

        if program is not None:
            for stmt in getattr(program, "statements", []):
                _scan_statement_dependencies(stmt, index, current_writers, incoming_sources, outgoing_by_source, defined, used)

        outgoing_targets = outgoing_by_source.get(index, {})

        flows.append(
            ChunkFlow(
                title=f"Chunk {index}",
                original_text=chunk_text,
                rewritten_text="",
                defined=defined,
                used=used,
                incoming_sources=incoming_sources,
                outgoing_targets={},
            )
        )

    for index, flow in enumerate(flows, 1):
        flow.outgoing_targets = outgoing_by_source.get(index, {})

    return flows


def format_flow_lines(mapping: dict[str, set[int]], arrow: str) -> str:
    if not mapping:
        return "none"
    parts: list[str] = []
    for name in sorted(mapping):
        chunks = ", ".join(f"Chunk {index}" for index in sorted(mapping[name]))
        parts.append(f"{name} {arrow} {chunks}")
    return "\n".join(parts)


def _bold_qubit_name(name: str, qubit_names: set[str]) -> str:
    escaped = html.escape(name)
    if name in qubit_names:
        return f"<b>{escaped}</b>"
    return escaped


def format_flow_lines_html(mapping: dict[str, set[int]], arrow: str, qubit_names: set[str]) -> str:
    if not mapping:
        return "none"
    parts: list[str] = []
    escaped_arrow = html.escape(arrow)
    for name in sorted(mapping):
        chunks = ", ".join(html.escape(f"Chunk {index}") for index in sorted(mapping[name]))
        parts.append(f"{_bold_qubit_name(name, qubit_names)} {escaped_arrow} {chunks}")
    return "\n".join(parts)


def collect_qubit_register_names(circuit: Any) -> set[str]:
    names: set[str] = set()
    for qubit in getattr(circuit, "qubits", []) or []:
        register = getattr(qubit, "_register", None)
        name = getattr(register, "name", "") or ""
        if name:
            names.add(name)
    return names


class ChunkDagView(QGraphicsView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setBackgroundBrush(QColor("#f8fbf7"))
        self.setStyleSheet("border: 1px solid #d0d0d0;")

    def set_flows(self, flows: list[ChunkFlow], font: QFont) -> None:
        scene = self.scene()
        if scene is None:
            scene = QGraphicsScene(self)
            self.setScene(scene)
        scene.clear()

        node_font = QFont(font)
        if node_font.pointSizeF() > 0:
            node_font.setPointSizeF(max(7.0, node_font.pointSizeF() - 2.0))
        elif node_font.pointSize() > 0:
            node_font.setPointSize(max(7, node_font.pointSize() - 2))
        else:
            node_font.setPointSize(8)

        if not flows:
            empty = scene.addSimpleText("No dependency DAG available")
            empty.setFont(node_font)
            empty.setBrush(QBrush(QColor("#555555")))
            scene.setSceneRect(empty.boundingRect().adjusted(-10, -10, 10, 10))
            return

        node_w = 320.0
        node_h = 72.0
        gap = 60.0
        left = 20.0
        top = 20.0

        node_items: dict[int, tuple[QGraphicsRectItem, float, float]] = {}
        for index, flow in enumerate(flows, 1):
            y = top + (index - 1) * (node_h + gap)
            rect = scene.addRect(left, y, node_w, node_h, QPen(QColor("#6d8f6a")), QBrush(QColor("#eef6ec")))
            title = scene.addSimpleText(flow.title)
            title.setFont(node_font)
            title.setBrush(QBrush(QColor("#223322")))
            title.setPos(left + 10, y + 6)

            incoming = scene.addSimpleText(f"in: {len(flow.incoming_sources)}")
            incoming.setFont(node_font)
            incoming.setBrush(QBrush(QColor("#345")))
            incoming.setPos(left + 10, y + 30)

            outgoing = scene.addSimpleText(f"out: {len(flow.outgoing_targets)}")
            outgoing.setFont(node_font)
            outgoing.setBrush(QBrush(QColor("#345")))
            outgoing.setPos(left + 110, y + 30)

            summary = scene.addSimpleText("declared/used: " + (", ".join(sorted(flow.defined)) if flow.defined else "none"))
            summary.setFont(node_font)
            summary.setBrush(QBrush(QColor("#556")))
            summary.setPos(left + 10, y + 50)

            node_items[index] = (rect, left, y)

        edge_labels: dict[tuple[int, int], list[str]] = {}
        for index, flow in enumerate(flows, 1):
            for name, sources in flow.incoming_sources.items():
                for source in sources:
                    edge_labels.setdefault((source, index), []).append(name)

        edge_color = QColor("#2f6fff")
        edge_pen = QPen(edge_color)
        edge_pen.setWidthF(1.4)
        for (source, dest), labels in sorted(edge_labels.items()):
            _, x1, y1 = node_items[source]
            _, x2, y2 = node_items[dest]
            start_x = x1 + node_w
            start_y = y1 + node_h / 2
            end_x = x2
            end_y = y2 + node_h / 2
            angle = math.atan2(end_y - start_y, end_x - start_x)
            arrow_size = 9.0
            line_end_x = end_x - math.cos(angle) * arrow_size
            line_end_y = end_y - math.sin(angle) * arrow_size

            line = QGraphicsLineItem(start_x, start_y, line_end_x, line_end_y)
            line.setPen(edge_pen)
            scene.addItem(line)

            arrow_head = QPolygonF([
                QPointF(end_x, end_y),
                QPointF(
                    line_end_x - math.cos(angle - math.pi / 6) * arrow_size,
                    line_end_y - math.sin(angle - math.pi / 6) * arrow_size,
                ),
                QPointF(
                    line_end_x - math.cos(angle + math.pi / 6) * arrow_size,
                    line_end_y - math.sin(angle + math.pi / 6) * arrow_size,
                ),
            ])
            arrow = QGraphicsPolygonItem(arrow_head)
            arrow.setPen(QPen(edge_color))
            arrow.setBrush(QBrush(edge_color))
            scene.addItem(arrow)

            label = scene.addSimpleText(", ".join(sorted(labels)))
            label_font = QFont(font)
            label_font.setBold(True)
            if label_font.pointSizeF() > 0:
                label_font.setPointSizeF(label_font.pointSizeF() + 3.0)
            elif label_font.pointSize() > 0:
                label_font.setPointSize(label_font.pointSize() + 3)
            else:
                label_font.setPointSize(13)
            label.setFont(label_font)
            label.setBrush(QBrush(edge_color))
            label_rect = label.boundingRect().adjusted(-6, -3, 6, 3)
            midpoint_x = (start_x + end_x) / 2
            midpoint_y = (start_y + end_y) / 2
            label_x = midpoint_x - label_rect.width() / 2
            label_y = midpoint_y - label_rect.height() / 2
            label.setPos(label_x, label_y)

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))


class QiskitDagView(QGraphicsView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setBackgroundBrush(QColor("#f7faff"))
        self.setStyleSheet("border: 1px solid #d0d0d0;")
        self._dragging = False
        self._drag_start_pos: tuple[int, int] | None = None
        self._scroll_bar_start: tuple[int, int] | None = None
        self._user_interacted = False
        self._last_user_interaction: float = 0.0

    def set_message(self, message: str, font: QFont) -> None:
        scene = self.scene()
        if scene is None:
            scene = QGraphicsScene(self)
            self.setScene(scene)
        scene.clear()
        text = scene.addSimpleText(message)
        text.setFont(font)
        text.setBrush(QBrush(QColor("#555555")))
        scene.setSceneRect(text.boundingRect().adjusted(-12, -12, 12, 12))
        if not self._user_interacted:
            self.resetTransform()
            self.fitInView(text.boundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self.centerOn(text.boundingRect().center())

    def set_circuit(self, circuit: Any, font: QFont) -> None:
        scene = self.scene()
        if scene is None:
            scene = QGraphicsScene(self)
            self.setScene(scene)
        scene.clear()

        try:
            dag = circuit_to_dag(circuit)
        except Exception as exc:
            self.set_message(f"Qiskit DAG unavailable: {exc}", font)
            return

        qubits = list(getattr(dag, "qubits", []) or [])
        clbits = list(getattr(dag, "clbits", []) or [])
        wires = qubits + clbits
        op_nodes = list(dag.topological_op_nodes())
        if not wires:
            self.set_message("No wires available for DAG rendering", font)
            return
        if not op_nodes:
            self.set_message("No operation nodes available for DAG rendering", font)
            return

        node_font = QFont(font)
        if node_font.pointSizeF() > 0:
            node_font.setPointSizeF(max(7.0, node_font.pointSizeF() - 2.0))
        elif node_font.pointSize() > 0:
            node_font.setPointSize(max(7, node_font.pointSize() - 2))
        else:
            node_font.setPointSize(8)

        left = 96.0
        top = 34.0
        wire_gap = 42.0
        layer_gap = 140.0
        node_h = 28.0

        metrics = QFontMetricsF(node_font)
        node_widths: list[float] = [max(44.0, metrics.horizontalAdvance(getattr(node, "name", "op")) + 18.0) for node in op_nodes]

        wire_y: dict[Any, float] = {wire: top + index * wire_gap for index, wire in enumerate(wires)}
        max_node_w = max(node_widths, default=44.0)
        scene_right = left + max(1, len(op_nodes) - 1) * layer_gap + max_node_w + 40.0
        edge_pen = QPen(QColor("#2f6fff"))
        edge_pen.setWidthF(1.4)

        for wire, y in wire_y.items():
            label = scene.addSimpleText(wire_label(wire))
            label.setFont(node_font)
            label.setBrush(QBrush(QColor("#5b6d8a")))
            label.setPos(10, y - label.boundingRect().height() / 2)
            scene.addLine(left - 8, y, scene_right, y, QPen(QColor("#d7deea")))

        last_x: dict[Any, float] = {wire: left - 8 for wire in wires}
        for index, node in enumerate(op_nodes):
            node_w = node_widths[index]
            node_wires = list(getattr(node, "qargs", []) or []) + list(getattr(node, "cargs", []) or [])
            if not node_wires:
                continue

            x_center = left + index * layer_gap
            y_center = sum(wire_y[wire] for wire in node_wires) / len(node_wires)
            if len(node_wires) > 1:
                y_center += (index % 2) * 6.0

            node_rect = scene.addRect(
                x_center - node_w / 2,
                y_center - node_h / 2,
                node_w,
                node_h,
                QPen(QColor("#4a74b6")),
                QBrush(QColor("#e8f0ff")),
            )
            node_rect.setZValue(2)

            label = scene.addSimpleText(getattr(node, "name", "op"))
            label.setFont(node_font)
            label.setBrush(QBrush(QColor("#20304e")))
            label_rect = label.boundingRect()
            label.setPos(
                x_center - label_rect.width() / 2,
                y_center - label_rect.height() / 2,
            )
            label.setZValue(3)

            for wire in node_wires:
                start_x = last_x[wire]
                start_y = wire_y[wire]
                end_x = x_center - node_w / 2
                end_y = y_center
                angle = math.atan2(end_y - start_y, end_x - start_x)
                arrow_size = 8.0
                line_end_x = end_x - math.cos(angle) * arrow_size
                line_end_y = end_y - math.sin(angle) * arrow_size

                line = QGraphicsLineItem(start_x, start_y, line_end_x, line_end_y)
                line.setPen(edge_pen)
                scene.addItem(line)

                arrow_head = QPolygonF([
                    QPointF(end_x, end_y),
                    QPointF(
                        line_end_x - math.cos(angle - math.pi / 6) * arrow_size,
                        line_end_y - math.sin(angle - math.pi / 6) * arrow_size,
                    ),
                    QPointF(
                        line_end_x - math.cos(angle + math.pi / 6) * arrow_size,
                        line_end_y - math.sin(angle + math.pi / 6) * arrow_size,
                    ),
                ])
                arrow = QGraphicsPolygonItem(arrow_head)
                arrow.setPen(QPen(QColor("#2f6fff")))
                arrow.setBrush(QBrush(QColor("#2f6fff")))
                scene.addItem(arrow)

                last_x[wire] = x_center + node_w / 2

        scene.setSceneRect(scene.itemsBoundingRect().adjusted(-20, -20, 20, 20))
        self._user_interacted = False
        self._last_user_interaction = 0.0
        self.resetTransform()
        self.fitInView(scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.centerOn(scene.sceneRect().center())

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._user_interacted = True
            self._last_user_interaction = time.monotonic()
            pos = event.position().toPoint()
            self._drag_start_pos = (pos.x(), pos.y())
            self._scroll_bar_start = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._dragging and self._drag_start_pos and self._scroll_bar_start:
            pos = event.position().toPoint()
            dx = pos.x() - self._drag_start_pos[0]
            dy = pos.y() - self._drag_start_pos[1]
            self._user_interacted = True
            self._last_user_interaction = time.monotonic()
            self.horizontalScrollBar().setValue(self._scroll_bar_start[0] - dx)
            self.verticalScrollBar().setValue(self._scroll_bar_start[1] - dy)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._drag_start_pos = None
            self._scroll_bar_start = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._last_user_interaction = time.monotonic()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: Any) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._interactive_scale(factor)
        self._last_user_interaction = time.monotonic()

    def contextMenuEvent(self, event: Any) -> None:
        menu = QMenu(self)

        def _reset_zoom() -> None:
            self._user_interacted = False
            self._auto_fit()

        menu.addAction("Reset zoom", _reset_zoom)
        menu.exec(event.globalPos())

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if not self._user_interacted:
            self._auto_fit()

    def _auto_fit(self) -> None:
        scene_rect = self.scene().itemsBoundingRect()
        if scene_rect.isNull():
            return
        self.resetTransform()
        self.fitInView(scene_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self.centerOn(scene_rect.center())

    def _current_uniform_scale(self) -> float:
        try:
            val = float(self.transform().m11())
        except Exception:
            return 1.0
        if not math.isfinite(val):
            return 1.0
        return max(1e-6, min(val, 1e6))

    def _compute_fit_scale(self) -> float:
        scene_rect = self.scene().itemsBoundingRect()
        if scene_rect.isNull():
            return 1.0
        view_w = self.viewport().width()
        view_h = self.viewport().height()
        scene_w = scene_rect.width()
        scene_h = scene_rect.height()
        if scene_w <= 0 or scene_h <= 0 or view_w <= 0 or view_h <= 0:
            return 1.0
        scale_w = view_w / scene_w
        scale_h = view_h / scene_h
        return float(max(1e-6, min(scale_w, scale_h)))

    def _interactive_scale(self, factor: float) -> None:
        cur = self._current_uniform_scale()
        target = cur * factor
        fit = self._compute_fit_scale()
        eps = 1e-9
        if factor < 1.0:
            if cur <= fit * (1.0 + eps):
                return
            if target <= fit * (1.0 + eps):
                self._user_interacted = False
                self._auto_fit()
                return
        self.scale(factor, factor)
        self._user_interacted = True


class CodeEditor(QPlainTextEdit):
    """Read-only QASM editor with line numbers and split point markers."""
    
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.line_number_area = LineNumberArea(self)
        self.split_points: set[int] = set()  # 1-indexed line numbers where splits occur
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.update_line_number_area_width(0)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def show_context_menu(self, pos: Any) -> None:
        """Toggle split marker on right-clicked line without showing a menu."""
        cursor = self.cursorForPosition(pos)
        line = cursor.blockNumber() + 1  # Convert to 1-indexed

        if line in self.split_points:
            self.remove_split_point(line)
        else:
            # Verify that a split at this line is allowed (top-level only)
            window = cast("SplitWindow", self.window())
            try:
                allowed = True if not hasattr(window, "is_line_splittable") else window.is_line_splittable(line)
            except Exception:
                allowed = True
            if not allowed:
                # flash a short status message to inform the user
                try:
                    if hasattr(window, "flash_status"):
                        window.flash_status("Cannot split inside gate/loop body (unsupported)")
                    else:
                        # Fallback: set parent status if available
                        parent = getattr(self, "parent", lambda: None)()
                        if parent and hasattr(parent, "status_label"):
                            parent.status_label.setText("Cannot split inside gate/loop body (unsupported)")
                except Exception:
                    pass
                return
            self.add_split_point(line)

    def add_split_point(self, line: int) -> None:
        """Mark a split point at line (1-indexed, means split AFTER this line)."""
        window = self.window()
        if isinstance(window, SplitWindow):
            window.on_split_point_added(line)
            return
        self.split_points.add(line)
        self.line_number_area.update()

    def remove_split_point(self, line: int) -> None:
        """Remove a split point."""
        window = self.window()
        if isinstance(window, SplitWindow):
            window.on_split_point_removed(line)
            return
        self.split_points.discard(line)
        self.line_number_area.update()

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
        self.line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )

    def paint_line_numbers(self, event: Any) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor("#1f1f1f"))
        
        block = self.firstVisibleBlock()
        number = block.blockNumber() + 1  # Convert to 1-indexed
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                # Highlight split point lines
                if number in self.split_points:
                    painter.fillRect(0, top, self.line_number_area.width(), int(self.blockBoundingRect(block).height()), QColor("#ff6b6b"))
                    painter.setPen(QColor("#ffffff"))
                else:
                    painter.setPen(QColor("#808080"))
                
                painter.drawText(0, top, self.line_number_area.width() - 4, 
                               self.fontMetrics().height(), 
                               Qt.AlignmentFlag.AlignRight, str(number))
            
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            number += 1


class LineNumberArea(QWidget):
    def __init__(self, editor: CodeEditor) -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event: Any) -> None:
        self.editor.paint_line_numbers(event)


class SplitWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("QASM3 Splitter")
        self.setGeometry(100, 100, 1400, 900)
        self.current_file: Path | None = None
        self.current_dqc_file: Path | None = None
        self.current_program: Any | None = None
        self.current_dqc_document: DqcDocument | None = None
        self.chunk_dependency_qubits: set[str] = set()
        self.font_size = 10
        
        # Left pane: original code with split markers + tabbed DAG views
        self.editor = CodeEditor()
        editor_panel, _ = self.make_titled_panel("QASM original (right-click to toggle split)", "#d8ecff", self.editor)

        self.qiskit_dag_view = QiskitDagView()
        self.flow_graph_view = ChunkDagView()
        self.qiskit_dag_view.setMinimumHeight(180)
        self.flow_graph_view.setMinimumHeight(180)
        self.dag_tabs = QTabWidget()
        self.dag_tabs.addTab(self.qiskit_dag_view, "Overall DAG")
        self.dag_tabs.addTab(self.flow_graph_view, "Chunk dependencies")
        self.dag_tabs.setCurrentIndex(0)
        dag_panel, _ = self.make_titled_panel("DAG views", "#dbeed8", self.dag_tabs)

        left_splitter = QSplitter(Qt.Orientation.Vertical)
        left_splitter.addWidget(editor_panel)
        left_splitter.addWidget(dag_panel)
        left_splitter.setStretchFactor(0, 3)
        left_splitter.setStretchFactor(1, 1)
        left_splitter.setSizes([640, 220])
        
        # Right pane: rewritten preview or tabbed chunks
        self.rewritten_chunk_view = QPlainTextEdit()
        self.rewritten_chunk_view.setReadOnly(True)
        self.rewritten_chunk_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.chunk_tabs = QTabWidget()
        self.chunk_stack = QStackedWidget()
        self.chunk_stack.addWidget(self.rewritten_chunk_view)
        self.chunk_stack.addWidget(self.chunk_tabs)
        chunk_panel, _ = self.make_titled_panel("Chunks (rewritten)", "#ffe7c2", self.chunk_stack)
        
        # Main horizontal splitter (full height)
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.addWidget(left_splitter)
        main_splitter.addWidget(chunk_panel)
        main_splitter.setStretchFactor(0, 1)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setSizes([700, 700])
        
        # Root layout - just the splitter (buttons in toolbar)
        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(main_splitter)
        
        central = QWidget()
        central.setLayout(root_layout)
        self.setCentralWidget(central)
        
        # Status label
        self.status_label = QLabel("Load a QASM file or chunks directory to begin")
        
        # Toolbar for buttons
        self.build_toolbar()
        self.build_menu()
        self.apply_font()

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

    def build_toolbar(self) -> None:
        """Create compact toolbar with action buttons."""
        toolbar = QToolBar("Actions", self)
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)
        
        self.split_button = QPushButton("Save & Create Chunks")
        self.split_button.clicked.connect(self.save_chunks)
        self.split_button.setEnabled(False)
        toolbar.addWidget(self.split_button)
        
        self.run_button = QPushButton("Run Chunks")
        self.run_button.clicked.connect(self.run_chunks)
        self.run_button.setEnabled(False)
        toolbar.addWidget(self.run_button)
        
        toolbar.addSeparator()
        toolbar.addWidget(self.status_label)

    def build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        
        open_action = QAction("Open QASM/DQC file...", self)
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)

        file_menu.addSeparator()
        
        examples_menu = file_menu.addMenu("QASM examples")
        for path in sorted(EXAMPLES.glob("*.qasm")):
            if "problematic" in str(path):
                continue
            action = QAction(path.name, self)
            action.triggered.connect(lambda _=False, p=path: self.load_file(p))
            examples_menu.addAction(action)

        # DQC chunks submenu: list .dqc files under examples/chunks/*/*.dqc
        chunks_menu = file_menu.addMenu("DQC chunks")
        chunks_root = EXAMPLES / "chunks"
        if chunks_root.exists():
            for path in sorted(chunks_root.glob("*/*.dqc")):
                action = QAction(path.parent.name, self)
                action.setToolTip(str(path))
                action.triggered.connect(lambda _=False, p=path: self.load_dqc_file(p))
                chunks_menu.addAction(action)
        
        file_menu.addSeparator()
        
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

    def apply_font(self) -> None:
        font = QFont("DejaVu Sans Mono", self.font_size)
        for widget in (self.editor, self.rewritten_chunk_view, self.chunk_tabs, self.dag_tabs, self.qiskit_dag_view, self.flow_graph_view):
            widget.setFont(font)

    def set_font_size(self, size: int) -> None:
        self.font_size = size
        self.apply_font()
        self.refresh_chunk_view()

    def open_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Open QASM or DQC", str(EXAMPLES), "QASM/DQC files (*.qasm *.dqc);;All files (*)"
        )
        if name:
            self.load_file(Path(name))

    def load_file(self, path: Path) -> None:
        if path.suffix.lower() == ".dqc":
            self.load_dqc_file(path)
            return
        try:
            text = path.read_text()
            self.editor.setPlainText(text)
            self.editor.split_points.clear()
            self.current_file = path
            self.current_dqc_file = None
            self.current_program = None
            self.current_dqc_document = None
            
            # Parse to get statements and mark them
            try:
                self.current_program = parse(text)
            except Exception:
                pass
            
            apply_gray_line_numbers(self.editor, set())
            self.refresh_chunk_view()
            self.split_button.setEnabled(True)
            self.run_button.setEnabled(False)
            self.status_label.setText(f"Loaded {path.name}")
            self.setWindowTitle(f"QASM3 Splitter - {path.resolve()}")
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            self.status_label.setText("Load failed")

    def load_dqc_file(self, path: Path) -> None:
        try:
            text = path.read_text()
            document = parse_dqc_text(text)

            self.current_file = None
            self.current_dqc_file = path
            self.current_program = None
            self.current_dqc_document = document

            try:
                self.current_program = parse(document.raw_text)
            except Exception:
                pass

            self.editor.setPlainText(text)
            self.editor.split_points = set(document.pragma_line_numbers)
            self.editor.line_number_area.update()
            apply_gray_line_numbers(self.editor, document.pragma_line_numbers)

            self.refresh_chunk_view()
            self.split_button.setEnabled(True)
            self.run_button.setEnabled(True)
            self.status_label.setText(f"Loaded {path.name}")
            self.setWindowTitle(f"QASM3 Splitter - {path.resolve()}")
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            self.status_label.setText("Load failed")

    def extract_chunks_by_lines(self, text: str, split_after_lines: set[int]) -> list[str]:
        """Split text after specified 1-indexed lines."""
        lines = text.splitlines(keepends=True)
        if not split_after_lines:
            return [text]
        
        chunks: list[str] = []
        current_chunk: list[str] = []
        split_after_sorted = sorted(split_after_lines)
        split_idx = 0
        
        for i, line in enumerate(lines):
            line_num = i + 1  # 1-indexed
            current_chunk.append(line)
            
            if split_idx < len(split_after_sorted) and line_num == split_after_sorted[split_idx]:
                chunks.append("".join(current_chunk))
                current_chunk = []
                split_idx += 1
        
        if current_chunk:
            chunks.append("".join(current_chunk))
        
        return chunks


    # Inter-chunk reference UI removed.

    def is_line_splittable(self, line: int) -> bool:
        """Return True if the given 1-indexed line is a top-level location where splitting is allowed.

        Splitting inside gate bodies, loops, subroutines, boxes, and similar inner scopes
        is not allowed. If the program failed to parse, allow splitting (we can't determine).
        """
        if self.current_program is None:
            return True

        if self.current_dqc_document is not None:
            document = parse_dqc_text(self.editor.toPlainText())
            if line in document.pragma_line_numbers:
                return True
            line = document.display_to_raw_after_line.get(line, line)

        return not line_is_inside_blocking_scope(self.current_program, line)

    def flash_status(self, message: str, timeout_ms: int = 1800) -> None:
        """Temporarily show `message` in the status label, then restore previous text."""
        prev = self.status_label.text()
        self.status_label.setText(message)
        # Use red text for visibility
        self.status_label.setStyleSheet("color: #cc0000; font-weight: 700;")
        def _restore() -> None:
            self.status_label.setText(prev)
            self.status_label.setStyleSheet("")
        QTimer.singleShot(timeout_ms, _restore)

    def preview_chunks(self) -> list[tuple[str, str, str]]:
        """Generate and preview chunks. Returns list of (name, original, rewritten)."""
        if not self.editor.toPlainText():
            return []

        if self.current_dqc_document is not None:
            document = parse_dqc_text(self.editor.toPlainText())
            result: list[tuple[str, str, str]] = []
            for chunk in document.chunks:
                try:
                    chunk_text = prepare_chunk_text_for_run(chunk.text, document.raw_text)
                    rewritten, _, _ = transpile_for_split(chunk_text)
                except Exception as exc:
                    rewritten = f"[ERROR: {exc}]"
                result.append((f"Chunk {chunk.index}", chunk.text, rewritten))
            return result

        original_text = self.editor.toPlainText()
        chunks = self.extract_chunks_by_lines(original_text, self.editor.split_points)

        result: list[tuple[str, str, str]] = []
        for i, chunk_text in enumerate(chunks, 1):
            try:
                chunk_for_run = prepare_chunk_text_for_run(chunk_text, original_text)
                rewritten, _, _ = transpile_for_split(chunk_for_run)
            except Exception as exc:
                rewritten = f"[ERROR: {exc}]"

            result.append((f"Chunk {i}", chunk_text, rewritten))

        return result

    def refresh_dag_views(self, chunks: list[tuple[str, str, str]], source_text: str) -> list[ChunkFlow]:
        font = QFont("DejaVu Sans Mono", self.font_size)

        try:
            rewritten, _, _ = transpile_for_split(source_text)
            circuit = qiskit_parse(rewritten)
            self.qiskit_dag_view.set_circuit(circuit, font)
            self.chunk_dependency_qubits = collect_qubit_register_names(circuit)
        except Exception as exc:
            self.qiskit_dag_view.set_message(f"Qiskit DAG unavailable: {exc}", font)
            self.chunk_dependency_qubits = set()

        flows = compute_chunk_flows([original for _, original, _ in chunks], source_text)
        self.flow_graph_view.set_flows(flows, font)
        return flows

    def _make_flow_panel(self, title: str, text_html: str) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(
            "QWidget { background-color: #f3f3f3; border: 1px solid #d2d2d2; border-radius: 3px; }"
        )
        blue = "#2f6fff"
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(3, 1, 3, 1)
        layout.setSpacing(0)

        header = QLabel(title)
        header.setAlignment(Qt.AlignmentFlag.AlignLeft)
        header.setFixedHeight(12)
        header.setStyleSheet(f"color: {blue}; font-size: 10px; font-weight: 600; margin: 0px;")
        layout.addWidget(header)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        body.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        body.setStyleSheet(
            f"background: transparent; border: none; padding: 0px; margin: 0px; color: {blue}; font-family: 'DejaVu Sans Mono';"
        )
        body.document().setDocumentMargin(0)
        body.setHtml(f'<pre style="margin:0; color:{blue};">{text_html}</pre>')
        body.setFont(QFont("DejaVu Sans Mono", self.font_size))
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(body)
        layout.setStretch(0, 0)
        layout.setStretch(1, 1)

        return panel

    def refresh_chunk_view(self) -> None:
        """Refresh the rewritten preview or split chunk tabs."""
        if not self.current_file and self.current_dqc_document is None:
            return

        chunks = self.preview_chunks()
        source_text = self.editor.toPlainText()
        if self.current_dqc_document is not None:
            source_text = parse_dqc_text(source_text).raw_text
        flows = self.refresh_dag_views(chunks, source_text)

        if not self.editor.split_points:
            self.chunk_stack.setCurrentWidget(self.rewritten_chunk_view)
            rewritten = chunks[0][2] if chunks else ""
            apply_gray_include_format(self.rewritten_chunk_view, rewritten)
            return

        self.chunk_stack.setCurrentWidget(self.chunk_tabs)
        self.chunk_tabs.clear()

        for (name, _, rewritten), flow in zip(chunks, flows):
            container = QWidget()
            v = QVBoxLayout(container)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(4)

            header = self._make_flow_panel(
                "Importing:",
                format_flow_lines_html(flow.incoming_sources, "<-", self.chunk_dependency_qubits),
            )
            footer = self._make_flow_panel(
                "Exporting:",
                format_flow_lines_html(flow.outgoing_targets, "->", self.chunk_dependency_qubits),
            )

            bottom = QPlainTextEdit()
            bottom.setReadOnly(True)
            bottom.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            bottom.setFont(QFont("DejaVu Sans Mono", self.font_size))
            apply_gray_include_format(bottom, rewritten)

            v.addWidget(header)
            v.addWidget(bottom)
            v.addWidget(footer)
            v.setStretch(0, 1)
            v.setStretch(1, 2)
            v.setStretch(2, 1)

            self.chunk_tabs.addTab(container, name)

    def refresh_chunk_tabs(self) -> None:
        """Backward-compatible alias for refreshing the chunk view."""
        self.refresh_chunk_view()

    def on_split_point_added(self, line: int) -> None:
        if self.current_dqc_document is None:
            self.editor.split_points.add(line)
            self.editor.line_number_area.update()
            self.refresh_chunk_view()
            return

        document = parse_dqc_text(self.editor.toPlainText())
        raw_text = document.raw_text
        raw_split_after_lines = display_split_lines_to_raw_split_after_lines(
            document,
            self.editor.split_points | {line},
        )
        dqc_text = render_dqc_text(raw_text, raw_split_after_lines)
        updated = parse_dqc_text(dqc_text)
        self.current_dqc_document = updated
        self.editor.setPlainText(updated.source_text)
        self.editor.split_points = set(updated.pragma_line_numbers)
        self.editor.line_number_area.update()
        apply_gray_line_numbers(self.editor, updated.pragma_line_numbers)
        self.refresh_chunk_view()

    def on_split_point_removed(self, line: int) -> None:
        if self.current_dqc_document is None:
            self.editor.split_points.discard(line)
            self.editor.line_number_area.update()
            self.refresh_chunk_view()
            return

        document = parse_dqc_text(self.editor.toPlainText())
        raw_text = document.raw_text
        raw_split_after_lines = display_split_lines_to_raw_split_after_lines(
            document,
            {split_line for split_line in self.editor.split_points if split_line != line},
        )
        dqc_text = render_dqc_text(raw_text, raw_split_after_lines)
        updated = parse_dqc_text(dqc_text)
        self.current_dqc_document = updated
        self.editor.setPlainText(updated.source_text)
        self.editor.split_points = set(updated.pragma_line_numbers)
        self.editor.line_number_area.update()
        apply_gray_line_numbers(self.editor, updated.pragma_line_numbers)
        self.refresh_chunk_view()

    def save_chunks(self) -> None:
        """Save the current split state as a single DQC file."""
        if not self.current_file and self.current_dqc_file is None:
            QMessageBox.warning(self, "No file", "Load a QASM or DQC file first")
            return

        base_name = self.current_file.stem if self.current_file else (self.current_dqc_file.stem if self.current_dqc_file else None)
        if not base_name:
            QMessageBox.warning(self, "No chunks", "No file name available to save")
            return

        chunks_parent = EXAMPLES / "chunks"
        chunks_parent.mkdir(parents=True, exist_ok=True)
        out_dir = chunks_parent / base_name
        clear_directory_contents(out_dir)

        if self.current_dqc_document is not None:
            document = parse_dqc_text(self.editor.toPlainText())
            raw_text = document.raw_text
            split_after_lines = document.raw_split_after_lines
        else:
            raw_text = self.editor.toPlainText()
            split_after_lines = set(self.editor.split_points)

        dqc_text = render_dqc_text(raw_text, split_after_lines)
        dqc_file = out_dir / f"{base_name}.dqc"
        dqc_file.write_text(dqc_text)

        self.current_dqc_file = dqc_file
        self.current_dqc_document = parse_dqc_text(dqc_text)
        self.editor.setPlainText(self.current_dqc_document.source_text)
        self.editor.split_points = set(self.current_dqc_document.pragma_line_numbers)
        self.editor.line_number_area.update()
        apply_gray_line_numbers(self.editor, self.current_dqc_document.pragma_line_numbers)

        self.split_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.refresh_chunk_view()
        
        self.status_label.setText(f"Saved DQC file to examples/chunks/{base_name}/{base_name}.dqc")
        QMessageBox.information(
            self,
            "DQC saved",
            f"Saved DQC file to:\n{dqc_file}",
        )

    def run_chunks(self) -> None:
        """Launch run.py in tabbed mode using the current DQC file."""
        if not self.current_file and self.current_dqc_file is None:
            QMessageBox.warning(self, "No chunks", "Load a QASM or DQC file first")
            return

        base_name = self.current_file.stem if self.current_file else (self.current_dqc_file.stem if self.current_dqc_file else None)
        if not base_name:
            QMessageBox.warning(self, "No chunks", "No file name available to run")
            return

        # Prepare chunk texts and pass them to run.py via stdin as JSON
        if self.current_dqc_document is not None:
            document = parse_dqc_text(self.editor.toPlainText())
            chunks: list[tuple[str,str]] = []
            for chunk in document.chunks:
                chunk_for_run = prepare_chunk_text_for_run(chunk.text, document.raw_text)
                try:
                    rewritten, _, _ = transpile_for_split(chunk_for_run)
                except Exception:
                    rewritten = chunk_for_run
                chunks.append((f"Chunk {chunk.index}", rewritten))
        else:
            original_text = self.editor.toPlainText()
            piece_texts = self.extract_chunks_by_lines(original_text, set(self.editor.split_points))
            chunks = []
            for i, text in enumerate(piece_texts, 1):
                chunk_for_run = prepare_chunk_text_for_run(text, original_text)
                try:
                    rewritten, _, _ = transpile_for_split(chunk_for_run)
                except Exception:
                    rewritten = chunk_for_run
                chunks.append((f"Chunk {i}", rewritten))

        try:
            script = ROOT / "run.py"
            proc = subprocess.Popen(
                [sys.executable, str(script), "--chunks-stdin"],
                stdin=subprocess.PIPE,
                cwd=str(ROOT),
            )
            payload = json.dumps(chunks)
            # Write and close stdin so the child can proceed
            if proc.stdin:
                proc.stdin.write(payload.encode("utf-8"))
                proc.stdin.close()
        except Exception as exc:
            QMessageBox.critical(self, "Launch failed", f"Failed to launch run.py: {exc}")
            return

        self.status_label.setText(f"Launched run.py with {len(chunks)} chunks")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("QASM3 Splitter")
    app.setOrganizationName("Copilot")
    
    window = SplitWindow()
    window.show()
    
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
