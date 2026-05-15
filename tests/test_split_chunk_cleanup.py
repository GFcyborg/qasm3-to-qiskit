from pathlib import Path
import tempfile

from dqc_container import parse_dqc_text, render_dqc_text
from split import clear_directory_contents


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


