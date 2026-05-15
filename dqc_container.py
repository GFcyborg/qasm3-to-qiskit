from __future__ import annotations

from dataclasses import dataclass
import re


PRAGMA_RE = re.compile(
    r"^\s*pragma\s+dqc\.v(?P<version>[0-9]+)\.split\s+id\s*=\s*(?P<id>[1-9][0-9]*)\s*$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class DqcChunk:
    index: int
    text: str


@dataclass(slots=True)
class DqcDocument:
    source_text: str
    raw_text: str
    chunks: list[DqcChunk]
    pragma_line_numbers: set[int]
    display_to_raw_after_line: dict[int, int]
    raw_split_after_lines: set[int]


def is_dqc_pragma_line(line: str) -> bool:
    return PRAGMA_RE.match(line.rstrip("\r\n")) is not None


def parse_dqc_text(text: str) -> DqcDocument:
    lines = text.splitlines(keepends=True)
    raw_lines: list[str] = []
    chunks: list[DqcChunk] = []
    current_chunk: list[str] = []
    pragma_line_numbers: set[int] = set()
    raw_split_after_lines: set[int] = set()
    display_to_raw_after_line: dict[int, int] = {}

    chunk_index = 1
    raw_line_count = 0
    for display_line_number, line in enumerate(lines, 1):
        if is_dqc_pragma_line(line):
            pragma_line_numbers.add(display_line_number)
            raw_split_after_lines.add(raw_line_count)
            display_to_raw_after_line[display_line_number] = raw_line_count
            chunks.append(DqcChunk(index=chunk_index, text="".join(current_chunk)))
            current_chunk = []
            chunk_index += 1
            continue

        current_chunk.append(line)
        raw_lines.append(line)
        raw_line_count += 1
        display_to_raw_after_line[display_line_number] = raw_line_count

    chunks.append(DqcChunk(index=chunk_index, text="".join(current_chunk)))
    if len(chunks) == 1 and not chunks[0].text and not lines:
        chunks = [DqcChunk(index=1, text="")]

    # Drop empty edge chunks introduced by malformed leading/trailing pragmas.
    chunks = [chunk for chunk in chunks if chunk.text or len(chunks) == 1]

    return DqcDocument(
        source_text=text,
        raw_text="".join(raw_lines),
        chunks=chunks,
        pragma_line_numbers=pragma_line_numbers,
        display_to_raw_after_line=display_to_raw_after_line,
        raw_split_after_lines={line for line in raw_split_after_lines if line > 0},
    )


def render_dqc_text(raw_text: str, split_after_lines: set[int]) -> str:
    lines = raw_text.splitlines(keepends=True)
    if not split_after_lines:
        return raw_text

    split_after_sorted = sorted(split_after_lines)
    split_idx = 0
    pragma_id = 1
    current_chunk: list[str] = []
    rendered: list[str] = []

    for i, line in enumerate(lines, 1):
        current_chunk.append(line)
        if split_idx < len(split_after_sorted) and i == split_after_sorted[split_idx]:
            rendered.append("".join(current_chunk))
            rendered.append(f"pragma dqc.v1.split id={pragma_id}\n")
            current_chunk = []
            split_idx += 1
            pragma_id += 1

    if current_chunk:
        rendered.append("".join(current_chunk))

    return "".join(rendered)


def strip_dqc_pragmas(text: str) -> str:
    return parse_dqc_text(text).raw_text


def display_split_lines_to_raw_split_after_lines(document: DqcDocument, split_lines: set[int]) -> set[int]:
    return {
        document.display_to_raw_after_line[line]
        for line in split_lines
        if line in document.display_to_raw_after_line
    }
