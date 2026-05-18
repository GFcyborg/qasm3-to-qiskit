from __future__ import annotations

import dataclasses
import concurrent.futures
from html import escape
import json
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
from urllib import request

import PySide6
import qiskit_qasm3_import
from PySide6.QtCore import QObject, QRunnable, QRect, QSize, Qt, QThreadPool, QTimer, Signal, QEvent
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPainter, QPixmap, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QGraphicsScene,
    QGraphicsView,
    QGraphicsPixmapItem,
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
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from openqasm3 import dumps, parse
from qasm_rewriter import (
    Issue, append_issue, transpile_qasm, emit_stmt, eval_node, eval_text,
    span, contains_timing_constructs, is_supported_decl, rewrite_condition_text,
    inline_subroutine_call, extract_qasm_version, subst_env, substitute_names,
    inline_call_assignment_if_pattern, logical_meas_call_pattern,
    majority_vote_pairs_from_subroutine, node_iter, range_values,
    stdgates_compat_lines, subroutine_is_inlineable, subroutine_param_names, kind,
)
from dqc_container import parse_dqc_text, prepare_chunk_text_for_run
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
    "Rename colliding user-defined gates to my_<name> so stdgates definitions remain untouched.",
    "Keep reset, measurement, gate definitions, and quantum gate operations.",
    "Substitute compile-time environment values into emitted statements.",
    "Normalize uint tokens to int in rewritten output.",
    "Strip residual stretch and duration forms in post-processing.",
)


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


class VersionCheckSignals(QObject):
    finished = Signal(str)


class VersionCheckWorker(QRunnable):
    def __init__(self, pyside6_version: str, openqasm3_version: str, antlr4_version: str,
                 matplotlib_version: str, pylatexenc_version: str, qiskit_version: str,
                 qiskit_aer_version: str, qiskit_qasm3_import_version: str, shots: int) -> None:
        super().__init__()
        self.pyside6_version = pyside6_version
        self.openqasm3_version = openqasm3_version
        self.antlr4_version = antlr4_version
        self.matplotlib_version = matplotlib_version
        self.pylatexenc_version = pylatexenc_version
        self.qiskit_version = qiskit_version
        self.qiskit_aer_version = qiskit_aer_version
        self.qiskit_qasm3_import_version = qiskit_qasm3_import_version
        self.shots = shots
        self.signals = VersionCheckSignals()

    def run(self) -> None:
        lines = [
            f"Python: {sys.version.split()[0]}",
            f"Python executable: {sys.executable}",
            "",
            "Library versions:",
        ]
        try:
            lines.append(format_version_status("PySide6", self.pyside6_version))
        except Exception:
            lines.append(f"PySide6: {self.pyside6_version}")
        try:
            lines.append(format_version_status("openqasm3", self.openqasm3_version))
        except Exception:
            lines.append(f"openqasm3: {self.openqasm3_version}")
        try:
            lines.append(format_version_status("antlr4-python3-runtime", self.antlr4_version))
        except Exception:
            lines.append(f"antlr4-python3-runtime: {self.antlr4_version}")
        try:
            lines.append(format_version_status("matplotlib", self.matplotlib_version))
        except Exception:
            lines.append(f"matplotlib: {self.matplotlib_version}")
        try:
            lines.append(format_version_status("pylatexenc", self.pylatexenc_version))
        except Exception:
            lines.append(f"pylatexenc: {self.pylatexenc_version}")
        lines.extend(["", "Qiskit runtime:"])
        try:
            lines.append(f"  {format_version_status('qiskit', self.qiskit_version)}")
        except Exception:
            lines.append(f"  qiskit: {self.qiskit_version}")
        try:
            lines.append(f"  {format_version_status('qiskit-aer', self.qiskit_aer_version)}")
        except Exception:
            lines.append(f"  qiskit-aer: {self.qiskit_aer_version}")
        try:
            lines.append(f"  {format_version_status('qiskit-qasm3-import', self.qiskit_qasm3_import_version)}")
        except Exception:
            lines.append(f"  qiskit-qasm3-import: {self.qiskit_qasm3_import_version}")
        lines.append("")
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
            lines.append("Qiskit smoke tests:")
            lines.append(f"  Aer backend: {backend.name}")
            lines.append(f"  qasm3 parse smoke: ok ({t_parse:.1f} ms)")
            lines.append(f"  transpile smoke: ok ({t_transpile:.1f} ms)")
            lines.append(f"  run smoke (Hadamard gate, {self.shots} shots): ok ({t_run:.1f} ms)")
            lines.append(f"  counts sample (Hadamard gate): {result.get_counts()}")
        except Exception as exc:
            lines.append(f"Qiskit smoke tests: failed ({exc})")
        self.signals.finished.emit("\n".join(lines))




