#!/usr/bin/env python
"""QASM3 Splitter - Split OpenQASM 3 files at statement boundaries.

Allows users to load a QASM file, mark split points via right-click,
preview chunks in tabs, save them to disk, and launch independent run.py instances.
"""

from __future__ import annotations

import sys
import subprocess
import json
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Any, cast

import PySide6
from PySide6.QtCore import Qt, QTimer, QSize, QRect, QEvent
from PySide6.QtGui import QColor, QFont, QPainter, QAction, QKeySequence, QCursor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QSplitter,
    QVBoxLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QLabel,
    QFileDialog,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QStackedWidget,
    QInputDialog,
    QLineEdit,
    QToolBar,
)

from openqasm3 import parse
from qasm_rewriter import kind, span, node_iter, stdgates_compat_lines
import re
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


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


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
        self.font_size = 10
        
        # Left pane: original code with split markers
        self.editor = CodeEditor()
        editor_panel, _ = self.make_titled_panel("Original QASM (right-click to toggle split)", "#d8ecff", self.editor)
        
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
        main_splitter.addWidget(editor_panel)
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
        
        examples_menu = file_menu.addMenu("Examples")
        for path in sorted(EXAMPLES.glob("*.qasm")):
            if "problematic" in str(path):
                continue
            action = QAction(path.name, self)
            action.triggered.connect(lambda _=False, p=path: self.load_file(p))
            examples_menu.addAction(action)
        
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
        for widget in (self.editor, self.rewritten_chunk_view, self.chunk_tabs):
            widget.setFont(font)

    def set_font_size(self, size: int) -> None:
        self.font_size = size
        self.apply_font()

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


    def _collect_defined_and_used(self, text: str) -> tuple[set[str], set[str]]:
        """Return (defined_names, used_identifiers) found in `text` using the parser.

        Falls back to empty sets if parsing fails.
        """
        # Ensure minimal header and stdgates include to aid parsing
        t = text
        try:
            has_header = bool(re.search(r"(?mi)^[ \t]*OPENQASM\b", t))
            if not has_header:
                t = "OPENQASM 3.0;\n" + t
            has_stdgates = bool(re.search(r'(?mi)^\s*include\s+"stdgates\.inc"\s*;', t))
            if not has_stdgates:
                lines = t.splitlines()
                insert_at = 1 if lines and lines[0].strip().upper().startswith("OPENQASM") else 0
                lines.insert(insert_at, 'include "stdgates.inc";')
                t = "\n".join(lines)

            prog = parse(t)
        except Exception:
            return set(), set()

        defined: set[str] = set()
        used: set[str] = set()
        try:
            for stmt in getattr(prog, "statements", []):
                k = kind(stmt)
                if k == "QuantumGateDefinition":
                    name_obj = getattr(stmt, "name", None) or getattr(stmt, "identifier", None)
                    name = getattr(name_obj, "name", None)
                    if name:
                        defined.add(name)
                else:
                    name = getattr(getattr(stmt, "identifier", None), "name", None)
                    if name:
                        defined.add(name)

            for node in node_iter(prog):
                if kind(node) == "Identifier":
                    n = getattr(node, "name", "")
                    if n:
                        used.add(n)
        except Exception:
            return set(), set()

        return defined, used


    def _compute_chunk_references(self, chunk_texts: list[str]) -> list[set[str]]:
        """Compute for each chunk the set of identifiers it references that were defined in earlier chunks."""
        # Gather stdgates names to treat as globally defined
        stdgates_names: set[str] = set()
        for defline in stdgates_compat_lines():
            m = re.match(r"^gate\s+([A-Za-z_]\w*)", defline)
            if m:
                stdgates_names.add(m.group(1))

        cumulative_defined: set[str] = set(stdgates_names)
        results: list[set[str]] = []

        for text in chunk_texts:
            defined, used = self._collect_defined_and_used(text)
            # referenced from previous chunks = used intersection cumulative_defined
            referenced = set(sorted(name for name in used if name in cumulative_defined))
            results.append(referenced)
            cumulative_defined.update(defined)

        # The first chunk cannot reference previous chunks (there are none).
        if results:
            results[0] = set()

        return results

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

    def refresh_chunk_view(self) -> None:
        """Refresh the rewritten preview or split chunk tabs."""
        if not self.current_file and self.current_dqc_document is None:
            return

        if self.current_dqc_document is not None:
            document = parse_dqc_text(self.editor.toPlainText())
            chunks = document.chunks

            if len(chunks) <= 1 or not self.editor.split_points:
                self.chunk_stack.setCurrentWidget(self.rewritten_chunk_view)
                rewritten = ""
                if chunks:
                    try:
                        rewritten, _, _ = transpile_for_split(document.raw_text)
                    except Exception as exc:
                        rewritten = f"[ERROR: {exc}]"
                apply_gray_include_format(self.rewritten_chunk_view, rewritten)
                return

            self.chunk_stack.setCurrentWidget(self.chunk_tabs)
            self.chunk_tabs.clear()

            # Compute references for each chunk (identifiers referenced from previous chunks)
            chunk_texts = [c.text for c in chunks]
            refs_list = self._compute_chunk_references(chunk_texts)

            for chunk, refs in zip(chunks, refs_list):
                # Container widget with top references area and bottom rewritten code
                container = QWidget()
                v = QVBoxLayout(container)
                v.setContentsMargins(0, 0, 0, 0)
                v.setSpacing(2)

                refs_view = QPlainTextEdit()
                refs_view.setReadOnly(True)
                refs_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
                refs_view.setFont(QFont("DejaVu Sans Mono", max(8, self.font_size - 2)))
                if refs:
                    refs_view.setPlainText("\n".join(sorted(refs)))
                else:
                    refs_view.setPlainText("(no references from previous chunks)")
                refs_view.setFixedHeight(24 + 16 * min(4, len(refs)))

                bottom = QPlainTextEdit()
                bottom.setReadOnly(True)
                bottom.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
                bottom.setFont(QFont("DejaVu Sans Mono", self.font_size))
                try:
                    chunk_text = prepare_chunk_text_for_run(chunk.text, document.raw_text)
                    rewritten, _, _ = transpile_for_split(chunk_text)
                except Exception as exc:
                    rewritten = f"[ERROR: {exc}]"
                apply_gray_include_format(bottom, rewritten)

                v.addWidget(refs_view)
                v.addWidget(bottom)

                self.chunk_tabs.addTab(container, f"Chunk {chunk.index}")
            return

        chunks = self.preview_chunks()

        if not self.editor.split_points:
            self.chunk_stack.setCurrentWidget(self.rewritten_chunk_view)
            rewritten = chunks[0][2] if chunks else ""
            apply_gray_include_format(self.rewritten_chunk_view, rewritten)
            return

        self.chunk_stack.setCurrentWidget(self.chunk_tabs)
        self.chunk_tabs.clear()

        # For non-DQC chunks, chunks is a list of (name, original, rewritten)
        original_texts = [orig for _, orig, _ in chunks]
        refs_list = self._compute_chunk_references(original_texts)

        for (name, _, rewritten), refs in zip(chunks, refs_list):
            container = QWidget()
            v = QVBoxLayout(container)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(2)

            refs_view = QPlainTextEdit()
            refs_view.setReadOnly(True)
            refs_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            refs_view.setFont(QFont("DejaVu Sans Mono", max(8, self.font_size - 2)))
            if refs:
                refs_view.setPlainText("\n".join(sorted(refs)))
            else:
                refs_view.setPlainText("(no references from previous chunks)")
            refs_view.setFixedHeight(24 + 16 * min(4, len(refs)))

            bottom = QPlainTextEdit()
            bottom.setReadOnly(True)
            bottom.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            bottom.setFont(QFont("DejaVu Sans Mono", self.font_size))
            apply_gray_include_format(bottom, rewritten)

            v.addWidget(refs_view)
            v.addWidget(bottom)

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
            chunks = [(f"Chunk {chunk.index}", prepare_chunk_text_for_run(chunk.text, document.raw_text)) for chunk in document.chunks]
        else:
            original_text = self.editor.toPlainText()
            piece_texts = self.extract_chunks_by_lines(original_text, set(self.editor.split_points))
            chunks = [(f"Chunk {i}", prepare_chunk_text_for_run(text, original_text)) for i, text in enumerate(piece_texts, 1)]

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

        self.current_dqc_file = None
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
