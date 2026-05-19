from pathlib import Path
import tempfile

from dqc_container import parse_dqc_text, prepare_chunk_text_for_run, render_dqc_text
from split import (
    clear_directory_contents,
    compute_chunk_flows,
    dqc_display_split_before_lines,
    dqc_display_split_before_to_raw_split_after_lines,
    format_flow_lines_html,
    line_is_inside_blocking_scope,
    normalize_dqc_clicked_split_line,
)
from openqasm3 import parse


def test_clear_directory_contents_removes_previous_chunks():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target = root / "chunks" / "example"
        target.mkdir(parents=True)
        (target / "old_1.qasm").write_text("old chunk")
        (target / "old_2.qasm").write_text("old chunk 2")
        nested = target / "nested"
        nested.mkdir()
        (nested / "keep.txt").write_text("nested")

        clear_directory_contents(target)

        assert target.exists()
        assert list(target.iterdir()) == []

        (target / "new_1.qasm").write_text("new chunk")
        assert (target / "new_1.qasm").exists()


def test_render_dqc_text_roundtrips_chunks_and_pragmas():
    raw_text = "line 1\nline 2\nline 3\n"
    dqc_text = render_dqc_text(raw_text, {1, 2})
    document = parse_dqc_text(dqc_text)

    assert document.raw_text == raw_text
    assert len(document.chunks) == 3
    assert document.chunks[0].text == "line 1\n"
    assert document.chunks[1].text == "line 2\n"
    assert document.chunks[2].text == "line 3\n"
    assert document.pragma_line_numbers == {2, 4}


def test_dqc_display_split_before_lines_and_conversion_are_split_before_semantics():
    raw_text = "line 1\nline 2\nline 3\n"
    dqc_text = render_dqc_text(raw_text, {1, 2})
    document = parse_dqc_text(dqc_text)

    # pragma lines are {2, 4}, but user-visible split markers should be on the
    # first line of each next chunk: lines 3 and 5 in display text.
    assert dqc_display_split_before_lines(document) == {3, 5}

    # Converting those markers back to raw split-after lines should round-trip.
    assert dqc_display_split_before_to_raw_split_after_lines(document, {3, 5}) == {1, 2}


def test_normalize_dqc_clicked_split_line_maps_pragma_to_same_split_marker():
    raw_text = "line 1\nline 2\nline 3\n"
    dqc_text = render_dqc_text(raw_text, {1, 2})

    # Pragmas are at lines 2 and 4. They should map to split markers at 3 and 5.
    assert normalize_dqc_clicked_split_line(dqc_text, 2) == 3
    assert normalize_dqc_clicked_split_line(dqc_text, 4) == 5
    # Clicking directly on marker lines remains unchanged.
    assert normalize_dqc_clicked_split_line(dqc_text, 3) == 3
    assert normalize_dqc_clicked_split_line(dqc_text, 5) == 5


def test_prepare_chunk_text_for_run_preserves_openqasm_3_1_header():
    source_text = "OPENQASM 3.1;\ninclude \"stdgates.inc\";\nqubit[1] q;\n"
    chunk_text = "qubit[1] q;\n"

    prepared = prepare_chunk_text_for_run(chunk_text, source_text)

    assert prepared == "OPENQASM 3.1;\ninclude \"stdgates.inc\";\nqubit[1] q;\n"


def test_prepare_chunk_text_for_run_leaves_existing_header_untouched():
    source_text = "OPENQASM 3.1;\ninclude \"stdgates.inc\";\nqubit[1] q;\n"
    chunk_text = "OPENQASM 3.1;\ninclude \"stdgates.inc\";\nqubit[1] q;\n"

    assert prepare_chunk_text_for_run(chunk_text, source_text) == chunk_text


def test_line_is_inside_blocking_scope_catches_while_loops():
    source_text = Path(__file__).resolve().parents[1] / "examples" / "qiskit-example.qasm"
    source_text = source_text.read_text()
    program = parse(source_text)

    assert line_is_inside_blocking_scope(program, 38)
    assert line_is_inside_blocking_scope(program, 37)
    assert not line_is_inside_blocking_scope(program, 45)


def test_format_flow_lines_html_bolds_qubit_names_only():
    text = format_flow_lines_html({"q": {1, 3}, "c": {2}}, "<-", {"q"})

    assert "<b>q</b> &lt;- Chunk 1, Chunk 3" in text
    assert "c &lt;- Chunk 2" in text


def test_compute_chunk_flows_uses_latest_write_before_rvalue_use():
    source_text = "OPENQASM 3.0;\n"
    chunks = [
        "int[32] x = 0;\n",
        "x = 1;\nint[32] y = x;\n",
        "int[32] z = x;\n",
    ]

    flows = compute_chunk_flows(chunks, source_text)

    assert flows[0].incoming_sources == {}
    assert flows[0].outgoing_targets == {}
    assert flows[1].incoming_sources == {}
    assert flows[1].outgoing_targets == {"x": {3}}
    assert flows[2].incoming_sources == {"x": {2}}


def test_compute_chunk_flows_treats_qubit_targets_as_updates():
    source_text = 'OPENQASM 3.0;\ninclude "stdgates.inc";\n'
    chunks = [
        "qubit[1] q;\n",
        "h q[0];\n",
        "h q[0];\n",
    ]

    flows = compute_chunk_flows(chunks, source_text)

    assert flows[0].outgoing_targets == {"q": {2}}
    assert flows[1].incoming_sources == {"q": {1}}
    assert flows[1].outgoing_targets == {"q": {3}}
    assert flows[2].incoming_sources == {"q": {2}}


