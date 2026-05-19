import openqasm3
from openqasm3 import ast

def pprint_obj(obj):
    if isinstance(obj, ast.Identifier):
        return obj.name
    if isinstance(obj, ast.IndexedIdentifier):
        return f"{obj.name.name}[{obj.indices}]"
    return str(obj)

def inspect_node(node, indent=0):
    pref = "  " * indent
    node_type = type(node).__name__
    
    info = ""
    if isinstance(node, ast.QuantumGateDefinition):
        info = f"name={node.name.name}"
    elif isinstance(node, ast.QuantumGate):
        qargs = [pprint_obj(arg) for arg in node.qubits]
        info = f"gate={node.name.name}, qargs={qargs}"
    elif isinstance(node, ast.QuantumMeasurementStatement):
        info = f"measure_target={pprint_obj(node.target) if node.target else 'None'}"
    elif isinstance(node, ast.ClassicalAssignment):
        info = f"lvalue={pprint_obj(node.lvalue)}, op={node.op}"
    elif isinstance(node, ast.ClassicalDeclaration):
        info = f"decl_name={node.identifier.name}"
    elif isinstance(node, ast.QubitDeclaration):
        info = f"qubit_name={node.quidentifier.name}"
    elif isinstance(node, ast.Include):
        info = f"filename={node.filename}"
    elif isinstance(node, ast.AliasStatement):
        info = f"name={node.target.name}"
    elif isinstance(node, ast.WhileLoop):
        info = f"while_condition={node.while_condition}"
    elif isinstance(node, ast.BranchingStatement):
         info = f"condition={node.condition}"
    elif isinstance(node, ast.QuantumReset):
        info = f"qubits={node.qubits}"

    print(f"{pref}{node_type}: {info}")

path = "examples/qiskit-example.qasm"
with open(path, "r") as f:
    program = openqasm3.parse(f.read())

for stmt in program.statements:
    inspect_node(stmt)
