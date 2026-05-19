from pathlib import Path
import importlib.util
import sys

from qiskit_qasm3_import import parse


def _load_minimal_transpile():
    run_path = Path(__file__).resolve().parents[1] / "run.py"
    root = str(run_path.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location("run_module_for_tests", run_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.minimal_transpile


def test_minimal_transpile_keeps_qasm_prologue_first_for_hw_registers():
    minimal_transpile = _load_minimal_transpile()
    source = (Path(__file__).resolve().parents[1] / "examples" / "problematic" / "bell_state_hw-regs.qasm").read_text()

    rewritten, issues, program = minimal_transpile(source)
    lines = rewritten.splitlines()

    assert lines[0] == "OPENQASM 3.0;"
    assert lines[1] == 'include "stdgates.inc";'
    assert "qubit[2] hw;" in rewritten
    assert "bit[2] c;" in rewritten
    assert lines.index('include "stdgates.inc";') < lines.index("qubit[2] hw;")
    assert lines.index("qubit[2] hw;") < lines.index("bit[2] c;")
    assert "c[0] = measure hw[0];" in rewritten
    assert "c[1] = measure hw[1];" in rewritten
    assert parse(rewritten) is not None
    assert program is not None
    assert issues == [] or all(issue.kind for issue in issues)
