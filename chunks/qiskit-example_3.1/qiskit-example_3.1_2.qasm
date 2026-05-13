OPENQASM 3.0;
gate my_phase(a) c {
  ctrl @ inv @ gphase(a) c;
}
qubit[1] aliased;
qubit[3] q;
my_gate(a * 2) aliased[0], q[{1, 2}][0];
mid[0] = measure q[0];
mid[1] = measure q[1];
while (mid == "00") {
  reset q[0];
  reset q[1];
  my_gate(a) q[0], q[1];
  my_phase(a - pi / 2) q[1];
  mid[0] = measure q[0];
  mid[1] = measure q[1];
}
if (mid[0]) {
  let inner_alias = q[{0, 1}];
  reset inner_alias;
}
out = measure q;