def to_pos(editor: QPlainTextEdit, line: int, column: int) -> int:
    block = editor.document().findBlockByNumber(max(0, line))
    if not block.isValid():
        return max(0, len(editor.toPlainText()))
    pos = block.position() + max(0, column)
    return max(0, min(pos, max(0, len(editor.toPlainText()) - 1)))


def clamp_cursor_pos(editor: QPlainTextEdit, pos: int) -> int:
    limit = max(0, len(editor.toPlainText()) - 1)
    return max(0, min(pos, limit))



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


def get_latest_package_version(package_name: str) -> str | None:
    """Fetch the latest version of a package from PyPI.
    
    Args:
        package_name: Name of the package on PyPI.
        
    Returns:
        Latest version string, or None if unable to fetch.
    """
    try:
        url = f"https://pypi.org/pypi/{package_name}/json"
        with request.urlopen(url, timeout=3) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data.get('info', {}).get('version')
    except Exception:
        return None


def format_version_status(package_name: str, current_version: str) -> str:
    """Format version status for a package (current vs latest).
    
    Args:
        package_name: Name of the package.
        current_version: Currently installed version.
        
    Returns:
        Formatted string with version info and status indicator.
    """
    latest = get_latest_package_version(package_name)
    if latest is None:
        return f"{package_name}: {current_version}"
    if latest == current_version:
        return f"{package_name}: {current_version} (up-to-date)"
    return f"{package_name}: {current_version} → {latest} (update available)"




















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


