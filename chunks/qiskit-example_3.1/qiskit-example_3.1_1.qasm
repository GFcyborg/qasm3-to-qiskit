// from: https://github.com/Qiskit/qiskit-qasm3-import
// WARNING: setting param (a:=0) results in infinite always-true while-loop.

OPENQASM 3.1;
// The 'stdgates.inc' include is supported, and the gates are only available
// if it has correctly been included.
include "stdgates.inc";

// Parametrised inputs are supported.
input float[64] a;

qubit[3] q;
bit[2] mid;
bit[3] out;

// Aliasing and re-aliasing are supported.
let aliased = q[0:1];

// Parametrised gates that make use of the stdlib.
gate my_gate(a) c, t {
  gphase(a / 2);
  ry(a) c;
  cx c, t;
}

