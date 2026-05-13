#!/usr/bin/env python
"""QASM3 Splitter - Split OpenQASM 3 files at statement boundaries.

Allows users to load a QASM file, mark split points via right-click,
preview chunks in tabs, save them to disk, and launch independent run.py instances.
"""

from __future__ import annotations

import sys
import subprocess
import tempfile
import json
import shutil
from pathlib import Path
from dataclasses import dataclass
from typing import Any

import PySide6
from PySide6.QtCore import Qt, QTimer, QSize, QRect, QEvent
from PySide6.QtGui import QColor, QFont, QPainter, QAction, QKeySequence, QCursor, QTextCursor
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
    QInputDialog,
    QLineEdit,
    QMenu,
    QToolBar,
)

from openqasm3 import parse
from qasm_rewriter import transpile_qasm, kind, span, node_iter


ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


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
        """Show right-click menu to add/remove split points."""
        cursor = self.cursorForPosition(pos)
        line = cursor.blockNumber() + 1  # Convert to 1-indexed
        
        menu = QMenu(self)
        if line in self.split_points:
            remove_action = QAction(f"Remove split at line {line}", self)
            remove_action.triggered.connect(lambda: self.remove_split_point(line))
            menu.addAction(remove_action)
        else:
            add_action = QAction(f"Add split after line {line}", self)
            add_action.triggered.connect(lambda: self.add_split_point(line))
            menu.addAction(add_action)
        
        menu.exec(self.mapToGlobal(pos))

    def add_split_point(self, line: int) -> None:
        """Mark a split point at line (1-indexed, means split AFTER this line)."""
        self.split_points.add(line)
        self.line_number_area.update()

    def remove_split_point(self, line: int) -> None:
        """Remove a split point."""
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
        self.current_chunks_dir: Path | None = None  # Track if we're viewing chunks
        self.current_program: Any | None = None
        self.font_size = 10
        
        # Left pane: original code with split markers
        self.editor = CodeEditor()
        editor_panel, _ = self.make_titled_panel("Original QASM (right-click to split)", "#d8ecff", self.editor)
        
        # Right pane: tabbed chunks
        self.chunk_tabs = QTabWidget()
        chunk_panel, _ = self.make_titled_panel("Chunks (rewritten)", "#ffe7c2", self.chunk_tabs)
        
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
        
        open_action = QAction("Open QASM file...", self)
        open_action.triggered.connect(self.open_file)
        file_menu.addAction(open_action)
        
        open_chunks_action = QAction("Open chunks directory...", self)
        open_chunks_action.triggered.connect(self.open_chunks_directory)
        file_menu.addAction(open_chunks_action)
        
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
        for widget in (self.editor, self.chunk_tabs):
            widget.setFont(font)

    def set_font_size(self, size: int) -> None:
        self.font_size = size
        self.apply_font()

    def open_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Open QASM", str(EXAMPLES), "QASM files (*.qasm);;All files (*)"
        )
        if name:
            self.load_file(Path(name))

    def open_chunks_directory(self) -> None:
        """Open a chunks directory to view/run existing chunks."""
        dir_path = QFileDialog.getExistingDirectory(
            self, "Open chunks directory", str(ROOT / "chunks")
        )
        if dir_path:
            self.load_chunks_directory(Path(dir_path))

    def load_file(self, path: Path) -> None:
        try:
            text = path.read_text()
            self.editor.setPlainText(text)
            self.editor.split_points.clear()
            self.current_file = path
            self.current_chunks_dir = None
            self.current_program = None
            
            # Parse to get statements and mark them
            try:
                self.current_program = parse(text)
            except Exception:
                pass
            
            self.chunk_tabs.clear()
            self.split_button.setEnabled(True)
            self.run_button.setEnabled(False)
            self.status_label.setText(f"Loaded {path.name}")
            self.setWindowTitle(f"QASM3 Splitter - {path.resolve()}")
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            self.status_label.setText("Load failed")

    def load_chunks_directory(self, chunks_dir: Path) -> None:
        """Load and display all chunks from a chunks directory."""
        try:
            # Find all chunk files
            chunk_files = sorted(chunks_dir.glob("*.qasm"))
            if not chunk_files:
                QMessageBox.warning(self, "No chunks", f"No .qasm files found in {chunks_dir}")
                return
            
            # Load metadata if available
            metadata_file = chunks_dir / ".split_metadata.json"
            original_file = None
            if metadata_file.exists():
                try:
                    metadata = json.loads(metadata_file.read_text())
                    original_file = metadata.get("original_file")
                except Exception:
                    pass
            
            # Load chunks into tabs
            self.current_file = None
            self.current_chunks_dir = chunks_dir
            self.current_program = None
            self.editor.setPlainText("")
            self.editor.split_points.clear()
            self.chunk_tabs.clear()
            
            for chunk_file in chunk_files:
                tab = QPlainTextEdit()
                tab.setReadOnly(True)
                tab.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
                tab.setFont(QFont("DejaVu Sans Mono", self.font_size))
                content = chunk_file.read_text()
                tab.setPlainText(content)
                self.chunk_tabs.addTab(tab, chunk_file.stem)
            
            self.split_button.setEnabled(False)  # Can't split chunks directory
            self.run_button.setEnabled(True)  # Can run the chunks
            msg = f"Loaded {len(chunk_files)} chunks from {chunks_dir.name}"
            if original_file:
                msg += f"\nOriginal: {original_file}"
            self.status_label.setText(msg)
            self.setWindowTitle(f"QASM3 Splitter - Chunks: {chunks_dir.resolve()}")
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

    def preview_chunks(self) -> list[tuple[str, str, str]]:
        """Generate and preview chunks. Returns list of (name, original, rewritten)."""
        if not self.editor.toPlainText():
            return []
        
        original_text = self.editor.toPlainText()
        chunks = self.extract_chunks_by_lines(original_text, self.editor.split_points)
        
        result: list[tuple[str, str, str]] = []
        for i, chunk_text in enumerate(chunks, 1):
            try:
                rewritten, _, _ = transpile_qasm(chunk_text)
            except Exception as exc:
                rewritten = f"[ERROR: {exc}]"
            
            result.append((f"Chunk {i}", chunk_text, rewritten))
        
        return result

    def refresh_chunk_tabs(self) -> None:
        """Refresh the chunk preview tabs."""
        self.chunk_tabs.clear()
        chunks = self.preview_chunks()
        
        for name, _, rewritten in chunks:
            tab = QPlainTextEdit()
            tab.setReadOnly(True)
            tab.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            tab.setFont(QFont("DejaVu Sans Mono", self.font_size))
            tab.setPlainText(rewritten)
            self.chunk_tabs.addTab(tab, name)

    def save_chunks(self) -> None:
        """Save chunks to disk in chunks/<filename>/ directory."""
        if not self.current_file:
            QMessageBox.warning(self, "No file", "Load a QASM file first")
            return
        
        chunks_data = self.preview_chunks()
        if not chunks_data:
            QMessageBox.warning(self, "No chunks", "No content to split")
            return
        
        # Create output directory: chunks/<filename>/
        base_name = self.current_file.stem
        chunks_parent = ROOT / "chunks"
        chunks_parent.mkdir(exist_ok=True)
        out_dir = chunks_parent / base_name
        clear_directory_contents(out_dir)
        
        # Save chunks
        chunk_files: list[Path] = []
        for i, (_, original, rewritten) in enumerate(chunks_data, 1):
            chunk_file = out_dir / f"{base_name}_{i}.qasm"
            chunk_file.write_text(rewritten)
            chunk_files.append(chunk_file)
        
        # Save metadata
        metadata = {
            "original_file": str(self.current_file),
            "chunk_count": len(chunks_data),
            "chunk_files": [str(f) for f in chunk_files],
        }
        metadata_file = out_dir / ".split_metadata.json"
        metadata_file.write_text(json.dumps(metadata, indent=2))
        
        self.run_button.setEnabled(True)
        self.status_label.setText(f"Saved {len(chunks_data)} chunks to chunks/{base_name}/")
        QMessageBox.information(
            self,
            "Chunks saved",
            f"Saved {len(chunks_data)} chunks to:\n{out_dir}",
        )

    def run_chunks(self) -> None:
        """Launch independent run.py windows for each chunk."""
        if self.current_chunks_dir:
            # Running chunks from a directory
            chunks_dir = self.current_chunks_dir
        elif self.current_file:
            # Running chunks from a split file
            base_name = self.current_file.stem
            chunks_parent = ROOT / "chunks"
            chunks_dir = chunks_parent / base_name
        else:
            QMessageBox.warning(self, "No chunks", "Load a QASM file or chunks directory first")
            return
        
        if not chunks_dir.exists():
            QMessageBox.warning(self, "No chunks directory", f"Directory not found: {chunks_dir}")
            return
        
        # Find chunk files
        base_name = chunks_dir.name
        chunk_files = sorted(chunks_dir.glob(f"{base_name}_*.qasm"))
        if not chunk_files:
            QMessageBox.warning(self, "No chunk files", f"No chunks found in {chunks_dir}")
            return
        
        for chunk_file in chunk_files:
            try:
                # Launch run.py with this chunk
                script = ROOT / "run.py"
                subprocess.Popen(
                    [sys.executable, str(script), str(chunk_file)],
                    cwd=str(ROOT),
                )
            except Exception as exc:
                QMessageBox.critical(self, "Launch failed", f"Failed to launch run.py: {exc}")
                return
        
        self.status_label.setText(f"Launched {len(chunk_files)} run.py instances")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("QASM3 Splitter")
    app.setOrganizationName("Copilot")
    
    window = SplitWindow()
    window.show()
    
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