def mark_rewrite_issues(issues: list[Issue], editor: QPlainTextEdit) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for issue in issues:
        if issue.start <= 0 or issue.end <= 0:
            continue
        start = to_pos(editor, issue.start - 1, 0)
        end = clamp_cursor_pos(editor, to_pos(editor, issue.end - 1, 999999))
        if end >= start:
            spans.append((start, end, issue.detail))
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
        # Whether the user has manually panned or zoomed the view.
        # If True, automatic re-fitting on resize is suppressed until reset.
        self._user_interacted = False
        # Timestamp of the last user interaction (wheel/drag) to avoid
        # immediate auto-fit while the user is actively interacting.
        self._last_user_interaction: float = 0.0
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
        item = self.scene().addPixmap(placeholder)
        # Ensure the scene rect matches the pixmap so fitInView works.
        self.scene().setSceneRect(item.boundingRect())
        # Auto-fit only when the user hasn't manually interacted.
        if not self._user_interacted:
            self.resetTransform()
            self.fitInView(item.boundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self.centerOn(item.boundingRect().center())

    def set_image(self, image_bytes: bytes) -> None:
        self.scene().clear()
        pixmap = QPixmap()
        if not pixmap.loadFromData(image_bytes):
            self.show_placeholder()
            return
        item = self.scene().addPixmap(pixmap)
        # Make sure the scene rect covers the pixmap so subsequent
        # fitInView/centering is correct even if the scene was previously
        # empty or had different extents.
        self.scene().setSceneRect(item.boundingRect())
        # When a new circuit image is loaded, reset to the automatic
        # visualization: clear any previous manual interaction and
        # auto-fit/center the new image so each circuit starts maximized.
        self._user_interacted = False
        self._last_user_interaction = 0.0
        self.resetTransform()
        self.fitInView(item.boundingRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.centerOn(item.boundingRect().center())

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            # mark as user interaction when starting to drag
            self._user_interacted = True
            self._last_user_interaction = time.monotonic()
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
            # any dragging counts as manual movement
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
        # Use the interactive scaler which enforces a minimum (fit) scale
        # so zooming out doesn't make the image smaller than the fitted size.
        self._interactive_scale(factor)
        self._last_user_interaction = time.monotonic()

    def contextMenuEvent(self, event: Any) -> None:
        menu = QMenu(self)
        # Reset zoom restores automatic fitting behavior as if the user hadn't interacted.
        def _reset_zoom():
            self._user_interacted = False
            self._auto_fit_vertical_and_center()
        menu.addAction("Copy image (original size)", self._copy_image_to_clipboard)
        menu.addAction("Reset zoom", _reset_zoom)
        menu.exec(event.globalPos())

    def eventFilter(self, obj: QObject, event: Any) -> bool:
        # Listen for resize events coming from the runtime-results text area
        # (installed in MainWindow.__init__). When that area is resized, force
        # the circuit view to V-fit and H-center the image.
        if event.type() == QEvent.Type.Resize:
            # When the runtime-results area is resized, re-fit the circuit
            # view unless the user has very recently interacted (zoom/pan).
            # This avoids snapping the view back to fit while the user is
            # actively zooming.
            now = time.monotonic()
            if now - self._last_user_interaction > 0.25:
                self._user_interacted = False
                self._auto_fit_vertical_and_center()
        return super().eventFilter(obj, event)

    def _copy_image_to_clipboard(self) -> None:
        # Find the first QGraphicsPixmapItem in the scene and copy its
        # underlying pixmap (original size) to the clipboard as an image.
        for item in self.scene().items():
            if isinstance(item, QGraphicsPixmapItem):
                pix = item.pixmap()
                if not pix.isNull():
                    img = pix.toImage()
                    QApplication.clipboard().setImage(img)
                return

    def resizeEvent(self, event: Any) -> None:
        # When the view is resized (including indirect resizing via splitters),
        # re-apply automatic vertical-fit and horizontal centering unless the
        # user has manually panned/zoomed the view.
        super().resizeEvent(event)
        if not self._user_interacted:
            self._auto_fit_vertical_and_center()

    def _auto_fit_vertical_and_center(self) -> None:
        """Fit the scene into the viewport while preserving aspect ratio and
        center it. Uses `fitInView` so the image will be as large as possible
        while still fully visible after area resizes.
        """
        scene_rect = self.scene().itemsBoundingRect()
        if scene_rect.isNull():
            return

        view_w = self.viewport().width()
        view_h = self.viewport().height()
        if view_w <= 0 or view_h <= 0:
            return

        # Reset any previous transform and fit the full scene into the view
        # while keeping the original aspect ratio.
        self.resetTransform()
        self.fitInView(scene_rect, Qt.AspectRatioMode.KeepAspectRatio)
        # Ensure the scene is centered in the view.
        self.centerOn(scene_rect.center())

    def _current_uniform_scale(self) -> float:
        """Return the current uniform X scale from the view transform."""
        # QTransform is uniform because we always scale uniformly.
        try:
            val = float(self.transform().m11())
        except Exception:
            return 1.0
        # Guard against degenerate or infinite values from repeated scaling.
        if not math.isfinite(val):
            return 1.0
        return max(1e-6, min(val, 1e6))

    def _compute_fit_scale(self) -> float:
        """Compute the uniform scale at which the full scene fits the viewport.

        This mirrors `fitInView` scaling: choose the smaller of width/height
        scales so the entire scene is visible while preserving aspect ratio.
        """
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
        """Apply an interactive scale factor while preventing zoom-out below fit.

        - Zoom-in (factor>1): always allowed and marks the view as manually
          interacted so automatic re-fitting on resize is suppressed.
        - Zoom-out (factor<1): allowed until the fitted scale is reached; once
          the fitted scale would be exceeded (image fully visible), restore
          the auto-fit state instead of scaling smaller.
        """
        # Compute current, target and fit scales.
        cur = self._current_uniform_scale()
        target = cur * factor
        fit = self._compute_fit_scale()

        # Allow tiny epsilon to avoid float noise when comparing scales.
        eps = 1e-9

        # Zoom-out behavior: do nothing if we're already at (or below) fit;
        # if the requested target would go below fit, clamp to fit and
        # re-enable auto-fit. This prevents any zoom-out beyond the fitted
        # size.
        if factor < 1.0:
            if cur <= fit * (1.0 + eps):
                # Already at or below fit, ignore further zoom-out.
                return
            if target <= fit * (1.0 + eps):
                # Clamp to fit and restore auto-fit behavior.
                self._user_interacted = False
                self._auto_fit_vertical_and_center()
                return

        # For zoom-in (factor>1) or allowed zoom-out above fit, apply scale
        # and mark as a manual interaction so it persists across resizes.
        self.scale(factor, factor)
        self._user_interacted = True


class RulesDialog(QDialog):
    def __init__(self, text: str, parent: QWidget | None = None, title: str = "Rewrite rules") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        self.box = QTextEdit()
        self.box.setReadOnly(True)
        self.box.setHtml(self._format_text(text))
        layout.addWidget(self.box)

    def update_text(self, text: str) -> None:
        """Update the dialog's text content."""
        self.box.setHtml(self._format_text(text))

    def _format_text(self, text: str) -> str:
        def is_section_heading(line: str) -> bool:
            stripped = line.strip()
            if not stripped:
                return False
            if stripped.startswith("-"):
                return False
            if stripped.endswith(":"):
                return True
            if stripped.isupper() and any(ch.isalpha() for ch in stripped):
                return True
            return stripped in {
                "INSERTIONS",
                "FOLDING / UNROLLING / UNBOXING",
                "DATA-TYPE CASTINGS",
                "OTHERS",
                "DROPPING",
            }

        html_lines: list[str] = [
            '<div style="font-family: monospace; white-space: pre-wrap; line-height: 1.35;">'
        ]
        for line in text.splitlines():
            safe = escape(line)
            if is_section_heading(line):
                html_lines.append(f"<div><strong>{safe}</strong></div>")
            else:
                html_lines.append(f"<div>{safe}</div>")
        html_lines.append("</div>")
        return "\n".join(html_lines)


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

    def __init__(self, file_to_load: str | None = None, load_default: bool = True) -> None:
        """Initialize the main window.
        
        Args:
            file_to_load: Optional path to a QASM file to load at startup.
        """
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
        # Ensure resizing the runtime text area triggers an auto-fit of the
        # circuit view. CircuitView implements an eventFilter to handle this.
        self.circuit_info.installEventFilter(self.circuit)

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
        # Load specified file or default if no file_to_load provided
        if file_to_load:
            try:
                path = Path(file_to_load)
                if path.exists():
                    self.load_path(path)
                else:
                    print(f"Warning: File not found: {file_to_load}", file=sys.stderr)
                    if load_default:
                        self.load_default()
            except Exception as e:
                print(f"Error loading file {file_to_load}: {e}", file=sys.stderr)
                if load_default:
                    self.load_default()
        elif load_default:
            self.load_default()

    def build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        open_action = QAction("Open QASM/DQC...", self)
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
        # Circuit view zoom controls
        zoom_circ_in = QAction("Zoom circuit in", self)
        zoom_circ_in.setShortcut("Ctrl++")
        zoom_circ_in.triggered.connect(lambda: self.circuit._interactive_scale(1.15))
        view_menu.addAction(zoom_circ_in)
        zoom_circ_out = QAction("Zoom circuit out", self)
        zoom_circ_out.setShortcut("Ctrl+-")
        zoom_circ_out.triggered.connect(lambda: self.circuit._interactive_scale(1 / 1.15))
        view_menu.addAction(zoom_circ_out)
        reset_circ = QAction("Reset circuit zoom", self)
        reset_circ.triggered.connect(lambda: (setattr(self.circuit, '_user_interacted', False), self.circuit._auto_fit_vertical_and_center()))
        view_menu.addAction(reset_circ)
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
        name, _ = QFileDialog.getOpenFileName(self, "Open QASM/DQC", str(EXAMPLES), "QASM/DQC files (*.qasm *.inc *.dqc);;All files (*)")
        if name:
            self.load_path(Path(name))

    def load_source(self, source_text: str, title: str | None = None) -> None:
        self._syncing = True
        try:
            self.editor.setPlainText(source_text)
            self.editor.set_issue_spans([])
            self.editor.set_include_spans([])
            if title is None:
                self.setWindowTitle(self.base_title)
            else:
                self.setWindowTitle(f"{self.base_title} - {title}")
            self.statusBar().showMessage("Loaded source")
        finally:
            self._syncing = False
        self.refresh_views()

    def load_path(self, path: Path) -> None:
        self.load_source(path.read_text(), str(path.resolve()))

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
        issue_spans = mark_unsupported(program, self.editor) if program else []
        issue_spans.extend(mark_rewrite_issues(issues, self.editor))
        self.editor.set_issue_spans(issue_spans)
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
        fig = cast(Any, circuit_drawer(
            circuit,
            output="mpl",
            fold=500,
            vertical_compression="low",
            cregbundle=False,
            expr_len=60,
        ))
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

        # Collect version info
        pyside6_version = PySide6.__version__
        openqasm3_version = getattr(sys.modules.get('openqasm3'), '__version__', 'unknown')
        qiskit_version = getattr(sys.modules.get('qiskit'), '__version__', 'unknown')
        qiskit_aer_version = getattr(sys.modules.get('qiskit_aer'), '__version__', 'unknown')
        qiskit_qasm3_import_version = getattr(qiskit_qasm3_import, '__version__', 'unknown')
        matplotlib_version = getattr(sys.modules.get('matplotlib'), '__version__', 'unknown')
        pylatexenc_version = getattr(sys.modules.get('pylatexenc'), '__version__', 'unknown')

        # Show initial dialog with placeholder
        initial_text = (
            f"Python: {sys.version.split()[0]}\n"
            f"Python executable: {sys.executable}\n"
            "\n"
            "Library versions:\n"
            f"  PySide6: {pyside6_version}\n"
            f"  openqasm3: {openqasm3_version}\n"
            f"  antlr4-python3-runtime: {antlr4_version}\n"
            f"  matplotlib: {matplotlib_version}\n"
            f"  pylatexenc: {pylatexenc_version}\n"
            "\n"
            "Qiskit runtime:\n"
            f"  qiskit: {qiskit_version}\n"
            f"  qiskit-aer: {qiskit_aer_version}\n"
            f"  qiskit-qasm3-import: {qiskit_qasm3_import_version}\n"
            "\n"
            "⟳ Checking for updates..."
        )
        dialog = RulesDialog(initial_text, self, title="Diagnostics")

        # Start background version check worker
        worker = VersionCheckWorker(
            pyside6_version, openqasm3_version, antlr4_version,
            matplotlib_version, pylatexenc_version, qiskit_version,
            qiskit_aer_version, qiskit_qasm3_import_version, self.shots
        )
        worker.signals.finished.connect(lambda text: dialog.update_text(text))
        if not hasattr(self, '_thread_pool'):
            self._thread_pool = QThreadPool()
        self._thread_pool.start(worker)

        dialog.exec()

    def show_rules(self) -> None:
        dialog = RulesDialog(self.load_rewrite_rules_text(), self)
        dialog.showMaximized()
        dialog.exec()

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


class ChunkTabsWindow(QMainWindow):
    """Host one full `MainWindow` instance per chunk in top-level tabs."""

    def __init__(self, chunk_texts: list[tuple[str, str]], title: str) -> None:
        super().__init__()
        self.base_title = title
        self.setWindowTitle(self.base_title)
        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)

        for chunk_title, chunk_text in chunk_texts:
            child = MainWindow(file_to_load=None, load_default=False)
            child.setParent(self)
            # Set editor content directly to avoid accidental reloading of the full DQC
            child._syncing = True
            try:
                child.editor.setPlainText(chunk_text)
                child.setWindowTitle(f"{self.base_title} - {chunk_title}")
            finally:
                child._syncing = False
            child.refresh_views()
            self._tabs.addTab(child, chunk_title)

        self._tabs.currentChanged.connect(self._refresh_title)
        self._refresh_title(self._tabs.currentIndex())

    def _refresh_title(self, index: int) -> None:
        if 0 <= index < self._tabs.count():
            self.setWindowTitle(f"{self.base_title} - {self._tabs.tabText(index)}")
        else:
            self.setWindowTitle(self.base_title)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="QASM3 Aer Lab - Test OpenQASM 3 with Qiskit")
    parser.add_argument("file", nargs="?", default=None, help="QASM file or directory to load on startup")
    args = parser.parse_args()
    
    app = QApplication(sys.argv)
    app.setApplicationName("QASM3 Aer Lab")
    app.setOrganizationName("Copilot")

    window: QMainWindow
    if args.file:
        path = Path(args.file)
        if path.exists() and path.suffix.lower() == ".dqc":
            document = parse_dqc_text(path.read_text())
            chunk_texts = [
                (f"Chunk {chunk.index}", prepare_chunk_text_for_run(chunk.text, document.raw_text))
                for chunk in document.chunks
            ]
            window = ChunkTabsWindow(chunk_texts, f"QASM3 Aer Lab - {path.name}")
        else:
            window = MainWindow(file_to_load=args.file)
    else:
        window = MainWindow(file_to_load=None)

    screen = app.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        window.resize(int(geo.width() * 0.9), int(geo.height() * 0.9))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
