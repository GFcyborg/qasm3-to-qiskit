from pathlib import Path
import tempfile

from dqc_container import parse_dqc_text, prepare_chunk_text_for_run, render_dqc_text
from split import clear_directory_contents, line_is_inside_blocking_scope
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
    source_text = Path(__file__).resolve().parents[1] / "examples" / "qiskit-example_3.1.qasm"
    source_text = source_text.read_text()
    program = parse(source_text)

    assert line_is_inside_blocking_scope(program, 38)
    assert line_is_inside_blocking_scope(program, 37)
    assert not line_is_inside_blocking_scope(program, 45)


