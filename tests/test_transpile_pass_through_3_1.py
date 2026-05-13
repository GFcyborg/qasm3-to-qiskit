from pathlib import Path

from qasm_rewriter import transpile_qasm


def test_transpile_pass_through_openqasm_3_1():
    path = Path(__file__).resolve().parents[1] / "examples" / "cphase_3.1+.qasm"
    source = path.read_text()
    rewritten, issues, program = transpile_qasm(source)
    # For OPENQASM 3.1 sources, the rewriter must return the original
    # source text unchanged and report no rewrite issues.
    assert rewritten == source
    assert issues == []
