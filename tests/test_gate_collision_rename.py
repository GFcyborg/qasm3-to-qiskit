from qasm_rewriter import transpile_qasm


def test_colliding_user_gate_is_renamed_and_stdgates_stay_untouched():
    source = """OPENQASM 3.0;
include \"stdgates.inc\";

gate cphase(theta) a, b {
  U(0, 0, theta) a;
  CX a, b;
}

qubit[2] q;
bit[2] c;
cphase(pi / 2) q[0], q[1];
c = measure q;
"""

    rewritten, issues, program = transpile_qasm(source)

    assert 'gate cphase(lambda) a, b { ctrl @ p(lambda) a, b; }' in rewritten
    assert 'gate my_cphase(theta) a, b {' in rewritten
    assert 'my_cphase(pi / 2) q[0], q[1];' in rewritten
    assert 'gate cphase(theta) a, b {' not in rewritten
    assert any(issue.kind == 'quantumgatedefinition' for issue in issues)
    assert program is not None
