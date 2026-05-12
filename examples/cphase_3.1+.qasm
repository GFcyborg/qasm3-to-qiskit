OPENQASM 3.1;
include "stdgates.inc";
// gate renamed to avoid collision:
gate MY_cphase(θ) a, b
{
  U(0, 0, θ / 2) a;
  CX a, b;
  U(0, 0, -θ / 2) b;
  CX a, b;
  U(0, 0, θ / 2) b;
}

qubit[2] q;
bit[2] c;
MY_cphase(2 * π) q[0], q[1];
cphase(π / 2) q[0], q[1];

c = measure q;
