OPENQASM 3.0;
gate p(lambda) a { U(0, 0, lambda) a; }
gate phase(lambda) q { U(0, 0, lambda) q; }
gate x a { U(pi, 0, pi) a; }
gate y a { U(pi, pi/2, pi/2) a; }
gate z a { U(0, 0, pi) a; }
gate h a { U(pi/2, 0, pi) a; }
gate s a { U(0, 0, pi/2) a; }
gate sdg a { U(0, 0, -pi/2) a; }
gate t a { U(0, 0, pi/4) a; }
gate tdg a { U(0, 0, -pi/4) a; }
gate sx a { U(pi/2, -pi/2, pi/2) a; }
gate rx(theta) a { U(theta, -pi/2, pi/2) a; }
gate ry(theta) a { U(theta, 0, 0) a; }
gate rz(lambda) a { U(0, 0, lambda) a; }
gate u1(lambda) q { U(0, 0, lambda) q; }
gate u2(phi, lambda) q { U(pi/2, phi, lambda) q; }
gate u3(theta, phi, lambda) q { U(theta, phi, lambda) q; }
gate id a { U(0, 0, 0) a; }
gate cx a, b { ctrl @ x a, b; }
gate cy a, b { ctrl @ y a, b; }
gate cz a, b { ctrl @ z a, b; }
gate cp(lambda) a, b { ctrl @ p(lambda) a, b; }
gate cphase(lambda) a, b { ctrl @ p(lambda) a, b; }
gate crx(theta) a, b { ctrl @ rx(theta) a, b; }
gate cry(theta) a, b { ctrl @ ry(theta) a, b; }
gate crz(lambda) a, b { ctrl @ rz(lambda) a, b; }
gate ch a, b { ctrl @ h a, b; }
gate swap a, b { cx a, b; cx b, a; cx a, b; }
gate ccx a, b, c { ctrl @ ctrl @ x a, b, c; }
gate cswap a, b, c { ctrl @ swap a, b, c; }
gate cu(theta, phi, lambda, gamma) a, b { p(gamma - theta / 2) a; ctrl @ U(theta, phi, lambda) a, b; }
qubit[4] q;
if (c == 2) {
  rz(pi / 2) q[2];
}
if (c == 3) {
  rz(pi / 2 + pi / 4) q[2];
}
h q[2];
c[2] = measure q[2];
if (c == 1) {
  rz(pi / 8) q[3];
}
if (c == 2) {
  rz(pi / 4) q[3];
}
if (c == 3) {
  rz(pi / 4 + pi / 8) q[3];
}
if (c == 4) {
  rz(pi / 2) q[3];
}
if (c == 5) {
  rz(pi / 2 + pi / 8) q[3];
}
if (c == 6) {
  rz(pi / 2 + pi / 4) q[3];
}
if (c == 7) {
  rz(pi / 2 + pi / 4 + pi / 8) q[3];
}
h q[3];
c[3] = measure q[3];
