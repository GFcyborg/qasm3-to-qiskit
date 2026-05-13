from pathlib import Path
import tempfile

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
